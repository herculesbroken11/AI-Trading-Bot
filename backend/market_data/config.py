"""
Market-data configuration validation (Checkpoint 2.9).

Tastytrade production is used for read-only quotes only. Execution stays on
the sandbox; these credentials are separate and never reach the execution path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List

from backend.config.settings import ALLOWED_TRADING_MODES_PHASE_2, ConfigurationError, Settings
from backend.config.tastytrade_urls import MARKET_DATA_BASE_URL

SUPPORTED_MARKET_DATA_PROVIDERS: FrozenSet[str] = frozenset({"tastytrade"})
SUPPORTED_MARKET_DATA_ENVS: FrozenSet[str] = frozenset({"production"})
# Scopes that are safe on a production token. "trade" is never requested so
# even a leaked/misrouted token cannot place orders.
ALLOWED_MARKET_DATA_SCOPES: FrozenSet[str] = frozenset({"read", "openid"})

MARKET_DATA_ENV_KEYS = (
    "MARKET_DATA_PROVIDER",
    "MARKET_DATA_ENV",
    "MARKET_DATA_READ_ONLY",
    "TASTYTRADE_MARKET_DATA_CLIENT_ID",
    "TASTYTRADE_MARKET_DATA_CLIENT_SECRET",
    "TASTYTRADE_MARKET_DATA_REFRESH_TOKEN",
    "TASTYTRADE_MARKET_DATA_SCOPES",
)


class MarketDataConfigError(ConfigurationError):
    """Market-data settings are unsafe or incomplete."""

    def __init__(self, message: str, *, problems: List[str] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [message])


def safe_fingerprint(value: str) -> str:
    """First4…last4 plus short SHA256. Never the full value."""
    raw = (value or "").strip()
    if not raw:
        return "(not set)"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if len(raw) <= 10:
        return f"(len={len(raw)}) sha256:{digest}"
    return f"{raw[:4]}…{raw[-4:]} (len={len(raw)}) sha256:{digest}"


@dataclass(frozen=True)
class MarketDataConfig:
    provider: str
    env: str
    read_only: bool
    scopes: str
    max_quote_age_seconds: float
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    @property
    def base_url(self) -> str:
        return MARKET_DATA_BASE_URL

    def safe_summary(self) -> Dict[str, object]:
        return {
            "provider": self.provider,
            "env": self.env,
            "read_only": self.read_only,
            "base_url": self.base_url,
            "scopes": self.scopes,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "client_id": safe_fingerprint(self.client_id),
            "client_secret": safe_fingerprint(self.client_secret),
            "refresh_token": safe_fingerprint(self.refresh_token),
        }


def validate_execution_still_sandbox(settings: Settings) -> None:
    """Execution must remain sandbox/paper; live/production execution fails closed."""
    problems: List[str] = []
    if settings.live_trading_enabled:
        problems.append("LIVE_TRADING_ENABLED must be false")
    mode = (settings.trading_mode or "").strip().lower()
    if mode not in ALLOWED_TRADING_MODES_PHASE_2:
        problems.append(
            f"TRADING_MODE={settings.trading_mode!r} is not permitted "
            f"(allowed: {sorted(ALLOWED_TRADING_MODES_PHASE_2)})"
        )
    if (settings.tastytrade_env or "").strip().lower() != "sandbox":
        problems.append(
            f"TASTYTRADE_ENV={settings.tastytrade_env!r}; order execution must stay on sandbox"
        )
    if problems:
        raise MarketDataConfigError(
            "Execution environment is not sandbox-only: " + "; ".join(problems),
            problems=problems,
        )


def _scope_tokens(scopes: str) -> List[str]:
    return [token for token in (scopes or "").replace(",", " ").split() if token]


def validate_market_data_settings(settings: Settings) -> MarketDataConfig:
    """
    Validate production market-data settings. Fails closed on anything unsafe.

    Raises MarketDataConfigError listing every problem (no secret values).
    """
    validate_execution_still_sandbox(settings)

    problems: List[str] = []
    provider = (settings.market_data_provider or "").strip().lower()
    env = (settings.market_data_env or "").strip().lower()

    if settings.market_data_read_only is not True:
        problems.append("MARKET_DATA_READ_ONLY must be true")
    if provider not in SUPPORTED_MARKET_DATA_PROVIDERS:
        problems.append(
            f"MARKET_DATA_PROVIDER={settings.market_data_provider!r} is not supported "
            f"(allowed: {sorted(SUPPORTED_MARKET_DATA_PROVIDERS)})"
        )
    if env not in SUPPORTED_MARKET_DATA_ENVS:
        problems.append(
            f"MARKET_DATA_ENV={settings.market_data_env!r} is not supported "
            f"(allowed: {sorted(SUPPORTED_MARKET_DATA_ENVS)})"
        )

    client_id = (settings.tastytrade_market_data_client_id or "").strip()
    client_secret = (settings.tastytrade_market_data_client_secret or "").strip()
    refresh_token = (settings.tastytrade_market_data_refresh_token or "").strip()
    for key, value in (
        ("TASTYTRADE_MARKET_DATA_CLIENT_ID", client_id),
        ("TASTYTRADE_MARKET_DATA_CLIENT_SECRET", client_secret),
        ("TASTYTRADE_MARKET_DATA_REFRESH_TOKEN", refresh_token),
    ):
        if not value:
            problems.append(f"{key} is not set")

    sandbox_pairs = (
        ("TASTYTRADE_MARKET_DATA_CLIENT_ID", client_id, settings.tastytrade_client_id),
        ("TASTYTRADE_MARKET_DATA_CLIENT_SECRET", client_secret, settings.tastytrade_client_secret),
        (
            "TASTYTRADE_MARKET_DATA_REFRESH_TOKEN",
            refresh_token,
            settings.tastytrade_refresh_token,
        ),
    )
    for key, market_value, sandbox_value in sandbox_pairs:
        if market_value and market_value == (sandbox_value or "").strip():
            problems.append(
                f"{key} must not reuse the sandbox execution credential "
                "(use a separate production OAuth app)"
            )

    scopes = " ".join(_scope_tokens(settings.tastytrade_market_data_scopes)) or "read"
    tokens = set(_scope_tokens(scopes))
    forbidden = sorted(tokens - ALLOWED_MARKET_DATA_SCOPES)
    if forbidden:
        problems.append(
            f"TASTYTRADE_MARKET_DATA_SCOPES contains non-read-only scope(s) {forbidden}; "
            "use 'read' only"
        )
    if "read" not in tokens:
        problems.append("TASTYTRADE_MARKET_DATA_SCOPES must include 'read'")

    max_age = float(settings.market_data_max_quote_age_seconds)
    if max_age <= 0:
        problems.append("MARKET_DATA_MAX_QUOTE_AGE_SECONDS must be > 0")

    if problems:
        raise MarketDataConfigError(
            "Market-data configuration invalid: " + "; ".join(problems),
            problems=problems,
        )

    return MarketDataConfig(
        provider=provider,
        env=env,
        read_only=True,
        scopes=scopes,
        max_quote_age_seconds=max_age,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
