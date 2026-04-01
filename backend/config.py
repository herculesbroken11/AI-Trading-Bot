import json
import os
from typing import Any, Dict
from functools import lru_cache

DEFAULT_CONFIG: Dict[str, Any] = {
    "timezone": "US/Eastern",
    "min_confidence": 65,
    # Morning entry: wait N minutes after US open, then trade until entry_window_end (adaptive timing band).
    "market_open_time": "09:30",
    "morning_wait_minutes_after_open": 12,
    "entry_window_start": "09:42",
    "entry_window_end": "10:15",
    "entry_price_buffer": 0.001,
    "order_type": "Market",
    "pullback_entry_enabled": True,
    "pullback_min_retrace_pct": 0.0015,
    "pullback_lookback_bars": 5,
    "buying_power_reserve_pct": 0.08,
    "forced_exit_time": "15:30",
    "poll_interval_seconds": 60,
    "trailing_stop_pct": 0.02,
    "symbols": ["TNA", "TZA"],
    "stop_loss_pct": 0.05,
    "max_position_pct_of_buying_power": 0.25,
    "min_buying_power_required": 5000,
    "max_trades_per_day": 1,
    # Alpha Vantage: market data only (signals / AI). Stay under plan limits (e.g. 150/min).
    "alphavantage_cache_ttl_intraday_seconds": 45,
    "alphavantage_cache_ttl_daily_seconds": 300,
    "alphavantage_cache_ttl_quote_seconds": 20,
    "alphavantage_max_requests_per_minute": 120,
}

CONFIG_ENV_KEY = "TRADING_BOT_CONFIG"
CONFIG_FILENAME = "config.json"

@lru_cache()
def load_config() -> Dict[str, Any]:
    path = os.getenv(CONFIG_ENV_KEY, CONFIG_FILENAME)
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                file_config = json.load(fp)
                if isinstance(file_config, dict):
                    config.update(file_config)
        except Exception as exc:
            raise RuntimeError(f"Failed to load config from {path}: {exc}")
    return config
