"""Checkpoint 2.9 — production market data (read-only) with sandbox-only execution."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import backend.market_data.tastytrade_market_data as md
from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.sandbox_rate_limiter import SandboxRateLimiter
from backend.adapters.broker.tastytrade_sandbox import TastytradeSandboxAdapter
from backend.bot_worker.sandbox_worker import SandboxBotWorker
from backend.config.settings import ConfigurationError, Settings, load_settings
from backend.config.tastytrade_urls import (
    MARKET_DATA_BASE_URL,
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    BrokerUrlBlockedError,
    MarketDataCredentialMisuseError,
    MarketDataUrlBlockedError,
    assert_market_data_request,
    assert_sandbox_base_url,
    resolve_broker_base_url,
)
from backend.execution.execution_router import ExecutionRouter
from backend.market_data.config import (
    MarketDataConfig,
    MarketDataConfigError,
    safe_fingerprint,
    validate_execution_still_sandbox,
    validate_market_data_settings,
)
from backend.market_data.models import (
    QuoteSnapshot,
    evaluate_quote_freshness,
    is_quote_usable_for_trading,
    normalize_tastytrade_quote,
    parse_timestamp,
)
from backend.market_data.tastytrade_market_data import (
    MARKET_DATA_OAUTH_GROUP,
    MARKET_DATA_QUOTES_GROUP,
    MarketDataApiError,
    MarketDataAuthError,
    MarketDataOAuthClient,
    TastytradeMarketDataClient,
    build_market_data_rate_limiter_from_env,
)
from backend.risk.live_guard import LiveTradingBlockedError, assert_order_execution_allowed
from backend.risk.models import OrderIntent, OrderIntentValidationError, RiskContext
from scripts import check_tastytrade_market_data as script

_NO_ENV = Path("/nonexistent/.env")

MD_CLIENT_ID = "mdcid-1234567890-abcdef"
MD_SECRET = "mdsecret-ZZZZ-9876543210-qwerty"
MD_REFRESH = "mdrefresh-AAAA-1111222233334444-token"
MD_ACCESS = "mdaccess-BBBB-5555666677778888-jwt"
SECRETS = (MD_CLIENT_ID, MD_SECRET, MD_REFRESH, MD_ACCESS)

NOW = datetime(2026, 9, 28, 15, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    base = dict(
        trading_mode="sandbox",
        tastytrade_env="sandbox",
        live_trading_enabled=False,
        tastytrade_client_id="sandbox-cid-000000000000",
        tastytrade_client_secret="sandbox-secret-00000000000",
        tastytrade_refresh_token="sandbox-refresh-000000000000",
        market_data_provider="tastytrade",
        market_data_env="production",
        market_data_read_only=True,
        tastytrade_market_data_client_id=MD_CLIENT_ID,
        tastytrade_market_data_client_secret=MD_SECRET,
        tastytrade_market_data_refresh_token=MD_REFRESH,
        tastytrade_market_data_scopes="read",
    )
    base.update(overrides)
    return Settings(**base)


def _config(**overrides) -> MarketDataConfig:
    return validate_market_data_settings(_settings(**overrides))


def _quote_item(symbol: str, *, updated_at: str = "2026-09-28T14:59:55.000Z", **extra) -> dict:
    item = {
        "symbol": symbol,
        "instrument-type": "Equity",
        "updated-at": updated_at,
        "bid": "10.10",
        "ask": "10.20",
        "mid": "10.15",
        "mark": "10.15",
        "last": "10.12",
        "open": "9.90",
        "day-high-price": "10.50",
        "day-low-price": "9.80",
        "prev-close": "9.95",
        "volume": "123456",
        "is-trading-halted": False,
    }
    item.update(extra)
    return item


class FakeTastytrade:
    """httpx MockTransport handler recording every production request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.oauth_responses: list[httpx.Response] = []
        self.equity_responses: list[httpx.Response] = []
        self.index_responses: list[httpx.Response] = []

    def token(self, **extra) -> httpx.Response:
        body = {"access_token": MD_ACCESS, "expires_in": 900, "token_type": "Bearer"}
        body.update(extra)
        return httpx.Response(200, json=body)

    @staticmethod
    def quotes(items: list[dict]) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": items}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth/token":
            return self.oauth_responses.pop(0) if self.oauth_responses else self.token()
        if path == "/market-data/by-type":
            if "index" in request.url.params:
                if self.index_responses:
                    return self.index_responses.pop(0)
                return self.quotes([_quote_item("VIX", **{"instrument-type": "Index"})])
            if self.equity_responses:
                return self.equity_responses.pop(0)
            symbols = request.url.params["equity"].split(",")
            return self.quotes([_quote_item(s) for s in symbols])
        return httpx.Response(599, json={"error": "unexpected path in test"})


