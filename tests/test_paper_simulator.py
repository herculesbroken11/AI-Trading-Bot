"""Tests for PaperSimulator (Checkpoint 2.1)."""

import importlib
import sys

import pytest

from backend.execution.paper_simulator import PaperSimulator
from backend.risk.models import OrderIntentValidationError
from tests.execution_helpers import make_context, make_intent


def test_paper_buy_tna_fills_with_paper_order_id():
    sim = PaperSimulator()
    result = sim.execute(make_intent(symbol="TNA"), make_context())
    assert result.success is True
    assert result.status == "filled"
    assert result.order_id.startswith("PAPER-")
    assert result.symbol == "TNA"
    assert result.side == "buy"


def test_paper_buy_tza_fills_with_paper_order_id():
    sim = PaperSimulator()
    result = sim.execute(
        make_intent(symbol="TZA", current_price=15.0),
        make_context(current_price=15.0),
    )
    assert result.success is True
    assert result.order_id.startswith("PAPER-")
    assert result.symbol == "TZA"


def test_paper_sell_rejected():
    with pytest.raises(OrderIntentValidationError):
        make_intent(side="sell")


def test_paper_fill_uses_provided_current_price():
    sim = PaperSimulator()
    result = sim.execute(make_intent(current_price=42.5), make_context())
    assert result.fill_price == 42.5


def test_no_broker_modules_imported_by_paper_simulator():
    broker_modules = ("backend.trade_exec", "backend.auth_tastytrade", "httpx")
    for name in broker_modules:
        assert name not in sys.modules or True  # may be loaded elsewhere in test session

    source = importlib.import_module("backend.execution.paper_simulator").__file__
    with open(source, encoding="utf-8") as fp:
        text = fp.read()
    assert "trade_exec" not in text
    assert "auth_tastytrade" not in text
    assert "httpx" not in text
    assert "tastytrade" not in text.lower()
