"""Regression tests for TZA entry side fix (Checkpoint 2.1)."""

from datetime import datetime, time
from unittest.mock import patch

import pandas as pd
import pytz

from backend.models import AIAnalysisResponse
from backend.strategy import TradingStrategy


def _base_config():
    return {
        "timezone": "US/Eastern",
        "min_confidence": 65,
        "market_open_time": "09:30",
        "morning_wait_minutes_after_open": 12,
        "entry_window_end": "10:15",
        "pullback_entry_enabled": True,
        "pullback_min_retrace_pct": 0.001,
        "pullback_lookback_bars": 2,
        "entry_price_buffer": 0.001,
        "order_type": "Market",
    }


def _intraday_df(et_tz):
    idx = pd.date_range("2025-06-02 09:42", periods=6, freq="1min", tz=et_tz)
    return pd.DataFrame(
        {
            "open": [10.0] * 6,
            "high": [10.5] * 6,
            "low": [9.5] * 6,
            "close": [10.0] * 6,
            "volume": [1000] * 6,
            "volume_sma_10": [900] * 6,
            "volatility_10": [0.02] * 6,
            "money_flow_volume": [100] * 6,
        },
        index=idx,
    )


def _ai(**kwargs):
    base = {
        "direction": "bullish",
        "confidence": 80.0,
        "entry_price": 10.0,
        "take_profit": 0.05,
        "early_exit_profit": 0.015,
        "stop_loss": 0.05,
        "use_trailing_stop": False,
        "skip_trade": False,
    }
    base.update(kwargs)
    return AIAnalysisResponse(**base)


def _evaluate_with_fixed_window(strategy, ai, data_map):
    et = pytz.timezone("US/Eastern")
    fixed_now = et.localize(datetime(2025, 6, 2, 9, 50))
    window = (time(9, 42), time(10, 15), "09:42", "10:15")

    with patch.object(strategy, "get_effective_entry_window_times", return_value=window):
        with patch.object(strategy, "_today_at", return_value=fixed_now):
            with patch("backend.strategy.datetime") as mock_dt:
                mock_dt.now.return_value = fixed_now
                return strategy.evaluate_entry(ai, data_map)


def test_bullish_selects_tna_buy():
    et = pytz.timezone("US/Eastern")
    strategy = TradingStrategy(_base_config())
    strategy.is_active = True
    df = _intraday_df(et)
    entry = _evaluate_with_fixed_window(strategy, _ai(direction="bullish"), {"TNA": df, "TZA": df})

    assert entry is not None
    assert entry.symbol == "TNA"
    assert entry.side == "buy"


def test_bearish_selects_tza_buy_not_sell():
    et = pytz.timezone("US/Eastern")
    strategy = TradingStrategy(_base_config())
    strategy.is_active = True
    df = _intraday_df(et)
    entry = _evaluate_with_fixed_window(strategy, _ai(direction="bearish"), {"TNA": df, "TZA": df})

    assert entry is not None
    assert entry.symbol == "TZA"
    assert entry.side == "buy"


def test_sideways_returns_no_trade():
    et = pytz.timezone("US/Eastern")
    strategy = TradingStrategy(_base_config())
    strategy.is_active = True
    df = _intraday_df(et)
    entry = _evaluate_with_fixed_window(strategy, _ai(direction="sideways"), {"TNA": df, "TZA": df})

    assert entry is None
