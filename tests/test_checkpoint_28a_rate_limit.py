"""Checkpoint 2.8A — sandbox throttling, 429 cooldown, caching, and API call reduction."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from backend.adapters.broker.sandbox_auth import SandboxAuthError, SandboxOAuthClient
from backend.adapters.broker.sandbox_cooldown import (
    RATE_LIMITED_EXIT_CODE,
    format_cooldown_advice,
)
from backend.adapters.broker.sandbox_rate_limiter import (
    DEFAULT_429_COOLDOWN_SECONDS,
    DEFAULT_MIN_INTERVALS,
    ENDPOINT_GROUPS,
    RateLimitCooldownActive,
    RateLimitInfo,
    SandboxRateLimiter,
    intervals_from_env,
    parse_retry_after,
    rate_limit_info_from,
)
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.bot_worker.sandbox_worker import SandboxBotCycleResult, SandboxBotWorker
from backend.config.settings import Settings, reset_settings_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
CANCEL_SCRIPT = REPO_ROOT / "scripts" / "smoke_tastytrade_sandbox_cancel.py"
CYCLE_SCRIPT = REPO_ROOT / "scripts" / "run_sandbox_bot_cycle.py"
OAUTH_CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_tastytrade_oauth.py"
READ_SCRIPT = REPO_ROOT / "scripts" / "smoke_tastytrade_sandbox_read.py"


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(**overrides) -> Settings:
    base = {
        "live_trading_enabled": False,
        "trading_mode": "sandbox",
        "tastytrade_env": "sandbox",
        "emergency_halt": False,
        "tastytrade_client_id": "cid",
        "tastytrade_client_secret": "super-secret-client-secret",
        "tastytrade_refresh_token": "super-secret-refresh-token",
        "tastytrade_oauth_scopes": "read trade openid",
    }
    base.update(overrides)
    return Settings(**base)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _script_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "super-secret-client-secret")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "super-secret-refresh-token")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()


def _response(status: int, payload=None, headers=None, text=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    body = payload if payload is not None else {}
    resp.json.return_value = body
    resp.text = text if text is not None else json.dumps(body)
    resp.content = resp.text.encode("utf-8")
    return resp


def _mock_http(mock_client_cls, responses):
    client = MagicMock()
    client.__enter__.return_value = client
    if isinstance(responses, list):
        client.request.side_effect = responses
    else:
        client.request.return_value = responses
    mock_client_cls.return_value = client
    return client


def _fake_auth():
    auth = MagicMock()
    auth.request_headers.return_value = {
        "Authorization": "Bearer token-value-never-printed",
        "User-Agent": "AI-Trading-Bot/0.1",
    }
    return auth


def _rate_limit_info(step="get_balance", group="balances", cooldown=60.0) -> RateLimitInfo:
    return RateLimitInfo(cooldown_seconds=cooldown, endpoint_group=group, step=step)


# --- rate limiter -------------------------------------------------------------------------


def test_default_intervals_are_conservative():
    for group in ("oauth", "accounts", "balances", "positions", "live_orders"):
        assert DEFAULT_MIN_INTERVALS[group] >= 10.0
    assert DEFAULT_MIN_INTERVALS["orders"] >= 15.0
    assert DEFAULT_MIN_INTERVALS["cancel"] >= 15.0
    assert DEFAULT_429_COOLDOWN_SECONDS >= 60.0


def test_rate_limiter_enforces_minimum_delay_with_single_sleep():
    clock = FakeClock()
    limiter = SandboxRateLimiter(clock=clock, sleep=clock.sleep)

    assert limiter.acquire("accounts") == 0.0
    clock.advance(3.0)
    slept = limiter.acquire("accounts")

    assert slept == pytest.approx(7.0)
    assert clock.sleeps == [pytest.approx(7.0)]


def test_rate_limiter_groups_are_independent():
    clock = FakeClock()
    limiter = SandboxRateLimiter(clock=clock, sleep=clock.sleep)
    limiter.acquire("accounts")
    limiter.acquire("balances")
    limiter.acquire("positions")
    assert clock.sleeps == []


def test_rate_limiter_env_override_floored(monkeypatch):
    intervals = intervals_from_env(
        {
            "TASTYTRADE_SANDBOX_MIN_INTERVAL_ACCOUNTS": "30",
            "TASTYTRADE_SANDBOX_MIN_INTERVAL_ORDERS": "0",
            "TASTYTRADE_SANDBOX_MIN_INTERVAL_CANCEL": "not-a-number",
        }
    )
    assert intervals["accounts"] == 30.0
    assert intervals["orders"] == 1.0
    assert intervals["cancel"] == DEFAULT_MIN_INTERVALS["cancel"]


def test_rate_limiter_state_shared_across_processes(tmp_path):
    state = tmp_path / "rate_state.json"
    clock = FakeClock()
    first = SandboxRateLimiter(clock=clock, sleep=clock.sleep, state_path=state)
    first.acquire("live_orders")

    clock.advance(2.0)
    second = SandboxRateLimiter(clock=clock, sleep=clock.sleep, state_path=state)
    second.acquire("live_orders")
    assert clock.sleeps == [pytest.approx(8.0)]


def test_cooldown_blocks_request_without_sleeping(tmp_path):
    state = tmp_path / "rate_state.json"
    clock = FakeClock()
    limiter = SandboxRateLimiter(clock=clock, sleep=clock.sleep, state_path=state)
    limiter.record_rate_limited("balances", step="get_balance", retry_after="90")

    other_process = SandboxRateLimiter(clock=clock, sleep=clock.sleep, state_path=state)
    with pytest.raises(RateLimitCooldownActive) as exc:
        other_process.acquire("positions", step="get_positions")
    assert exc.value.info.cooldown_already_active is True
    assert exc.value.info.cooldown_seconds == pytest.approx(90.0)
    assert clock.sleeps == []

    clock.advance(91.0)
    other_process.acquire("positions")


def test_state_file_contains_no_secrets(tmp_path):
    state = tmp_path / "rate_state.json"
    limiter = SandboxRateLimiter(state_path=state, sleep=lambda _s: None)
    limiter.acquire("oauth")
    data = json.loads(state.read_text(encoding="utf-8"))
    assert set(data) == {"last_request", "cooldown_until", "updated_at"}
    assert "secret" not in state.read_text(encoding="utf-8").lower()


def test_parse_retry_after_seconds_date_and_default():
    assert parse_retry_after("120") == (120.0, True)
    assert parse_retry_after(None) == (DEFAULT_429_COOLDOWN_SECONDS, False)
    assert parse_retry_after("garbage") == (DEFAULT_429_COOLDOWN_SECONDS, False)
    from email.utils import parsedate_to_datetime

    http_date = "Wed, 21 Oct 2026 07:28:30 GMT"
    reference = parsedate_to_datetime(http_date).timestamp() - 10.0
    seconds, present = parse_retry_after(http_date, now=reference)
    assert present is True
    assert seconds == pytest.approx(10.0)


# --- adapter 429 handling -----------------------------------------------------------------


@patch("backend.adapters.broker.tastytrade_sandbox.time.sleep", return_value=None)
@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_adapter_429_classified_rate_limited_honors_retry_after(mock_client_cls, _sleep):
    client = _mock_http(
        mock_client_cls,
        _response(429, {"error": {"message": "Too Many Requests"}}, headers={"Retry-After": "120"}),
    )
    adapter = TastytradeSandboxAdapter(_settings(), auth=_fake_auth())

    with pytest.raises(SandboxApiError) as exc:
        adapter.get_accounts()

    info = rate_limit_info_from(exc.value)
    assert info is not None
    assert info.failure_reason == "rate_limited"
    assert info.cooldown_seconds == pytest.approx(120.0)
    assert info.retry_after_header_present is True
    text = str(exc.value)
    assert "failure_reason: rate_limited" in text
    assert "next_step: wait before retrying" in text
    assert "cooldown_seconds: 120" in text
    assert client.request.call_count == 1


@patch("backend.adapters.broker.tastytrade_sandbox.time.sleep", return_value=None)
@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_adapter_429_without_retry_after_uses_safe_cooldown(mock_client_cls, _sleep):
    _mock_http(mock_client_cls, _response(429, {"error": {"message": "slow down"}}))
    adapter = TastytradeSandboxAdapter(_settings(), auth=_fake_auth())
    with pytest.raises(SandboxApiError) as exc:
        adapter.get_accounts()
    assert exc.value.rate_limit.cooldown_seconds == pytest.approx(DEFAULT_429_COOLDOWN_SECONDS)
    assert exc.value.rate_limit.retry_after_header_present is False


@patch("backend.adapters.broker.tastytrade_sandbox.time.sleep", return_value=None)
@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_no_retry_loop_after_429_and_followup_calls_blocked(mock_client_cls, sleep_mock):
    client = _mock_http(mock_client_cls, _response(429, {}, headers={"Retry-After": "60"}))
    adapter = TastytradeSandboxAdapter(_settings(), auth=_fake_auth())

    with pytest.raises(SandboxApiError):
        adapter.get_balance("5WM30541")
    with pytest.raises(SandboxApiError) as followup:
        adapter.get_positions("5WM30541")
    with pytest.raises(SandboxApiError):
        adapter.cancel_order("5WM30541", "1225452")

    assert client.request.call_count == 1
    sleep_mock.assert_not_called()
    assert followup.value.rate_limit.cooldown_already_active is True


# --- OAuth token cache and OAuth 429 --------------------------------------------------------


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_access_token_cached_until_near_expiry(mock_client_cls):
    clock = FakeClock()
    token_response = MagicMock()
    token_response.status_code = 200
    token_response.json.return_value = {"access_token": "access-1", "expires_in": 900}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = token_response
    mock_client_cls.return_value = client

    auth = SandboxOAuthClient(_settings(), clock=clock)
    auth.ensure_authenticated()
    auth.ensure_authenticated()
    auth.request_headers()
    auth.request_headers()
    assert client.post.call_count == 1

    clock.advance(900 - 30)
    auth.ensure_authenticated()
    assert client.post.call_count == 2


@patch("backend.adapters.broker.sandbox_auth.httpx.Client")
def test_oauth_429_is_rate_limited_not_credential_failure(mock_client_cls):
    response = MagicMock()
    response.status_code = 429
    response.headers = {"Retry-After": "45"}
    response.text = '{"error":"rate limited"}'
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response
    mock_client_cls.return_value = client

    auth = SandboxOAuthClient(_settings())
    with pytest.raises(SandboxAuthError) as exc:
        auth.ensure_authenticated()
    info = rate_limit_info_from(exc.value)
    assert info is not None and info.cooldown_seconds == pytest.approx(45.0)
    assert exc.value.diagnostics.failure_reason == "rate_limited"
    assert "rate_limited" in str(exc.value)
    assert "super-secret" not in str(exc.value)

    with pytest.raises(SandboxAuthError) as again:
        auth.ensure_authenticated()
    assert rate_limit_info_from(again.value) is not None
    assert client.post.call_count == 1


# --- account / read cache -----------------------------------------------------------------


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_account_cache_prevents_repeat_account_calls(mock_client_cls):
    clock = FakeClock()
    accounts = _response(200, {"data": {"items": [{"account-number": "5WM30541"}]}})
    client = _mock_http(mock_client_cls, accounts)
    adapter = TastytradeSandboxAdapter(_settings(), auth=_fake_auth(), clock=clock)

    adapter.get_accounts()
    adapter.get_accounts()
    assert adapter.get_selected_account() == "5WM30541"
    assert adapter.get_selected_account() == "5WM30541"
    assert client.request.call_count == 1

    clock.advance(61)
    adapter.get_accounts()
    assert client.request.call_count == 2


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_live_orders_cached_briefly_and_invalidated_by_cancel(mock_client_cls):
    clock = FakeClock()
    live = _response(200, {"data": {"items": []}})
    cancel = _response(200, {"data": {"cancelled": True}})
    client = _mock_http(mock_client_cls, [live, live, cancel, live])
    adapter = TastytradeSandboxAdapter(_settings(), auth=_fake_auth(), clock=clock)

    adapter.list_live_orders("5WM30541")
    adapter.list_live_orders("5WM30541")
    assert client.request.call_count == 1

    adapter.cancel_order("5WM30541", "1225452")
    adapter.list_live_orders("5WM30541")
    assert client.request.call_count == 3


# --- worker -------------------------------------------------------------------------------


def _clean_worker_adapter(**overrides):
    adapter = MagicMock()
    adapter._auth = MagicMock()
    adapter.get_accounts.return_value = [{"account-number": "5WM30541"}]
    adapter.get_balance.return_value = {
        "account_number": "5WM30541",
        "buying_power": 0.0,
        "cash_balance": 100000.0,
    }
    adapter.get_positions.return_value = []
    adapter.list_live_orders.return_value = []
    adapter.dry_run_equity_order.return_value = {"data": {}}
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


def test_worker_skips_on_preflight_429():
    adapter = _clean_worker_adapter(
        get_balance=MagicMock(
            side_effect=SandboxApiError(429, "rate limited", rate_limit=_rate_limit_info())
        )
    )
    executor = MagicMock()
    worker = SandboxBotWorker(_settings(), adapter, executor=executor)
    result = worker.run_cycle(signal="bullish", confirm_submit=True)

    assert result.decision_status == "skipped_rate_limited"
    assert result.submitted is False
    assert result.dry_run_passed is False
    assert result.cooldown_seconds == 60
    summary = result.to_safe_summary()
    assert summary["failure_reason"] == "rate_limited"
    assert summary["next_step"] == "wait before retrying"
    adapter.get_positions.assert_not_called()
    adapter.dry_run_equity_order.assert_not_called()
    executor.execute.assert_not_called()


def test_worker_skips_on_oauth_429():
    adapter = _clean_worker_adapter()
    adapter._auth.ensure_authenticated.side_effect = SandboxAuthError(
        "Sandbox OAuth failed (429): rate_limited",
        rate_limit=_rate_limit_info(step="oauth_token", group="oauth"),
    )
    worker = SandboxBotWorker(_settings(), adapter)
    result = worker.run_cycle(signal="bearish")
    assert result.decision_status == "skipped_rate_limited"
    adapter.get_accounts.assert_not_called()


def test_worker_skips_on_dry_run_429_without_submit():
    adapter = _clean_worker_adapter(
        dry_run_equity_order=MagicMock(
            side_effect=SandboxApiError(
                429,
                "rate limited",
                rate_limit=_rate_limit_info(step="dry_run_order", group="orders"),
            )
        )
    )
    executor = MagicMock()
    worker = SandboxBotWorker(_settings(), adapter, executor=executor)
    result = worker.run_cycle(signal="bullish", confirm_submit=True)
    assert result.decision_status == "skipped_rate_limited"
    assert result.submitted is False
    assert result.dry_run_passed is False
    executor.execute.assert_not_called()


def test_worker_preflight_does_not_call_redundant_endpoints():
    adapter = _clean_worker_adapter()
    executor = MagicMock()
    from backend.risk.models import ExecutionResult

    executor.execute.return_value = ExecutionResult(
        success=True,
        status="submitted",
        order_id="SANDBOX-99",
        symbol="TNA",
        side="buy",
        quantity=1,
        trading_mode="sandbox",
        message="ok",
        raw={"broker_order_id": "99", "broker_status": "Live"},
    )
    worker = SandboxBotWorker(_settings(), adapter, executor=executor)
    result = worker.run_cycle(signal="bullish", confirm_submit=True)
    assert result.decision_status == "submitted"
    assert result.broker_status == "Live"
    adapter.get_customers_me.assert_not_called()
    adapter.fetch_order_status_summary.assert_not_called()
    assert adapter.get_accounts.call_count == 1
    assert adapter.list_live_orders.call_count == 1


def test_cycle_script_returns_rate_limited_exit_code(monkeypatch, capsys):
    _script_env(monkeypatch)
    mod = _load_module(CYCLE_SCRIPT, "run_cycle_rate_limited")

    class FakeWorker:
        @classmethod
        def from_settings(cls, settings, with_db=False):
            worker = MagicMock()
            worker.run_cycle.return_value = SandboxBotCycleResult(
                success=False,
                decision_status="skipped_rate_limited",
                signal="bullish",
                cooldown_seconds=75,
            )
            return worker

    monkeypatch.setattr(mod, "SandboxBotWorker", FakeWorker)
    assert mod.main(["--signal", "bullish"]) == RATE_LIMITED_EXIT_CODE
    captured = capsys.readouterr()
    assert "decision_status: skipped_rate_limited" in captured.out
    assert "submitted: False" in captured.out
    assert "dry_run_passed: False" in captured.out
    assert "recommended_wait_seconds: 75" in captured.err
    assert "do NOT retry immediately" in captured.err
    assert "super-secret" not in captured.out + captured.err


# --- cancel script ------------------------------------------------------------------------


class _RecordingCancelAdapter:
    def __init__(self, *, accounts_error=None, cancel_error=None, verify_error=None, live_after=None):
        self.calls: List[str] = []
        self._accounts_error = accounts_error
        self._cancel_error = cancel_error
        self._verify_error = verify_error
        self._live_after = live_after or []
        adapter = self

        class _Auth:
            is_authenticated = True

            def ensure_authenticated(self):
                adapter.calls.append("ensure_authenticated")

        self._auth = _Auth()

    def get_customers_me(self):
        self.calls.append("get_customers_me")
        return {}

    def get_accounts(self):
        self.calls.append("get_accounts")
        if self._accounts_error:
            raise self._accounts_error
        return [{"account-number": "5WM30541"}]

    def get_balance(self, *args, **kwargs):
        self.calls.append("get_balance")
        return {"account_number": "5WM30541"}

    def get_order(self, account_number, order_id):
        self.calls.append("get_order")
        return {"data": {"id": int(order_id), "status": "Live"}}

    def list_live_orders(self, account_number):
        self.calls.append("list_live_orders")
        if self._verify_error and "cancel_order" in self.calls:
            raise self._verify_error
        return list(self._live_after)

    def cancel_order(self, account_number, order_id):
        self.calls.append("cancel_order")
        if self._cancel_error:
            raise self._cancel_error
        return {"data": {"cancelled": True}}


def test_exact_cancel_makes_no_unnecessary_calls(monkeypatch, capsys):
    _script_env(monkeypatch)
    mod = _load_module(CANCEL_SCRIPT, "cancel_exact_budget")
    fake = _RecordingCancelAdapter()
    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: fake)

    assert mod.main(["--order-id", "1225452", "--confirm-sandbox-cancel"]) == 0
    assert fake.calls == [
        "ensure_authenticated",
        "get_accounts",
        "cancel_order",
        "list_live_orders",
    ]
    out = capsys.readouterr().out
    assert "post_cancel_active_live_orders_count: 0" in out
    assert "target_order_still_live: false" in out


def test_exact_cancel_waits_before_verification(monkeypatch, _isolate_sandbox_rate_limiter):
    _script_env(monkeypatch)
    pauses: List[float] = []
    monkeypatch.setattr(_isolate_sandbox_rate_limiter, "pause", lambda seconds: pauses.append(seconds))
    mod = _load_module(CANCEL_SCRIPT, "cancel_verify_delay")
    fake = _RecordingCancelAdapter()
    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: fake)

    assert mod.main(["--order-id", "1225452", "--confirm-sandbox-cancel"]) == 0
    assert pauses == [mod.DEFAULT_VERIFY_DELAY_SECONDS]
    assert mod.DEFAULT_VERIFY_DELAY_SECONDS > 0


def test_cancel_script_stops_safely_on_429_before_cancel(monkeypatch, capsys):
    _script_env(monkeypatch)
    mod = _load_module(CANCEL_SCRIPT, "cancel_429_preflight")
    fake = _RecordingCancelAdapter(
        accounts_error=SandboxApiError(
            429,
            "rate limited",
            rate_limit=_rate_limit_info(step="get_accounts", group="accounts"),
        )
    )
    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: fake)

    code = mod.main(["--order-id", "1225452", "--confirm-sandbox-cancel"])
    assert code == RATE_LIMITED_EXIT_CODE
    assert "cancel_order" not in fake.calls
    assert fake.calls.count("get_accounts") == 1
    err = capsys.readouterr().err
    assert "failure_reason: rate_limited" in err
    assert "do NOT retry immediately" in err
    assert "super-secret" not in err


def test_cancel_verification_429_reports_delayed_not_failed(monkeypatch, capsys):
    _script_env(monkeypatch)
    mod = _load_module(CANCEL_SCRIPT, "cancel_429_verify")
    fake = _RecordingCancelAdapter(
        verify_error=SandboxApiError(
            429,
            "rate limited",
            rate_limit=_rate_limit_info(step="list_live_orders", group="live_orders"),
        )
    )
    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: fake)

    assert mod.main(["--order-id", "1225452", "--confirm-sandbox-cancel"]) == 0
    assert fake.calls.count("cancel_order") == 1
    assert fake.calls.count("list_live_orders") == 1
    out = capsys.readouterr().out
    assert "cancel_attempted: true" in out
    assert "post_cancel_verify: delayed_rate_limited" in out


def test_cancel_preview_does_not_list_before_get_order(monkeypatch):
    _script_env(monkeypatch)
    mod = _load_module(CANCEL_SCRIPT, "cancel_preview_budget")
    fake = _RecordingCancelAdapter()
    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: fake)

    assert mod.main(["--order-id", "1225452"]) == 0
    assert fake.calls == ["ensure_authenticated", "get_accounts", "get_order"]


# --- scripts: API reduction and no secrets ------------------------------------------------


def test_oauth_check_script_reuses_auth_and_skips_balance_call():
    text = OAUTH_CHECK_SCRIPT.read_text(encoding="utf-8")
    assert "TastytradeSandboxAdapter(settings, auth=auth)" in text
    assert "get_balance" not in text


def test_read_script_reports_rate_limit(monkeypatch, capsys):
    _script_env(monkeypatch)
    mod = _load_module(READ_SCRIPT, "read_429")
    fake = _RecordingCancelAdapter(
        accounts_error=SandboxApiError(
            429,
            "rate limited",
            rate_limit=_rate_limit_info(step="get_accounts", group="accounts"),
        )
    )
    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: fake)
    assert mod.main([]) == RATE_LIMITED_EXIT_CODE
    assert "get_balance" not in fake.calls
    err = capsys.readouterr().err
    assert "failure_reason: rate_limited" in err
    assert "recommended_wait_seconds: 60" in err
    assert "super-secret" not in err


def test_cooldown_advice_is_safe_and_actionable():
    advice = format_cooldown_advice(
        _rate_limit_info(cooldown=42.2),
        next_command="py -3.11 scripts/smoke_tastytrade_sandbox_read.py",
    )
    assert "recommended_wait_seconds: 43" in advice
    assert "do NOT retry immediately" in advice
    assert "safe_next_command (after 43s)" in advice
    assert "Bearer" not in advice
    assert "secret" not in advice.lower()


@patch("backend.adapters.broker.tastytrade_sandbox.httpx.Client")
def test_rate_limit_diagnostics_never_print_tokens(mock_client_cls):
    _mock_http(
        mock_client_cls,
        _response(
            429,
            {"error": {"message": "Too Many Requests"}, "access_token": "leaky-access-token-abcdef123456"},
            headers={"Retry-After": "30"},
        ),
    )
    adapter = TastytradeSandboxAdapter(_settings(), auth=_fake_auth())
    with pytest.raises(SandboxApiError) as exc:
        adapter.get_accounts()
    text = str(exc.value)
    assert "leaky-access-token-abcdef123456" not in text
    assert "token-value-never-printed" not in text
    assert "super-secret" not in text


# --- safety gates remain in place ---------------------------------------------------------


def test_live_trading_remains_blocked_for_adapter():
    with pytest.raises(SandboxAuthError):
        TastytradeSandboxAdapter(_settings(live_trading_enabled=True), auth=_fake_auth())


def test_production_remains_blocked_for_adapter_and_oauth():
    with pytest.raises(SandboxAuthError):
        TastytradeSandboxAdapter(_settings(tastytrade_env="production"), auth=_fake_auth())
    with pytest.raises(SandboxAuthError):
        SandboxOAuthClient(_settings(tastytrade_env="production"))


def test_public_trading_routes_remain_blocked(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    reset_settings_cache()
    from backend.app_factory import create_app

    client = create_app(skip_db_init=True, defer_heavy_services=True).test_client()
    assert client.post("/trade/execute", json={"symbol": "TNA"}).status_code == 423
    assert client.post("/trade/close/1", json={"reason": "test"}).status_code == 423
    assert client.post("/bot/start").status_code == 423


def test_rate_limited_scripts_never_submit_orders():
    for script in (CANCEL_SCRIPT, READ_SCRIPT, OAUTH_CHECK_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "submit_equity_order" not in text
        assert "OrderExecutor" not in text


def test_endpoint_groups_cover_required_set():
    required = {"oauth", "accounts", "balances", "positions", "live_orders", "orders", "cancel"}
    assert required.issubset(set(ENDPOINT_GROUPS))