@pytest.fixture
def fake_tt(monkeypatch):
    fake = FakeTastytrade()
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(md.httpx, "Client", client_factory)
    return fake


def _client(config: MarketDataConfig | None = None, **kwargs) -> TastytradeMarketDataClient:
    config = config or _config()
    persisted: list = kwargs.pop("persisted", [])
    auth = MarketDataOAuthClient(
        config,
        refresh_token_persister=lambda new, prev: persisted.append((new, prev)) or True,
    )
    return TastytradeMarketDataClient(config, auth=auth, sleep=lambda _s: None, **kwargs)


# ---------------------------------------------------------------------------
# Task 1 — config validation / read-only enforcement
# ---------------------------------------------------------------------------


def test_settings_market_data_defaults_are_read_only_production():
    settings = Settings()
    assert settings.market_data_provider == "tastytrade"
    assert settings.market_data_env == "production"
    assert settings.market_data_read_only is True
    assert settings.tastytrade_market_data_scopes == "read"


def test_load_settings_reads_market_data_env(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "Tastytrade")
    monkeypatch.setenv("MARKET_DATA_ENV", "PRODUCTION")
    monkeypatch.setenv("MARKET_DATA_READ_ONLY", "true")
    monkeypatch.setenv("TASTYTRADE_MARKET_DATA_CLIENT_ID", MD_CLIENT_ID)
    monkeypatch.setenv("TASTYTRADE_MARKET_DATA_SCOPES", "read")
    monkeypatch.setenv("MARKET_DATA_MAX_QUOTE_AGE_SECONDS", "45")
    settings = load_settings(env_path=_NO_ENV)
    assert settings.market_data_provider == "tastytrade"
    assert settings.market_data_env == "production"
    assert settings.tastytrade_market_data_client_id == MD_CLIENT_ID
    assert settings.market_data_max_quote_age_seconds == 45.0


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_market_data_read_only_false_fails_closed(monkeypatch, value):
    monkeypatch.setenv("MARKET_DATA_READ_ONLY", value)
    with pytest.raises(ConfigurationError, match="MARKET_DATA_READ_ONLY"):
        load_settings(env_path=_NO_ENV)


def test_market_data_read_only_invalid_value_fails_closed(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_READ_ONLY", "maybe")
    with pytest.raises(ConfigurationError):
        load_settings(env_path=_NO_ENV)


def test_settings_validate_rejects_read_only_false():
    with pytest.raises(ConfigurationError, match="MARKET_DATA_READ_ONLY"):
        Settings(market_data_read_only=False).validate()


def test_valid_market_data_config():
    config = _config()
    assert config.read_only is True
    assert config.env == "production"
    assert config.base_url == PRODUCTION_BASE_URL == MARKET_DATA_BASE_URL
    assert config.scopes == "read"


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"tastytrade_market_data_client_id": ""}, "TASTYTRADE_MARKET_DATA_CLIENT_ID is not set"),
        ({"tastytrade_market_data_client_secret": ""}, "CLIENT_SECRET is not set"),
        ({"tastytrade_market_data_refresh_token": ""}, "REFRESH_TOKEN is not set"),
        ({"market_data_env": "sandbox"}, "MARKET_DATA_ENV"),
        ({"market_data_provider": "alphavantage"}, "MARKET_DATA_PROVIDER"),
        ({"tastytrade_market_data_scopes": "read trade"}, "non-read-only"),
        ({"tastytrade_market_data_scopes": "trade"}, "non-read-only"),
        ({"tastytrade_market_data_scopes": "openid"}, "must include 'read'"),
        ({"market_data_max_quote_age_seconds": 0}, "MAX_QUOTE_AGE"),
    ],
)
def test_invalid_market_data_config_fails_closed(overrides, fragment):
    with pytest.raises(MarketDataConfigError) as info:
        _config(**overrides)
    assert any(fragment in problem for problem in info.value.problems)


@pytest.mark.parametrize(
    "field, sandbox_field",
    [
        ("tastytrade_market_data_client_id", "tastytrade_client_id"),
        ("tastytrade_market_data_client_secret", "tastytrade_client_secret"),
        ("tastytrade_market_data_refresh_token", "tastytrade_refresh_token"),
    ],
)
def test_market_data_cannot_reuse_sandbox_execution_credentials(field, sandbox_field):
    shared = "shared-credential-value-1234567890"
    with pytest.raises(MarketDataConfigError, match="must not reuse the sandbox"):
        _config(**{field: shared, sandbox_field: shared})


def test_market_data_config_read_only_false_rejected():
    with pytest.raises(MarketDataConfigError, match="MARKET_DATA_READ_ONLY"):
        validate_market_data_settings(_settings(market_data_read_only=False))


