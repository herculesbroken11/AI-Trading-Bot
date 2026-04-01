"""
Trading pipeline: market data vs execution.

- Alpha Vantage: intraday/daily bars, quotes → trend, momentum, volume, AI prompts.
- Tastytrade (sandbox or production): OAuth, balances, orders, positions, closes.

Reference prices can differ slightly between providers; fills are always Tastytrade.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .config import load_config


def describe_pipeline() -> Dict[str, Any]:
    cfg = load_config()
    env = os.getenv("TASTYTRADE_ENV", "sandbox")
    return {
        "market_data_provider": "Alpha Vantage",
        "execution_provider": "Tastytrade",
        "tastytrade_environment": env,
        "tastytrade_api_base": (
            "https://api.cert.tastytrade.com"
            if env == "sandbox"
            else "https://api.tastytrade.com"
        ),
        "symbols": cfg.get("symbols", ["TNA", "TZA"]),
        "flow": [
            "1. Fetch OHLCV from Alpha Vantage (cached + rate-limited).",
            "2. Summarize + OpenAI analysis + strategy entry/exit logic.",
            "3. Size position from Tastytrade buying power (capital_manager).",
            "4. Submit orders and read balances/positions only via Tastytrade API.",
        ],
        "notes": [
            "Signal prices come from Alpha Vantage; execution prices are Tastytrade fills.",
            "Sandbox is for safe order-path testing; production uses TASTYTRADE_ENV=production.",
        ],
    }


def pipeline_status(
    *,
    data_feed: Any,
    auth: Any,
    trade_executor: Optional[Any] = None,
) -> Dict[str, Any]:
    """Lightweight readiness check for dashboard / ops."""
    cfg = load_config()
    av_key = bool(os.getenv("ALPHAVANTAGE_API_KEY"))
    tt_id = bool(os.getenv("TASTYTRADE_CLIENT_ID"))
    tt_secret = bool(os.getenv("TASTYTRADE_CLIENT_SECRET"))
    tt_refresh = bool(os.getenv("TASTYTRADE_REFRESH_TOKEN", "").strip())

    status: Dict[str, Any] = {
        "pipeline": describe_pipeline(),
        "alphavantage": {
            "api_key_configured": av_key,
            "cache_ttl_intraday_s": cfg.get("alphavantage_cache_ttl_intraday_seconds"),
            "cache_ttl_daily_s": cfg.get("alphavantage_cache_ttl_daily_seconds"),
            "cache_ttl_quote_s": cfg.get("alphavantage_cache_ttl_quote_seconds"),
            "max_requests_per_minute": cfg.get("alphavantage_max_requests_per_minute"),
        },
        "tastytrade": {
            "client_id_configured": tt_id,
            "client_secret_configured": tt_secret,
            "refresh_token_configured": tt_refresh,
            "access_token_in_memory": bool(getattr(auth, "access_token", None)),
        },
    }

    if trade_executor and getattr(auth, "access_token", None):
        try:
            info = trade_executor.get_account_info()
            status["tastytrade"]["account_probe"] = "ok" if info.get("account_number") else "no_accounts"
            status["tastytrade"]["account_number"] = info.get("account_number")
        except Exception as exc:
            status["tastytrade"]["account_probe"] = f"error: {exc!s}"

    return status
