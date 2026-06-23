"""Ensure fake buying-power fallbacks are removed from trading paths."""

import ast
from pathlib import Path

import pytest

from backend.bot_manager import TradingBotManager


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
FORBIDDEN_LITERALS = {100000, 999999}


def _numeric_literals_in_file(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if node.value in FORBIDDEN_LITERALS:
                found.add(node.value)
    return found


def test_no_fake_buying_power_literals_in_trading_modules():
    targets = [
        BACKEND_ROOT / "main.py",
        BACKEND_ROOT / "bot_manager.py",
        BACKEND_ROOT / "app_factory.py",
    ]
    violations = []
    for path in targets:
        if not path.exists():
            continue
        bad = _numeric_literals_in_file(path)
        if bad:
            violations.append((str(path), bad))
    assert violations == [], f"Forbidden fallback literals found: {violations}"


def test_bot_manager_skips_when_buying_power_unavailable(monkeypatch):
    """_attempt_entry must not trade when account info is missing."""
    calls = {"place_order": 0}

    class FakeExecutor:
        def get_account_info(self):
            raise RuntimeError("account unavailable")

        def place_order(self, **kwargs):
            calls["place_order"] += 1

    class FakeFeed:
        def fetch_intraday(self, symbol):
            import pandas as pd

            return pd.DataFrame(
                {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]}
            )

        def fetch_daily(self, symbol, outputsize="full"):
            import pandas as pd

            return pd.DataFrame({"close": [1.0, 1.1], "rolling_volatility": [0.1, 0.1]})

        def summarize_intraday(self, df, lookback_minutes=60):
            return {"confidence": 80}

    class FakeAI:
        def analyze_market(self, summaries, data_map, trend_context):
            from backend.models import AIAnalysisResponse

            return (
                AIAnalysisResponse(
                    direction="bullish",
                    confidence=80,
                    entry_price=10,
                    take_profit=0.05,
                    early_exit_profit=0.01,
                    stop_loss=0.05,
                    use_trailing_stop=False,
                    skip_trade=False,
                ),
                "{}",
            )

    class FakeStrategy:
        is_active = True

        def evaluate_entry(self, ai, data_map):
            from backend.models import EntryStrategyResponse
            from datetime import datetime

            return EntryStrategyResponse(
                symbol="TNA",
                side="buy",
                entry_price=10.0,
                entry_window_start=datetime.utcnow(),
                entry_window_end=datetime.utcnow(),
                confidence=80,
                rationale="test",
                indicators={},
                order_type="Market",
            )

    mgr = TradingBotManager(FakeFeed(), FakeAI(), FakeExecutor(), FakeStrategy())
    mgr.config = {"symbols": ["TNA"], "max_trades_per_day": 1}
    mgr.tactics_monitor.should_skip_trading_today = lambda **kwargs: (False, "")

    class FakeSession:
        def close(self):
            pass

    monkeypatch.setattr("backend.bot_manager.TradeLogger.log_prediction", lambda *a, **k: None)
    monkeypatch.setattr("backend.bot_manager.TradeLogger.log_trend_signal", lambda *a, **k: None)
    monkeypatch.setattr("backend.bot_manager.TradeLogger.count_trades_closed_today", lambda s: 0)
    monkeypatch.setattr(mgr, "log_trend_summary", lambda *a, **k: None)

    mgr._attempt_entry(FakeSession())
    assert calls["place_order"] == 0