@pytest.mark.parametrize(
    "overrides",
    [
        {"live_trading_enabled": True},
        {"trading_mode": "live"},
        {"tastytrade_env": "production"},
    ],
)
def test_execution_must_remain_sandbox(overrides):
    with pytest.raises(MarketDataConfigError, match="sandbox"):
        validate_execution_still_sandbox(_settings(**overrides))
    with pytest.raises(MarketDataConfigError):
        validate_market_data_settings(_settings(**overrides))


def test_config_and_settings_repr_hide_market_data_secrets():
    config = _config()
    settings = _settings()
    for text in (repr(config), repr(settings), str(config.safe_summary()), str(settings.safe_summary())):
        for secret in (MD_CLIENT_ID, MD_SECRET, MD_REFRESH):
            assert secret not in text


def test_safe_fingerprint_never_returns_full_value():
    fp = safe_fingerprint(MD_SECRET)
    assert MD_SECRET not in fp
    assert fp.startswith(MD_SECRET[:4])
    assert "sha256:" in fp
    assert safe_fingerprint("") == "(not set)"


# ---------------------------------------------------------------------------
# Production allowed only for quotes
# ---------------------------------------------------------------------------


def test_production_allowed_only_for_quote_and_oauth_endpoints():
    assert_market_data_request("GET", f"{PRODUCTION_BASE_URL}/market-data/by-type")
    assert_market_data_request("POST", f"{PRODUCTION_BASE_URL}/oauth/token")


@pytest.mark.parametrize(
    "method, url",
    [
        ("POST", f"{PRODUCTION_BASE_URL}/accounts/5WX00000/orders"),
        ("POST", f"{PRODUCTION_BASE_URL}/accounts/5WX00000/orders/dry-run"),
        ("DELETE", f"{PRODUCTION_BASE_URL}/accounts/5WX00000/orders/1"),
        ("GET", f"{PRODUCTION_BASE_URL}/accounts/5WX00000/orders/live"),
        ("GET", f"{PRODUCTION_BASE_URL}/accounts/5WX00000/positions"),
        ("GET", f"{PRODUCTION_BASE_URL}/accounts/5WX00000/balances"),
        ("GET", f"{PRODUCTION_BASE_URL}/customers/me/accounts"),
        ("POST", f"{PRODUCTION_BASE_URL}/market-data/by-type"),
        ("GET", f"{PRODUCTION_BASE_URL}/oauth/token"),
        ("GET", f"{SANDBOX_BASE_URL}/market-data/by-type"),
        ("GET", "https://api.tastytrade.com/market-data/by-type"),
        ("GET", "http://api.tastyworks.com/market-data/by-type"),
    ],
)
def test_production_order_and_account_paths_blocked(method, url):
    with pytest.raises(MarketDataUrlBlockedError):
        assert_market_data_request(method, url)


def test_broker_url_resolution_still_blocks_production():
    with pytest.raises(BrokerUrlBlockedError):
        resolve_broker_base_url("production")
    with pytest.raises(BrokerUrlBlockedError):
        assert_sandbox_base_url(PRODUCTION_BASE_URL)


# ---------------------------------------------------------------------------
# Task 2 — normalized quote model
# ---------------------------------------------------------------------------


def test_normalize_dasherized_quote():
    quote = normalize_tastytrade_quote(_quote_item("tna"))
    assert quote.symbol == "TNA"
    assert quote.bid == Decimal("10.10")
    assert quote.ask == Decimal("10.20")
    assert quote.mid == Decimal("10.15")
    assert quote.mark == Decimal("10.15")
    assert quote.last == Decimal("10.12")
    assert quote.open == Decimal("9.90")
    assert quote.high == Decimal("10.50")
    assert quote.low == Decimal("9.80")
    assert quote.previous_close == Decimal("9.95")
    assert quote.volume == Decimal("123456")
    assert quote.updated_at == datetime(2026, 9, 28, 14, 59, 55, tzinfo=timezone.utc)
    assert quote.source == "tastytrade_production_rest"
    assert quote.is_realtime is True
    assert quote.is_trading_halted is False


def test_normalize_computes_mid_and_supports_camel_case_and_epoch_ms():
    stamp_ms = int(NOW.timestamp() * 1000)
    quote = normalize_tastytrade_quote(
        {
            "symbol": "SPY",
            "bid": "500.00",
            "ask": "500.10",
            "dayHighPrice": "505",
            "dayLowPrice": "495",
            "prevClose": "499",
            "updatedAt": stamp_ms,
        }
    )
    assert quote.mid == Decimal("500.05")
    assert quote.high == Decimal("505")
    assert quote.low == Decimal("495")
    assert quote.previous_close == Decimal("499")
    assert quote.updated_at == NOW


def test_normalize_handles_missing_and_bad_values():
    quote = normalize_tastytrade_quote({"symbol": "QQQ", "bid": "NaN", "ask": "", "volume": "x"})
    assert quote.bid is None and quote.ask is None and quote.mid is None
    assert quote.volume is None
    assert quote.updated_at is None
    with pytest.raises(ValueError):
        normalize_tastytrade_quote({"bid": "1"})


