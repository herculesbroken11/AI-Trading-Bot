"""Tastytrade sandbox broker adapter — cert API only, no production routing."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from backend.adapters.broker.sandbox_auth import SandboxAuthError, SandboxOAuthClient
from backend.adapters.broker.sandbox_http_retry import (
    is_transient_transport_error,
    should_retry_http_status,
)
from backend.adapters.broker.sandbox_rate_limiter import (
    RateLimitCooldownActive,
    RateLimitInfo,
    SandboxRateLimiter,
    get_sandbox_rate_limiter,
)
from backend.adapters.broker.sandbox_order_verification import (
    broker_status_is_filled,
    extract_broker_order_id,
    resolve_execution_fill_price,
    summarize_order_response,
)
from backend.adapters.broker.sandbox_step_diagnostics import (
    StepFailureDiagnostics,
    build_step_failure,
)
from backend.config.settings import Settings
from backend.config.tastytrade_urls import (
    ALLOWED_SANDBOX_SYMBOLS,
    SANDBOX_BASE_URL,
    SANDBOX_MAX_ORDER_QUANTITY,
    BrokerUrlBlockedError,
    assert_execution_component,
    assert_sandbox_base_url,
)
from backend.risk.models import ExecutionResult, OrderIntent

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
ACCOUNT_CACHE_TTL_SECONDS = 60.0
READ_CACHE_TTL_SECONDS = 10.0
MAX_TRANSIENT_ATTEMPTS = 3
TRANSIENT_BACKOFF_SECONDS = (0.25, 0.5, 1.0)
CUSTOMERS_ME_ACCOUNTS_PATH = "/customers/me/accounts"
EQUITY_BUY_ACTION = "Buy to Open"
EQUITY_SELL_CLOSE_ACTION = "Sell to Close"
PRICE_EFFECT_DEBIT = "Debit"
PRICE_EFFECT_CREDIT = "Credit"


def _coerce_amount(value: Any) -> Optional[float]:
    """Coerce Tastytrade balance fields (may be str) to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class SandboxApiError(Exception):
    status_code: int
    message: str
    body: Optional[Dict[str, Any]] = None
    step_diagnostics: Optional[StepFailureDiagnostics] = None
    rate_limit: Optional[RateLimitInfo] = None

    def __str__(self) -> str:
        parts = []
        if self.step_diagnostics:
            parts.append(self.step_diagnostics.format_safe())
        else:
            parts.append(self.message)
        if self.rate_limit:
            parts.append(self.rate_limit.format_safe())
        return "\n".join(parts)


