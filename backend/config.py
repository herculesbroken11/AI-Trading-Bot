import json
import os
from typing import Any, Dict
from functools import lru_cache

DEFAULT_CONFIG: Dict[str, Any] = {
    "timezone": "US/Eastern",
    "min_confidence": 65,
    "entry_window_start": "09:30",
    "entry_window_end": "10:00",
    "entry_price_buffer": 0.001,
    "forced_exit_time": "15:30",
    "poll_interval_seconds": 60,
    "trailing_stop_pct": 0.02,
    "symbols": ["TNA", "TZA"],
    "stop_loss_pct": 0.05,
    "max_position_pct_of_buying_power": 0.25,
    "min_buying_power_required": 5000,
    "max_trades_per_day": 1,
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