def test_parse_timestamp_variants():
    assert parse_timestamp("2026-09-28T15:00:00Z") == NOW
    assert parse_timestamp("2026-09-28T15:00:00+00:00") == NOW
    assert parse_timestamp(NOW.timestamp()) == NOW
    assert parse_timestamp("garbage") is None
    assert parse_timestamp(None) is None


def test_quote_to_safe_dict_includes_age():
    quote = normalize_tastytrade_quote(_quote_item("IWM", updated_at="2026-09-28T14:59:50Z"))
    data = quote.to_safe_dict(NOW)
    assert data["quote_age_seconds"] == 10.0
    assert data["bid"] == "10.10"
    assert data["previous_close"] == "9.95"


# ---------------------------------------------------------------------------
# Task 5 — freshness (diagnostic only)
# ---------------------------------------------------------------------------


def test_fresh_quote_not_stale():
    quote = normalize_tastytrade_quote(_quote_item("TNA", updated_at="2026-09-28T14:59:58Z"))
    freshness = evaluate_quote_freshness(quote, now=NOW, max_age_seconds=30)
    assert freshness.is_stale is False
    assert freshness.age_seconds == 2.0
    assert freshness.warning is None
    assert is_quote_usable_for_trading(quote, freshness) == (True, "ok")


def test_stale_quote_warns_and_is_not_usable():
    quote = normalize_tastytrade_quote(_quote_item("TZA", updated_at="2026-09-28T14:50:00Z"))
    freshness = evaluate_quote_freshness(quote, now=NOW, max_age_seconds=60)
    assert freshness.is_stale is True
    assert "stale" in freshness.warning
    assert "do not trade" in freshness.warning
    assert is_quote_usable_for_trading(quote, freshness) == (False, "stale_quote")


def test_missing_timestamp_is_treated_as_stale():
    quote = QuoteSnapshot(symbol="SPY", bid=Decimal("1"), ask=Decimal("2"))
    freshness = evaluate_quote_freshness(quote, now=NOW)
    assert freshness.is_stale is True
    assert freshness.has_timestamp is False
    assert "no updated_at" in freshness.warning


def test_future_timestamp_warns_about_clock():
    quote = QuoteSnapshot(symbol="SPY", updated_at=NOW + timedelta(seconds=30))
    freshness = evaluate_quote_freshness(quote, now=NOW)
    assert freshness.is_stale is False
    assert "clock" in freshness.warning


@pytest.mark.parametrize(
    "quote, reason",
    [
        (QuoteSnapshot(symbol="TNA", bid=Decimal("1"), ask=Decimal("2"), updated_at=NOW, is_trading_halted=True), "trading_halted"),
        (QuoteSnapshot(symbol="TNA", bid=None, ask=Decimal("2"), updated_at=NOW), "missing_bid_ask"),
        (QuoteSnapshot(symbol="TNA", bid=Decimal("3"), ask=Decimal("2"), updated_at=NOW), "crossed_market"),
    ],
)
def test_unusable_quote_reasons(quote, reason):
    freshness = evaluate_quote_freshness(quote, now=NOW)
    assert is_quote_usable_for_trading(quote, freshness) == (False, reason)


# ---------------------------------------------------------------------------
# Task 2 — adapter behavior (mocked production HTTP)
# ---------------------------------------------------------------------------


def test_get_quotes_fetches_default_symbols_and_vix(fake_tt):
    client = _client()
    result = client.get_quotes(indices=("VIX",))
    assert sorted(result.quotes) == ["IWM", "QQQ", "SPY", "TNA", "TZA", "VIX"]
    assert result.missing == []
    assert result.unsupported == {}

    hosts = {req.url.host for req in fake_tt.requests}
    assert hosts == {"api.tastyworks.com"}
    methods_paths = [(req.method, req.url.path) for req in fake_tt.requests]
    assert methods_paths == [
        ("POST", "/oauth/token"),
        ("GET", "/market-data/by-type"),
        ("GET", "/market-data/by-type"),
    ]
    oauth_body = json.loads(fake_tt.requests[0].content)
    assert oauth_body["grant_type"] == "refresh_token"
    assert oauth_body["scope"] == "read"
    assert "trade" not in oauth_body["scope"]
    assert fake_tt.requests[1].url.params["equity"] == "TNA,TZA,IWM,SPY,QQQ"
    assert fake_tt.requests[2].url.params["index"] == "VIX"
    assert fake_tt.requests[1].headers["Authorization"] == f"Bearer {MD_ACCESS}"


def test_access_token_reused_across_calls(fake_tt):
    client = _client()
    client.get_quotes(("TNA",))
    client.get_quotes(("TZA",))
    assert client.auth.token_request_count == 1


