"""Tastytrade sandbox broker adapter — cert API only, no production routing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from backend.adapters.broker.sandbox_auth import SandboxAuthError, SandboxOAuthClient
from backend.adapters.broker.sandbox_step_diagnostics import (
    StepFailureDiagnostics,
    build_step_failure,
)
from backend.config.settings import Settings
from backend.config.tastytrade_urls import (
    ALLOWED_SANDBOX_SYMBOLS,
    SANDBOX_BASE_URL,
    SANDBOX_MAX_ORDER_QUANTITY,
    USER_AGENT,
    assert_sandbox_base_url,
)
from backend.risk.models import ExecutionResult, OrderIntent

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0


@dataclass
class SandboxApiError(Exception):
    status_code: int
    message: str
    body: Optional[Dict[str, Any]] = None
    step_diagnostics: Optional[StepFailureDiagnostics] = None

    def __str__(self) -> str:
        if self.step_diagnostics:
            return self.step_diagnostics.format_safe()
        return self.message


class TastytradeSandboxAdapter:
    """
    Sandbox-only Tastytrade integration.

    Does not import or call legacy trade_exec.py.
    Base URL is hardcoded to https://api.cert.tastyworks.com.
    """

    def __init__(self, settings: Settings, auth: Optional[SandboxOAuthClient] = None) -> None:
        assert_sandbox_base_url(SANDBOX_BASE_URL)
        if settings.tastytrade_env.strip().lower() != "sandbox":
            raise SandboxAuthError("TastytradeSandboxAdapter requires TASTYTRADE_ENV=sandbox")
        if settings.live_trading_enabled:
            raise SandboxAuthError("LIVE_TRADING_ENABLED must be false for sandbox adapter")
        self._settings = settings
        self._auth = auth or SandboxOAuthClient(settings)
        self._selected_account: Optional[str] = None

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE_URL

    def _request(
        self,
        method: str,
        path: str,
        *,
        step: str,
        json: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        url = f"{SANDBOX_BASE_URL}{path}"
        assert_sandbox_base_url(SANDBOX_BASE_URL)
        headers = self._auth.request_headers()
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.request(method, url, headers=headers, json=json)
            if response.status_code == 401:
                self._auth.refresh_access_token()
                headers = self._auth.request_headers()
                response = client.request(method, url, headers=headers, json=json)
            if response.status_code >= 400:
                raise self._parse_error(
                    response,
                    step=step,
                    path=path,
                    request_headers=headers,
                )
            return response

    def _parse_error(
        self,
        response: httpx.Response,
        *,
        step: str,
        path: str,
        request_headers: Dict[str, str],
        symbol: Optional[str] = None,
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
                "Valid symbols may lag in sandbox instrumentation — retry later."
            )
        elif response.status_code == 429:
            msg = "Sandbox API rate limit (429). Retry after a short delay."
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
        )

    def get_customers_me(self) -> Dict[str, Any]:
        """Fetch authenticated sandbox customer profile (read smoke step)."""
        self._auth.ensure_authenticated()
        response = self._request("GET", "/customers/me", step="get_customers_me")
        return response.json().get("data", {})

    def get_accounts(self) -> List[Dict[str, Any]]:
        self._auth.ensure_authenticated()
        response = self._request("GET", "/customers/me/accounts", step="get_accounts")
        data = response.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            accounts_resp = self._request("GET", "/accounts", step="get_accounts")
            items = accounts_resp.json().get("data", {}).get("items", [])
        return items

    def _resolve_account_number(self, account_number: Optional[str] = None) -> str:
        if account_number:
            return account_number
        if self._selected_account:
            return self._selected_account
        accounts = self.get_accounts()
        if not accounts:
            raise SandboxApiError(
                404,
                "No sandbox accounts found",
                step_diagnostics=StepFailureDiagnostics(
                    step="get_accounts",
                    status_code=404,
                    endpoint_path="/customers/me/accounts",
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
        return self._selected_account

    def get_balance(self, account_number: Optional[str] = None) -> Dict[str, Any]:
        acct = self._resolve_account_number(account_number)
        path = f"/accounts/{acct}/balances"
        response = self._request("GET", path, step="get_balance")
        payload = response.json().get("data", {})
        return {
            "account_number": acct,
            "cash_balance": payload.get("cash-balance"),
            "buying_power": payload.get("day-trading-buying-power") or payload.get("equity-buying-power"),
            "day_pnl": payload.get("day-pnl"),
        }

    def get_positions(self, account_number: Optional[str] = None) -> List[Dict[str, Any]]:
        acct = self._resolve_account_number(account_number)
        path = f"/accounts/{acct}/positions"
        response = self._request("GET", path, step="get_positions")
        return response.json().get("data", {}).get("items", [])

    def submit_equity_order(
        self,
        account_number: Optional[str],
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "Market",
        time_in_force: str = "Day",
    ) -> Dict[str, Any]:
        symbol = symbol.strip().upper()
        side = side.strip().lower()
        if symbol not in ALLOWED_SANDBOX_SYMBOLS:
            raise SandboxApiError(400, f"Symbol {symbol!r} not allowed in Phase 2 sandbox adapter")
        if side != "buy":
            raise SandboxApiError(400, "Only buy orders are allowed in Phase 2 sandbox adapter")
        if quantity <= 0 or quantity > SANDBOX_MAX_ORDER_QUANTITY:
            raise SandboxApiError(
                400,
                f"Sandbox quantity must be 1..{SANDBOX_MAX_ORDER_QUANTITY}, got {quantity}",
            )

        acct = self._resolve_account_number(account_number)
        order_data = {
            "time-in-force": time_in_force,
            "order-type": order_type,
            "price": None,
            "price-effect": "Debit",
            "legs": [
                {
                    "instrument-type": "Equity",
                    "symbol": symbol,
                    "quantity": quantity,
                    "action": "Buy to Open",
                }
            ],
        }
        path = f"/accounts/{acct}/orders"
        response = self._request("POST", path, step="submit_order", json=order_data)
        return response.json()

    def get_order(self, account_number: Optional[str], order_id: str) -> Dict[str, Any]:
        acct = self._resolve_account_number(account_number)
        path = f"/accounts/{acct}/orders/{order_id}"
        response = self._request("GET", path, step="get_order")
        return response.json()

    def execute_order(self, intent: OrderIntent) -> ExecutionResult:
        """Submit sandbox order and return ExecutionResult (for ExecutionRouter)."""
        symbol = intent.symbol.strip().upper()
        side = intent.side.strip().lower()
        mode = intent.trading_mode.strip().lower()

        try:
            result = self.submit_equity_order(
                account_number=None,
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                order_type=intent.order_type or "Market",
            )
            order_data = result.get("data", {}).get("order") or result.get("data", {})
            order_id = str(
                order_data.get("id")
                or order_data.get("order-id")
                or result.get("data", {}).get("id")
                or "SANDBOX-UNKNOWN"
            )
            fill_price = intent.current_price
            return ExecutionResult(
                success=True,
                status="filled",
                order_id=f"SANDBOX-{order_id}",
                symbol=symbol,
                side=side,
                quantity=intent.quantity,
                fill_price=fill_price,
                trading_mode=mode,
                message=(
                    "Sandbox order submitted. Market orders in sandbox may fill at $1 — "
                    "not representative of real market fills."
                ),
                raw={"route": "sandbox", "placed": True, "broker_order_id": order_id},
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
