"""
Centralized runtime settings with safe Phase 2 defaults enforced in code.

Safe defaults live here — not only in .env or documentation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from dotenv import load_dotenv

from backend.config.tastytrade_urls import SANDBOX_BASE_URL

# Phase 2: only paper (local sim) and sandbox (cert API) are permitted modes.
ALLOWED_TRADING_MODES_PHASE_2: FrozenSet[str] = frozenset({"paper", "sandbox"})

# Repo root: backend/config/settings.py -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_PATH = _REPO_ROOT / ".env"


class ConfigurationError(ValueError):
    """Raised when settings violate Phase 2 safety rules."""


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Invalid boolean value: {value!r}")


def _parse_float(value: Optional[str], default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ConfigurationError(f"Invalid float value: {value!r}") from exc


def _env_str(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip()


@dataclass
class Settings:
    """Runtime configuration loaded from environment with safe code defaults."""

    live_trading_enabled: bool = False
    trading_mode: str = "paper"
    tastytrade_env: str = "sandbox"
    emergency_halt: bool = False
    database_url: str = "postgresql://tradebot:tradebot@localhost:5432/tradebot"
    api_admin_key: str = ""
    alphavantage_api_key: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    tastytrade_client_id: str = ""
    tastytrade_client_secret: str = ""
    tastytrade_username: str = ""
    tastytrade_password: str = ""
    tastytrade_refresh_token: str = ""
    tastytrade_redirect_uri: str = "https://localhost"
    tastytrade_oauth_scopes: str = "read trade openid"
    max_daily_loss_usd: float = 500.0
    sql_echo: bool = False
    trading_bot_config: str = "config.json"

    def validate(self) -> None:
        """Enforce Phase 2 trading-safety rules at startup."""
        mode = self.trading_mode.strip().lower()
        if mode == "live":
            raise ConfigurationError(
                "TRADING_MODE=live is blocked in Phase 2. "
                "Use paper (local simulator) or sandbox (cert API only)."
            )
        if mode not in ALLOWED_TRADING_MODES_PHASE_2:
            raise ConfigurationError(
                f"Invalid TRADING_MODE={self.trading_mode!r}. "
                f"Allowed in Phase 2: {sorted(ALLOWED_TRADING_MODES_PHASE_2)}"
            )
        if self.live_trading_enabled:
            raise ConfigurationError(
                "LIVE_TRADING_ENABLED=true is blocked in Phase 2. "
                "Live order routing is not implemented or permitted."
            )

    def validate_startup(self) -> None:
        """Full startup validation including broker environment."""
        self.validate()
        env = self.tastytrade_env.strip().lower()
        if env != "sandbox":
            raise ConfigurationError(
                f"TASTYTRADE_ENV={self.tastytrade_env!r} is not permitted in Phase 2. "
                "Only sandbox is allowed."
            )

    def safe_summary(self) -> Dict[str, Any]:
        """Return non-secret mode and configuration flags for logging."""
        return {
            "live_trading_enabled": self.live_trading_enabled,
            "trading_mode": self.trading_mode,
            "tastytrade_env": self.tastytrade_env,
            "emergency_halt": self.emergency_halt,
            "sandbox_base_url": (
                SANDBOX_BASE_URL if self.tastytrade_env.strip().lower() == "sandbox" else None
            ),
            "production_broker_blocked": True,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "sql_echo": self.sql_echo,
            "openai_model": self.openai_model,
            "trading_bot_config": self.trading_bot_config,
            "alphavantage_api_key_configured": bool(self.alphavantage_api_key),
            "openai_api_key_configured": bool(self.openai_api_key),
            "tastytrade_client_id_configured": bool(self.tastytrade_client_id),
            "tastytrade_client_secret_configured": bool(self.tastytrade_client_secret),
            "tastytrade_username_configured": bool(self.tastytrade_username),
            "tastytrade_password_configured": bool(self.tastytrade_password),
            "tastytrade_refresh_token_configured": bool(self.tastytrade_refresh_token),
            "api_admin_key_configured": bool(self.api_admin_key),
            "database_url_configured": bool(self.database_url),
        }


def load_settings(
    *,
    env_path: Optional[Path] = None,
    override: bool = False,
) -> Settings:
    """
    Load settings from .env and process environment.

    Safe defaults are applied when variables are unset.
    """
    path = env_path if env_path is not None else _DEFAULT_ENV_PATH
    if path.is_file():
        load_dotenv(path, override=override)

    settings = Settings(
        live_trading_enabled=_parse_bool(os.getenv("LIVE_TRADING_ENABLED"), False),
        trading_mode=_env_str("TRADING_MODE", "paper").lower(),
        tastytrade_env=_env_str("TASTYTRADE_ENV", "sandbox").lower(),
        emergency_halt=_parse_bool(os.getenv("EMERGENCY_HALT"), False),
        database_url=_env_str(
            "DATABASE_URL",
            "postgresql://tradebot:tradebot@localhost:5432/tradebot",
        ),
        api_admin_key=_env_str("API_ADMIN_KEY"),
        alphavantage_api_key=_env_str("ALPHAVANTAGE_API_KEY"),
        openai_api_key=_env_str("OPENAI_API_KEY"),
        openai_model=_env_str("OPENAI_MODEL", "gpt-4o-mini"),
        tastytrade_client_id=_env_str("TASTYTRADE_CLIENT_ID"),
        tastytrade_client_secret=_env_str("TASTYTRADE_CLIENT_SECRET"),
        tastytrade_username=_env_str("TASTYTRADE_USERNAME"),
        tastytrade_password=_env_str("TASTYTRADE_PASSWORD"),
        tastytrade_refresh_token=_env_str("TASTYTRADE_REFRESH_TOKEN"),
        tastytrade_redirect_uri=_env_str("TASTYTRADE_REDIRECT_URI", "https://localhost"),
        tastytrade_oauth_scopes=_env_str("TASTYTRADE_OAUTH_SCOPES", "read trade openid"),
        max_daily_loss_usd=_parse_float(os.getenv("MAX_DAILY_LOSS_USD"), 500.0),
        sql_echo=_parse_bool(os.getenv("SQL_ECHO"), False),
        trading_bot_config=_env_str("TRADING_BOT_CONFIG", "config.json"),
    )
    settings.validate()
    return settings


_settings: Optional[Settings] = None


def get_settings(*, reload: bool = False) -> Settings:
    """Return cached settings singleton (reload=True for tests)."""
    global _settings
    if _settings is None or reload:
        _settings = load_settings()
    return _settings


def reset_settings_cache() -> None:
    """Clear cached settings (for tests)."""
    global _settings
    _settings = None