def test_vix_unsupported_is_skipped_with_message(fake_tt):
    fake_tt.index_responses.append(httpx.Response(400, json={"error": {"message": "unsupported"}}))
    result = _client().get_quotes(indices=("VIX",))
    assert "VIX" not in result.quotes
    assert "VIX" in result.unsupported
    assert "skipped" in result.unsupported["VIX"]
    assert len(result.quotes) == 5


def test_vix_not_returned_is_unsupported(fake_tt):
    fake_tt.index_responses.append(FakeTastytrade.quotes([]))
    result = _client().get_quotes(indices=("VIX",))
    assert "not returned" in result.unsupported["VIX"]


def test_missing_equity_symbol_is_reported(fake_tt):
    fake_tt.equity_responses.append(FakeTastytrade.quotes([_quote_item("TNA")]))
    result = _client().get_quotes(("TNA", "TZA"))
    assert list(result.quotes) == ["TNA"]
    assert result.missing == ["TZA"]


def test_quote_403_classified_as_not_permitted(fake_tt):
    fake_tt.equity_responses.append(
        httpx.Response(403, json={"error": {"code": "forbidden", "message": "not funded"}})
    )
    with pytest.raises(MarketDataApiError) as info:
        _client().get_quotes(("TNA",))
    assert info.value.reason == "market_data_not_permitted"
    assert "funded" in info.value.next_step


def test_quote_401_refreshes_token_once(fake_tt):
    fake_tt.equity_responses.append(httpx.Response(401, json={"error": "expired"}))
    client = _client()
    result = client.get_quotes(("TNA",))
    assert "TNA" in result.quotes
    assert client.auth.token_request_count == 2


def test_quote_502_retried_then_succeeds(fake_tt):
    fake_tt.equity_responses.append(httpx.Response(502, text="bad gateway"))
    result = _client().get_quotes(("TNA",))
    assert "TNA" in result.quotes


def test_oauth_invalid_grant_classified_and_not_retried(fake_tt):
    fake_tt.oauth_responses.append(
        httpx.Response(401, json={"error": "invalid_grant", "error_description": "Grant revoked"})
    )
    client = _client()
    with pytest.raises(MarketDataAuthError) as info:
        client.get_quotes(("TNA",))
    assert info.value.reason == "invalid_refresh_token"
    assert "TASTYTRADE_MARKET_DATA_REFRESH_TOKEN" in info.value.next_step
    with pytest.raises(MarketDataAuthError) as again:
        client.get_quotes(("TNA",))
    assert again.value.reason == "oauth_previously_failed"
    assert sum(1 for r in fake_tt.requests if r.url.path == "/oauth/token") == 1