def build_equity_order_payload(
    *,
    symbol: str,
    quantity: int,
    order_type: str = "Market",
    time_in_force: str = "Day",
    limit_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Build Tastytrade equity order JSON with dashed keys (Market or Limit)."""
    normalized_type = order_type.strip().capitalize()
    if normalized_type not in {"Market", "Limit"}:
        raise SandboxApiError(400, f"Unsupported order_type {order_type!r} in Phase 2 sandbox adapter")

    payload: Dict[str, Any] = {
        "time-in-force": time_in_force,
        "order-type": normalized_type,
        "price-effect": PRICE_EFFECT_DEBIT,
        "legs": [
            {
                "instrument-type": "Equity",
                "symbol": symbol.strip().upper(),
                "quantity": quantity,
                "action": EQUITY_BUY_ACTION,
            }
        ],
    }
    if normalized_type == "Limit":
        if limit_price is None:
            raise SandboxApiError(400, "Limit orders require limit_price")
        payload["price"] = f"{float(limit_price):.2f}"
    return payload


def build_equity_close_payload(
    *,
    symbol: str,
    quantity: int,
    order_type: str = "Market",
    time_in_force: str = "Day",
    limit_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Build Tastytrade equity close JSON (Sell to Close, sandbox smoke only)."""
    normalized_type = order_type.strip().capitalize()
    if normalized_type not in {"Market", "Limit"}:
        raise SandboxApiError(400, f"Unsupported order_type {order_type!r} in Phase 2 sandbox adapter")

    payload: Dict[str, Any] = {
        "time-in-force": time_in_force,
        "order-type": normalized_type,
        "price-effect": PRICE_EFFECT_CREDIT,
        "legs": [
            {
                "instrument-type": "Equity",
                "symbol": symbol.strip().upper(),
                "quantity": quantity,
                "action": EQUITY_SELL_CLOSE_ACTION,
            }
        ],
    }
    if normalized_type == "Limit":
        if limit_price is None:
            raise SandboxApiError(400, "Limit close orders require limit_price")
        payload["price"] = f"{float(limit_price):.2f}"
    return payload


class TastytradeSandboxAdapter:
    """
    Sandbox-only Tastytrade integration.

    Does not import or call legacy trade_exec.py.
    Base URL is hardcoded to https://api.cert.tastyworks.com.
    """

    def __init__(
        self,
        settings: Settings,
        auth: Optional[SandboxOAuthClient] = None,
        *,
        rate_limiter: Optional[SandboxRateLimiter] = None,
        clock: Callable[[], float] = time.time,
        account_cache_ttl: float = ACCOUNT_CACHE_TTL_SECONDS,
        read_cache_ttl: float = READ_CACHE_TTL_SECONDS,
    ) -> None:
        assert_sandbox_base_url(SANDBOX_BASE_URL)
        if settings.tastytrade_env.strip().lower() != "sandbox":
            raise SandboxAuthError("TastytradeSandboxAdapter requires TASTYTRADE_ENV=sandbox")
        if settings.live_trading_enabled:
            raise SandboxAuthError("LIVE_TRADING_ENABLED must be false for sandbox adapter")
        try:
            assert_execution_component(auth, role="TastytradeSandboxAdapter.auth")
        except BrokerUrlBlockedError as exc:
            raise SandboxAuthError(str(exc)) from exc
        self._settings = settings
        self._rate_limiter = rate_limiter
        self._auth = auth or SandboxOAuthClient(settings, rate_limiter=rate_limiter)
        self._clock = clock
        self._account_cache_ttl = account_cache_ttl
        self._read_cache_ttl = read_cache_ttl
        self._selected_account: Optional[str] = None
        self._selected_account_at: float = 0.0
        self._accounts_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._read_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE_URL

    @property
    def rate_limiter(self) -> SandboxRateLimiter:
        return self._rate_limiter or get_sandbox_rate_limiter()

    def invalidate_read_cache(self) -> None:
        """Drop cached balances/positions/live orders after any state-changing call."""
        self._read_cache.clear()

    def _cached_read(self, kind: str, account: str, loader: Callable[[], Any], force_refresh: bool) -> Any:
        key = (kind, account)
        now = self._clock()
        cached = self._read_cache.get(key)
        if not force_refresh and cached and now - cached[0] < self._read_cache_ttl:
            return cached[1]
        value = loader()
        self._read_cache[key] = (now, value)
        return value

    def _acquire(self, group: str, *, step: str, path: str) -> None:
        try:
            self.rate_limiter.acquire(group, step=step)
        except RateLimitCooldownActive as exc:
            raise SandboxApiError(
                429,
                f"Sandbox API cooldown active; {step} request not sent. Wait before retrying.",
                step_diagnostics=StepFailureDiagnostics(
                    step=step,
                    status_code=429,
                    endpoint_path=path,
                    authorization_present=False,
                    user_agent_present=True,
                    provider_message="local cooldown active after prior 429; request not sent",
                ),
                rate_limit=exc.info,
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        step: str,
        group: str,
        json: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """
        Throttled sandbox request.

        Retries only 502/503/504 and transient transport errors. 429 is never retried:
        it starts a cooldown and raises immediately so callers stop the workflow.
        """
        url = f"{SANDBOX_BASE_URL}{path}"
        assert_sandbox_base_url(SANDBOX_BASE_URL)
        headers = self._auth.request_headers()
        last_response: Optional[httpx.Response] = None
        refreshed_after_401 = False

        for attempt in range(MAX_TRANSIENT_ATTEMPTS):
            is_last = attempt >= MAX_TRANSIENT_ATTEMPTS - 1
            self._acquire(group, step=step, path=path)
            try:
                with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                    response = client.request(method, url, headers=headers, json=json)
            except Exception as exc:
                if is_transient_transport_error(exc) and not is_last:
                    time.sleep(TRANSIENT_BACKOFF_SECONDS[min(attempt, len(TRANSIENT_BACKOFF_SECONDS) - 1)])
                    continue
                raise SandboxApiError(
                    503,
                    f"Sandbox API transport error for step {step}: {type(exc).__name__}",
                    step_diagnostics=StepFailureDiagnostics(
                        step=step,
                        status_code=503,
                        endpoint_path=path,
                        authorization_present=True,
                        user_agent_present=True,
                        provider_message=type(exc).__name__,
                    ),
                ) from exc

            last_response = response
            status = response.status_code
            if status < 400:
                return response
            if status == 429:
                info = self.rate_limiter.record_rate_limited(
                    group,
                    step=step,
                    retry_after=response.headers.get("Retry-After"),
                )
                raise self._parse_error(
                    response,
                    step=step,
                    path=path,
                    request_headers=headers,
                    rate_limit=info,
                )
            if status == 401 and not refreshed_after_401 and not is_last:
                refreshed_after_401 = True
                self._auth.refresh_access_token()
                headers = self._auth.request_headers()
                continue
            if should_retry_http_status(status) and not is_last:
                time.sleep(TRANSIENT_BACKOFF_SECONDS[min(attempt, len(TRANSIENT_BACKOFF_SECONDS) - 1)])
                continue
            raise self._parse_error(
                response,
                step=step,
                path=path,
                request_headers=headers,
            )

        assert last_response is not None
        raise self._parse_error(
            last_response,
            step=step,
            path=path,
            request_headers=headers,
        )

    def _parse_error(
        self,
        response: httpx.Response,
        *,
        step: str,
        path: str,
        request_headers: Dict[str, str],
        symbol: Optional[str] = None,
        rate_limit: Optional[RateLimitInfo] = None,
    ) -> SandboxApiError:
        step_diag = build_step_failure(
            step=step,
            status_code=response.status_code,
            endpoint_path=path,
            request_headers=request_headers,
            response_text=response.text,
        )
        if response.status_code == 422:
            msg = (
                f"Sandbox API returned 422 for {symbol or step}. "
                "One or more preflight checks failed."
            )
        elif response.status_code == 429:
            cooldown = int(round(rate_limit.cooldown_seconds)) if rate_limit else None
            msg = (
                f"Sandbox API rate_limited (429) at step {step}; wait before retrying"
                + (f" (cooldown_seconds={cooldown})." if cooldown is not None else ".")
            )
        elif response.status_code >= 500:
            msg = f"Sandbox API server error ({response.status_code})."
        elif response.status_code == 403:
            msg = f"Sandbox API returned 403 for step {step}."
        elif response.status_code == 401:
            msg = "Sandbox authentication failed. Check credentials and User-Agent header."
        else:
            msg = f"Sandbox API error ({response.status_code}) for step {step}."
        try:
            body = response.json()
        except Exception:
            body = None
        return SandboxApiError(
            response.status_code,
            msg,
            body,
            step_diagnostics=step_diag,
            rate_limit=rate_limit,
        )

    def _validate_equity_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: Optional[float],
    ) -> tuple[str, str]:
        symbol = symbol.strip().upper()
        side = side.strip().lower()
        order_type = order_type.strip().capitalize()
        if symbol not in ALLOWED_SANDBOX_SYMBOLS:
            raise SandboxApiError(400, f"Symbol {symbol!r} not allowed in Phase 2 sandbox adapter")
        if side != "buy":
            raise SandboxApiError(400, "Only buy orders are allowed in Phase 2 sandbox adapter")
        if quantity <= 0 or quantity > SANDBOX_MAX_ORDER_QUANTITY:
            raise SandboxApiError(
                400,
                f"Sandbox quantity must be 1..{SANDBOX_MAX_ORDER_QUANTITY}, got {quantity}",
            )
        if order_type == "Limit" and limit_price is None:
            raise SandboxApiError(400, "Limit orders require limit_price")
        return symbol, order_type

    def _validate_equity_close(self, symbol: str, quantity: int, order_type: str, limit_price: Optional[float]) -> str:
        symbol = symbol.strip().upper()
        order_type = order_type.strip().capitalize()
        if symbol not in ALLOWED_SANDBOX_SYMBOLS:
            raise SandboxApiError(400, f"Symbol {symbol!r} not allowed in Phase 2 sandbox adapter")
        if quantity <= 0 or quantity > SANDBOX_MAX_ORDER_QUANTITY:
            raise SandboxApiError(
                400,
                f"Sandbox quantity must be 1..{SANDBOX_MAX_ORDER_QUANTITY}, got {quantity}",
            )
        if order_type == "Limit" and limit_price is None:
            raise SandboxApiError(400, "Limit close orders require limit_price")
        return symbol

    def _post_equity_order(
        self,
        account_number: Optional[str],
        *,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        time_in_force: str,
        limit_price: Optional[float],
        dry_run: bool,
    ) -> Dict[str, Any]:
        symbol, order_type = self._validate_equity_order(
            symbol, side, quantity, order_type, limit_price
        )
        acct = self._resolve_account_number(account_number)
        order_data = build_equity_order_payload(
            symbol=symbol,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=limit_price,
        )
        suffix = "/dry-run" if dry_run else ""
        step = "dry_run_order" if dry_run else "submit_order"
        path = f"/accounts/{acct}/orders{suffix}"
        if not dry_run:
            self.invalidate_read_cache()
        response = self._request("POST", path, step=step, group="orders", json=order_data)
        return response.json()

    def _post_equity_close(
        self,
        account_number: Optional[str],
        *,
        symbol: str,
        quantity: int,
        order_type: str,
        time_in_force: str,
        limit_price: Optional[float],
        dry_run: bool,
    ) -> Dict[str, Any]:
        symbol = self._validate_equity_close(symbol, quantity, order_type, limit_price)
        acct = self._resolve_account_number(account_number)
        order_data = build_equity_close_payload(
            symbol=symbol,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=limit_price,
        )
        suffix = "/dry-run" if dry_run else ""
        step = "dry_run_close" if dry_run else "submit_close"
        path = f"/accounts/{acct}/orders{suffix}"
        if not dry_run:
            self.invalidate_read_cache()
        response = self._request("POST", path, step=step, group="orders", json=order_data)
        return response.json()

    def get_customers_me(self) -> Dict[str, Any]:
        """Fetch authenticated sandbox customer profile (read smoke step)."""
        self._auth.ensure_authenticated()
        response = self._request("GET", "/customers/me", step="get_customers_me", group="customer")
        return response.json().get("data", {})

    def get_accounts(self, *, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """List sandbox accounts via GET /customers/me/accounts only (cached ~60s)."""
        now = self._clock()
        if (
            not force_refresh
            and self._accounts_cache is not None
            and now - self._accounts_cache[0] < self._account_cache_ttl
        ):
            return list(self._accounts_cache[1])
        self._auth.ensure_authenticated()
        response = self._request(
            "GET",
            CUSTOMERS_ME_ACCOUNTS_PATH,
            step="get_accounts",
            group="accounts",
        )
        items = response.json().get("data", {}).get("items", [])
        items = items if isinstance(items, list) else []
        self._accounts_cache = (now, items)
        return list(items)

    def get_selected_account(self) -> str:
        """Selected sandbox account number (cached ~60s; reuses cached account list)."""
        return self._resolve_account_number(None)

    def _resolve_account_number(self, account_number: Optional[str] = None) -> str:
        if account_number:
            return account_number
        if (
            self._selected_account
            and self._clock() - self._selected_account_at < self._account_cache_ttl
        ):
            return self._selected_account
        accounts = self.get_accounts()
        if not accounts:
            raise SandboxApiError(
                404,
                "No sandbox accounts found",
                step_diagnostics=StepFailureDiagnostics(
                    step="get_accounts",
                    status_code=404,
                    endpoint_path=CUSTOMERS_ME_ACCOUNTS_PATH,
                    authorization_present=True,
                    user_agent_present=True,
                    provider_message="No accounts returned",
                ),
            )
        acct = accounts[0]
        number = acct.get("account-number") or acct.get("account_number")
        if not number and isinstance(acct.get("account"), dict):
            number = acct["account"].get("account-number")
        if not number:
            raise SandboxApiError(404, "Could not resolve sandbox account number")
        self._selected_account = str(number)
        self._selected_account_at = self._clock()
        return self._selected_account

    def get_balance(
        self,
        account_number: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        acct = self._resolve_account_number(account_number)

        def _load() -> Dict[str, Any]:
            path = f"/accounts/{acct}/balances"
            response = self._request("GET", path, step="get_balance", group="balances")
            payload = response.json().get("data", {})
            return {
                "account_number": acct,
                "cash_balance": _coerce_amount(payload.get("cash-balance")),
                "buying_power": _coerce_amount(
                    payload.get("day-trading-buying-power") or payload.get("equity-buying-power")
                ),
                "day_pnl": _coerce_amount(payload.get("day-pnl")),
            }

        return dict(self._cached_read("balances", acct, _load, force_refresh))

    def get_positions(
        self,
        account_number: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        acct = self._resolve_account_number(account_number)

        def _load() -> List[Dict[str, Any]]:
            path = f"/accounts/{acct}/positions"
            response = self._request("GET", path, step="get_positions", group="positions")
            return response.json().get("data", {}).get("items", [])

        return list(self._cached_read("positions", acct, _load, force_refresh))

    def dry_run_equity_order(
        self,
        account_number: Optional[str],
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "Market",
        time_in_force: str = "Day",
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Preflight order via POST /accounts/{account}/orders/dry-run (no placement)."""
        self._auth.ensure_authenticated()
        return self._post_equity_order(
            account_number,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=limit_price,
            dry_run=True,
        )

    def submit_equity_order(
        self,
        account_number: Optional[str],
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "Market",
        time_in_force: str = "Day",
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Submit order via POST /accounts/{account}/orders."""
        self._auth.ensure_authenticated()
        return self._post_equity_order(
            account_number,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=limit_price,
            dry_run=False,
        )

    def get_order(self, account_number: Optional[str], order_id: str) -> Dict[str, Any]:
        acct = self._resolve_account_number(account_number)
        path = f"/accounts/{acct}/orders/{order_id}"
        response = self._request("GET", path, step="get_order", group="orders")
        return response.json()

    def fetch_order_status_summary(
        self,
        account_number: Optional[str],
        order_id: str,
    ) -> Dict[str, Any]:
        """Fetch broker order and return a safe summary dict."""
        response = self.get_order(account_number, order_id)
        return summarize_order_response(response)

    def list_live_orders(
        self,
        account_number: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """List live sandbox orders via GET /accounts/{account}/orders/live."""
        self._auth.ensure_authenticated()
        acct = self._resolve_account_number(account_number)

        def _load() -> List[Dict[str, Any]]:
            path = f"/accounts/{acct}/orders/live"
            response = self._request("GET", path, step="list_live_orders", group="live_orders")
            payload = response.json().get("data", {})
            items = payload.get("items", []) if isinstance(payload, dict) else []
            return items if isinstance(items, list) else []

        return list(self._cached_read("live_orders", acct, _load, force_refresh))

    def cancel_order(self, account_number: Optional[str], order_id: str) -> Dict[str, Any]:
        """Cancel sandbox order via DELETE /accounts/{account}/orders/{order_id}."""
        self._auth.ensure_authenticated()
        acct = self._resolve_account_number(account_number)
        path = f"/accounts/{acct}/orders/{order_id}"
        self.invalidate_read_cache()
        response = self._request("DELETE", path, step="cancel_order", group="cancel")
        if response.content:
            try:
                return response.json()
            except Exception:
                return {"data": {"cancelled": True, "order_id": order_id}}
        return {"data": {"cancelled": True, "order_id": order_id}}

    def dry_run_equity_close(
        self,
        account_number: Optional[str],
        symbol: str,
        quantity: int,
        order_type: str = "Market",
        time_in_force: str = "Day",
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Preflight close via POST /accounts/{account}/orders/dry-run."""
        self._auth.ensure_authenticated()
        return self._post_equity_close(
            account_number,
            symbol=symbol,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=limit_price,
            dry_run=True,
        )

    def submit_equity_close(
        self,
        account_number: Optional[str],
        symbol: str,
        quantity: int,
        order_type: str = "Market",
        time_in_force: str = "Day",
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Submit close order via POST /accounts/{account}/orders."""
        self._auth.ensure_authenticated()
        return self._post_equity_close(
            account_number,
            symbol=symbol,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=limit_price,
            dry_run=False,
        )

    def execute_order(self, intent: OrderIntent) -> ExecutionResult:
        """Submit sandbox order and return ExecutionResult (for ExecutionRouter)."""
        symbol = intent.symbol.strip().upper()
        side = intent.side.strip().lower()
        mode = intent.trading_mode.strip().lower()
        order_type = (intent.order_type or "Market").strip().capitalize()

        try:
            result = self.submit_equity_order(
                account_number=None,
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                order_type=order_type,
                limit_price=intent.limit_price,
            )
            broker_order_id = extract_broker_order_id(result)
            order_data = result.get("data", {}).get("order") or result.get("data", {})
            order_id = broker_order_id or str(
                order_data.get("id")
                or order_data.get("order-id")
                or result.get("data", {}).get("id")
                or "UNKNOWN"
            )
            broker_status = None
            record_status = "submitted"
            fill_price = None
            limit_price = intent.limit_price if order_type == "Limit" else None
            if broker_order_id:
                try:
                    status_summary = self.fetch_order_status_summary(None, broker_order_id)
                    broker_status = status_summary.get("broker_status")
                    record_status = status_summary.get("status") or record_status
                    limit_price = status_summary.get("limit_price") if limit_price is None else limit_price
                    fill_price = resolve_execution_fill_price(
                        broker_status=broker_status,
                        broker_fill_price=status_summary.get("fill_price"),
                    )
                except SandboxApiError:
                    logger.warning("sandbox get_order status fetch failed for order %s", order_id)
            if broker_status_is_filled(broker_status):
                message = (
                    "Sandbox order filled. Market orders in sandbox may fill at $1 — "
                    "not representative of real market fills."
                )
            elif str(broker_status or "").lower() == "live":
                message = "Sandbox order submitted and is live (unfilled)."
            else:
                message = "Sandbox order submitted."
            return ExecutionResult(
                success=True,
                status=record_status,
                order_id=f"SANDBOX-{order_id}",
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                fill_price=fill_price,
                trading_mode=mode,
                message=message,
                raw={
                    "route": "sandbox",
                    "placed": True,
                    "broker_order_id": broker_order_id,
                    "broker_status": broker_status,
                    "record_status": record_status,
                    "limit_price": limit_price,
                    "order_type": order_type,
                },
            )
        except SandboxApiError as exc:
            status = "rejected" if exc.status_code == 422 else "error"
            message = (
                exc.step_diagnostics.format_safe()
                if exc.step_diagnostics
                else str(exc)
            )
            return ExecutionResult(
                success=False,
                status=status,
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                trading_mode=mode,
                message=message,
                raw={"status_code": exc.status_code, "route": "sandbox", "placed": False},
            )
        except SandboxAuthError as exc:
            message = (
                exc.step_diagnostics.format_safe()
                if exc.step_diagnostics
                else str(exc)
            )
            return ExecutionResult(
                success=False,
                status="error",
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                trading_mode=mode,
                message=message,
                raw={"route": "sandbox", "placed": False},
            )