def test_oauth_invalid_client_is_secret_mismatch(fake_tt):
    fake_tt.oauth_responses.append(httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(MarketDataAuthError) as info:
        _client().get_quotes(("TNA",))
    assert info.value.reason == "secret_token_mismatch"


def test_rotated_refresh_token_is_persisted_without_env_write(fake_tt):
    rotated = "mdrefresh-ROTATED-9999888877776666"
    fake_tt.oauth_responses.append(fake_tt.token(refresh_token=rotated))
    persisted: list = []
    client = _client(persisted=persisted)
    client.get_quotes(("TNA",))
    assert persisted == [(rotated, MD_REFRESH)]


def test_clients_refuse_non_read_only_config():
    config = MarketDataConfig(
        provider="tastytrade",
        env="production",
        read_only=False,
        scopes="read",
        max_quote_age_seconds=60,
        client_id=MD_CLIENT_ID,
        client_secret=MD_SECRET,
        refresh_token=MD_REFRESH,
    )
    with pytest.raises(MarketDataAuthError):
        MarketDataOAuthClient(config)
    with pytest.raises(MarketDataApiError):
        TastytradeMarketDataClient(config, auth=MagicMock())


# ---------------------------------------------------------------------------
# Rate limiting for market data
# ---------------------------------------------------------------------------


def test_quote_429_starts_cooldown_and_blocks_next_call(fake_tt):
    fake_tt.equity_responses.append(httpx.Response(429, headers={"Retry-After": "42"}, json={}))
    limiter = SandboxRateLimiter(
        min_intervals={MARKET_DATA_OAUTH_GROUP: 0.0, MARKET_DATA_QUOTES_GROUP: 0.0},
        state_path=None,
        sleep=lambda _s: None,
    )
    client = _client(rate_limiter=limiter)
    with pytest.raises(MarketDataApiError) as info:
        client.get_quotes(("TNA",))
    assert info.value.reason == "rate_limited"
    assert info.value.rate_limit.cooldown_seconds == 42
    assert info.value.rate_limit.endpoint_group == MARKET_DATA_QUOTES_GROUP
    sent = len(fake_tt.requests)

    with pytest.raises(MarketDataApiError) as again:
        client.get_quotes(("TNA",))
    assert again.value.reason == "rate_limited"
    assert again.value.rate_limit.cooldown_already_active is True
    assert len(fake_tt.requests) == sent


def test_oauth_429_is_rate_limited_not_credential_failure(fake_tt, _isolate_market_data_rate_limiter):
    fake_tt.oauth_responses.append(httpx.Response(429, json={}))
    with pytest.raises(MarketDataAuthError) as info:
        _client().get_quotes(("TNA",))
    assert info.value.reason == "rate_limited"
    assert info.value.rate_limit is not None
    assert _isolate_market_data_rate_limiter.cooldown_remaining() > 0


def test_market_data_limiter_is_separate_from_sandbox(
    fake_tt, _isolate_sandbox_rate_limiter, _isolate_market_data_rate_limiter
):
    _client().get_quotes(("TNA",))
    assert _isolate_sandbox_rate_limiter.cooldown_remaining() == 0
    assert not _isolate_sandbox_rate_limiter._last_request
    assert MARKET_DATA_QUOTES_GROUP in _isolate_market_data_rate_limiter._last_request


def test_market_data_limiter_env_overrides_and_state_roundtrip(tmp_path):
    state = tmp_path / "md_state.json"
    env = {
        "MARKET_DATA_MIN_INTERVAL_QUOTES": "7",
        "MARKET_DATA_MIN_INTERVAL_OAUTH": "0.1",
        "MARKET_DATA_429_COOLDOWN_SECONDS": "90",
        "MARKET_DATA_RATE_STATE_PATH": str(state),
    }
    limiter = build_market_data_rate_limiter_from_env(env)
    assert limiter.interval_for(MARKET_DATA_QUOTES_GROUP) == 7.0
    assert limiter.interval_for(MARKET_DATA_OAUTH_GROUP) == 1.0
    assert limiter.default_cooldown_seconds == 90.0
    limiter.acquire(MARKET_DATA_QUOTES_GROUP)
    reloaded = build_market_data_rate_limiter_from_env(env)
    assert MARKET_DATA_QUOTES_GROUP in reloaded._last_request

    disabled = build_market_data_rate_limiter_from_env({"MARKET_DATA_RATE_STATE_PATH": "none"})
    assert disabled.state_path is None
    default = build_market_data_rate_limiter_from_env({})
    assert default.state_path.name == ".market_data_rate_state.json"


# ---------------------------------------------------------------------------
# Task 4 — safety guards
# ---------------------------------------------------------------------------

_FORBIDDEN_METHOD_WORDS = ("order", "submit", "cancel", "execute", "trade", "position", "account", "balance", "close")


@pytest.mark.parametrize("cls", [TastytradeMarketDataClient, MarketDataOAuthClient])
def test_market_data_classes_have_no_order_methods(cls):
    public = [name for name in dir(cls) if not name.startswith("_")]
    offenders = [n for n in public if any(word in n.lower() for word in _FORBIDDEN_METHOD_WORDS)]
    assert offenders == []
    assert cls.IS_MARKET_DATA_ONLY is True


def test_market_data_package_never_imports_execution_code():
    root = Path(md.__file__).resolve().parent
    forbidden = (
        "backend.execution",
        "execution_router",
        "order_executor",
        "tastytrade_sandbox",
        "sandbox_auth",
        "sandbox_worker",
        "bot_worker",
        "trade_exec",
    )
    for path in root.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert not any(token in stripped for token in forbidden), f"{path.name}: {stripped}"


def test_market_data_source_has_no_order_payloads():
    source = inspect.getsource(md)
    for token in ('"/orders', "'/orders", "order-type", "time-in-force", "dry-run", "/positions", "/balances"):
        assert token not in source


def test_execution_router_rejects_market_data_client():
    with pytest.raises(MarketDataCredentialMisuseError):
        ExecutionRouter(sandbox_adapter=_client())


def test_execution_router_rejects_production_base_url_adapter():
    adapter = MagicMock()
    adapter.base_url = PRODUCTION_BASE_URL
    with pytest.raises(MarketDataCredentialMisuseError):
        ExecutionRouter(sandbox_adapter=adapter)


def _sandbox_intent_and_context(**ctx):
    intent = OrderIntent(symbol="TNA", side="buy", quantity=1, trading_mode="sandbox")
    base = dict(
        trading_mode="sandbox",
        live_trading_enabled=False,
        tastytrade_env="sandbox",
        emergency_halt=False,
        buying_power=10000.0,
        current_price=50.0,
        open_positions_count=0,
        pending_orders_count=0,
        trades_today_count=0,
        daily_pnl=0.0,
        market_data_healthy=True,
        max_trades_per_day=5,
        max_daily_loss_usd=500.0,
        buying_power_reserve_pct=0.1,
        max_position_pct_of_buying_power=0.5,
    )
    base.update(ctx)
    return intent, RiskContext(**base)


def test_execution_router_route_time_guard_blocks_market_data_adapter():
    router = ExecutionRouter(sandbox_adapter=MagicMock())
    market_client = _client()
    router._sandbox = market_client
    intent, context = _sandbox_intent_and_context()
    result = router.route(intent, context)
    assert result.success is False
    assert result.status == "rejected"
    assert "market-data" in result.message


def test_execution_router_still_blocks_live():
    adapter = MagicMock()
    router = ExecutionRouter(sandbox_adapter=adapter)
    intent, context = _sandbox_intent_and_context(live_trading_enabled=True)
    result = router.route(intent, context)
    assert result.success is False
    assert result.status == "rejected"
    adapter.execute_order.assert_not_called()

    with pytest.raises(OrderIntentValidationError):
        OrderIntent(symbol="TNA", side="buy", quantity=1, trading_mode="live")
    _, live_context = _sandbox_intent_and_context(trading_mode="live")
    live_result = router.route(intent, live_context)
    assert live_result.success is False
    adapter.execute_order.assert_not_called()


def test_live_guard_blocks_live_trading_enabled():
    with pytest.raises(LiveTradingBlockedError):
        assert_order_execution_allowed(Settings(live_trading_enabled=True))


def test_live_trading_enabled_true_still_fails(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ConfigurationError):
        load_settings(env_path=_NO_ENV)


def test_trading_mode_live_still_fails(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ConfigurationError):
        load_settings(env_path=_NO_ENV)


def test_sandbox_adapter_rejects_market_data_auth():
    auth = MarketDataOAuthClient(_config())
    with pytest.raises(SandboxAuthError, match="market-data"):
        TastytradeSandboxAdapter(_settings(), auth=auth)


def test_sandbox_worker_rejects_market_data_client():
    with pytest.raises(ConfigurationError, match="market-data"):
        SandboxBotWorker(_settings(), _client())


def test_sandbox_worker_does_not_submit_by_default():
    adapter = MagicMock()
    adapter._auth = MagicMock(is_authenticated=True)
    adapter.get_accounts.return_value = [{"account-number": "5WM30541"}]
    adapter.get_balance.return_value = {
        "account_number": "5WM30541",
        "buying_power": 0.0,
        "cash_balance": 100000.0,
    }
    adapter.get_positions.return_value = []
    adapter.list_live_orders.return_value = []
    adapter.dry_run_equity_order.return_value = {"data": {}}
    worker = SandboxBotWorker(_settings(), adapter)
    result = worker.run_cycle(signal="bullish")
    assert result.submitted is False
    adapter.submit_equity_order.assert_not_called()
    adapter.execute_order.assert_not_called()


def test_public_trading_routes_remain_blocked(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("MARKET_DATA_READ_ONLY", "true")
    from backend.app_factory import create_app
    from backend.config.settings import reset_settings_cache

    reset_settings_cache()
    client = create_app(skip_db_init=True, defer_heavy_services=True).test_client()
    assert client.post("/trade/execute", json={"symbol": "TNA"}).status_code == 423
    assert client.post("/bot/start").status_code == 423
    assert client.post("/trade/close/1").status_code == 423


# ---------------------------------------------------------------------------
# Task 3 — diagnostic script
# ---------------------------------------------------------------------------


class _FakeScriptClient:
    def __init__(self, result=None, exc=None, granted_scope="read"):
        self._result = result
        self._exc = exc
        self.auth = MagicMock(granted_scope=granted_scope)
        self.calls = []

    def get_quotes(self, equities, *, indices=()):
        self.calls.append((tuple(equities), tuple(indices)))
        if self._exc:
            raise self._exc
        return self._result


def _result(symbols=("TNA", "TZA", "IWM", "SPY", "QQQ"), *, updated_at="2026-09-28T14:59:55Z", missing=(), unsupported=None):
    from backend.market_data.models import MarketDataResult

    quotes = {s: normalize_tastytrade_quote(_quote_item(s, updated_at=updated_at)) for s in symbols}
    return MarketDataResult(
        quotes=quotes,
        missing=list(missing),
        unsupported=dict(unsupported or {}),
        requested=list(symbols) + list(missing) + list((unsupported or {}).keys()),
    )


def _run_script(capsys, settings=None, client=None, **kwargs):
    code = script.run_check(
        settings or _settings(),
        client_factory=(lambda _cfg: client) if client else None,
        now=NOW,
        **kwargs,
    )
    out = capsys.readouterr()
    return code, out.out + out.err


def _assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def test_script_passes_and_prints_normalized_quotes(capsys):
    fake = _FakeScriptClient(_result(unsupported={"VIX": "index quote not returned; skipped"}))
    code, out = _run_script(capsys, client=fake)
    assert code == 0
    assert script.PASSED in out
    assert "[TNA]" in out and "[QQQ]" in out
    assert "quote_age_seconds: 5.0" in out
    assert "previous_close: 9.95" in out
    assert "[VIX] unsupported" in out
    assert "production_order_execution: blocked" in out
    assert "TASTYTRADE_ENV: sandbox" in out
    assert fake.calls == [(("TNA", "TZA", "IWM", "SPY", "QQQ"), ("VIX",))]
    _assert_no_secrets(out)


def test_script_stale_quotes_warn_but_pass(capsys):
    fake = _FakeScriptClient(_result(updated_at="2026-09-28T13:00:00Z"))
    code, out = _run_script(capsys, client=fake)
    assert code == 0
    assert "warning: TNA: quote is stale" in out
    assert "usable_for_trading (diagnostic only): false (stale_quote)" in out
    assert script.PASSED in out


def test_script_require_fresh_fails_on_stale(capsys):
    fake = _FakeScriptClient(_result(updated_at="2026-09-28T13:00:00Z"))
    code, out = _run_script(capsys, client=fake, require_fresh=True)
    assert code == 1
    assert script.FAILED in out


def test_script_missing_symbol_fails(capsys):
    fake = _FakeScriptClient(_result(symbols=("TNA",), missing=("TZA",)))
    code, out = _run_script(capsys, client=fake)
    assert code == 1
    assert "required symbols missing: TZA" in out
    assert script.FAILED in out


def test_script_config_invalid_exits_2_without_secrets(capsys):
    shared = MD_SECRET
    settings = _settings(tastytrade_client_secret=shared, tastytrade_market_data_client_secret=shared)
    code, out = _run_script(capsys, settings=settings, client=_FakeScriptClient(_result()))
    assert code == script.CONFIG_EXIT_CODE
    assert "must not reuse the sandbox" in out
    assert script.FAILED in out
    _assert_no_secrets(out)


def test_script_execution_not_sandbox_fails(capsys):
    code, out = _run_script(capsys, settings=_settings(tastytrade_env="production"), client=_FakeScriptClient(_result()))
    assert code == script.CONFIG_EXIT_CODE
    assert script.FAILED in out


def test_script_auth_failure_is_classified(capsys):
    exc = MarketDataAuthError(
        "Market-data OAuth failed (401): invalid_refresh_token",
        step="oauth_token",
        reason="invalid_refresh_token",
        status_code=401,
        provider_message="Grant revoked",
        next_step="create a new personal grant",
    )
    code, out = _run_script(capsys, client=_FakeScriptClient(exc=exc))
    assert code == 1
    assert "failure_reason: invalid_refresh_token" in out
    assert "error_step: oauth_token" in out
    assert script.FAILED in out
    _assert_no_secrets(out)


def test_script_rate_limited_exits_3(capsys):
    from backend.adapters.broker.sandbox_rate_limiter import RateLimitInfo

    info = RateLimitInfo(cooldown_seconds=60, endpoint_group=MARKET_DATA_QUOTES_GROUP, step="quotes_equity")
    exc = MarketDataApiError(
        "Quote request failed (429): rate_limited",
        step="quotes_equity",
        reason="rate_limited",
        status_code=429,
        rate_limit=info,
    )
    code, out = _run_script(capsys, client=_FakeScriptClient(exc=exc))
    assert code == 3
    assert "market-data rate limit" in out
    assert "recommended_wait_seconds" in out
    assert script.FAILED in out


def test_script_end_to_end_with_mocked_production_http(capsys, fake_tt, monkeypatch):
    monkeypatch.setattr(md, "persist_refresh_token", lambda *a, **k: False)
    code, out = _run_script(capsys, client=None)
    assert code == 0, out
    assert script.PASSED in out
    assert "[VIX]" in out
    assert {r.url.host for r in fake_tt.requests} == {"api.tastyworks.com"}
    assert all(
        (r.method, r.url.path) in {("POST", "/oauth/token"), ("GET", "/market-data/by-type")}
        for r in fake_tt.requests
    )
    _assert_no_secrets(out)


def test_script_main_fails_closed_when_read_only_false(capsys, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MARKET_DATA_READ_ONLY=false\nTRADING_MODE=sandbox\n", encoding="utf-8")
    # Pre-set keys so monkeypatch restores them after load_dotenv(override=True).
    monkeypatch.setenv("MARKET_DATA_READ_ONLY", "true")
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setattr(script, "_REPO_ROOT", tmp_path)
    code = script.main([])
    out = capsys.readouterr().out
    assert code == script.CONFIG_EXIT_CODE
    assert "MARKET_DATA_READ_ONLY" in out
    assert script.FAILED in out
