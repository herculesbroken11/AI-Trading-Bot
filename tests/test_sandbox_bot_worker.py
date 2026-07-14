"""SandboxBotWorker unit tests."""

from unittest.mock import MagicMock

import pytest

from backend.adapters.broker.tastytrade_sandbox import SandboxApiError
from backend.bot_worker.sandbox_worker import (
    SandboxBotWorker,
    map_signal_to_symbol,
    validate_sandbox_worker_settings,
)
from backend.config.settings import ConfigurationError, Settings
from backend.risk.models import ApprovalResult, OrderIntent


def _settings(**overrides) -> Settings:
    base = {
        "trading_mode": "sandbox",
        "tastytrade_env": "sandbox",
        "live_trading_enabled": False,
        "emergency_halt": False,
        "tastytrade_client_id": "cid",
        "tastytrade_client_secret": "secret",
        "tastytrade_refresh_token": "refresh",
    }
    base.update(overrides)
    return Settings(**base)


def _fake_auth():
    auth = MagicMock()
    auth.is_authenticated = True
    auth.ensure_authenticated = MagicMock()
    return auth


def _clean_adapter(**overrides):
    adapter = MagicMock()
    adapter._auth = _fake_auth()
    adapter.get_customers_me.return_value = {}
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


def test_map_signal_to_symbol():
    assert map_signal_to_symbol("bullish") == "TNA"
    assert map_signal_to_symbol("bearish") == "TZA"
    assert map_signal_to_symbol("none") is None


def test_validate_sandbox_worker_settings_rejects_live():
    with pytest.raises(ConfigurationError):
        validate_sandbox_worker_settings(_settings(trading_mode="live"))


def test_signal_none_skips_without_dry_run():
    adapter = _clean_adapter()
    worker = SandboxBotWorker(_settings(), adapter)
    result = worker.run_cycle(signal="none")
    assert result.decision_status == "skipped_no_signal"
    assert result.success is True
    adapter.dry_run_equity_order.assert_not_called()


def test_bullish_maps_to_tna_dry_run():
    adapter = _clean_adapter()
    worker = SandboxBotWorker(_settings(), adapter)
    result = worker.run_cycle(signal="bullish", confirm_submit=False)
    assert result.decision_status == "dry_run_passed"
    assert result.symbol == "TNA"
    adapter.dry_run_equity_order.assert_called_once()
    args = adapter.dry_run_equity_order.call_args
    assert args[0][1] == "TNA"


def test_bearish_maps_to_tza_dry_run():
    adapter = _clean_adapter()
    worker = SandboxBotWorker(_settings(), adapter)
    result = worker.run_cycle(signal="bearish", confirm_submit=False)
    assert result.symbol == "TZA"
    assert adapter.dry_run_equity_order.call_args[0][1] == "TZA"


def test_active_live_order_blocks_trade():
    adapter = _clean_adapter(
        list_live_orders=MagicMock(
            return_value=[
                {
                    "id": 1,
                    "status": "Live",
                    "order-type": "Limit",
                    "price": "2.00",
                    "legs": [{"symbol": "TNA", "quantity": 1, "action": "Buy to Open"}],
                }
            ]
        )
    )
    worker = SandboxBotWorker(_settings(), adapter)
    result = worker.run_cycle(signal="bullish")
    assert result.decision_status == "skipped_live_order_exists"
    assert result.active_live_orders_count == 1
    adapter.dry_run_equity_order.assert_not_called()


def test_open_position_blocks_trade():
    adapter = _clean_adapter(get_positions=MagicMock(return_value=[{"symbol": "TNA", "quantity": 1}]))
    worker = SandboxBotWorker(_settings(), adapter)
    result = worker.run_cycle(signal="bullish")
    assert result.decision_status == "skipped_position_exists"
    assert result.positions_count == 1
    adapter.dry_run_equity_order.assert_not_called()


def test_risk_rejection_blocks_dry_run(monkeypatch):
    adapter = _clean_adapter()
    risk = MagicMock()
    risk.approve.return_value = ApprovalResult(
        approved=False,
        reason="blocked",
        rejection_code="TEST",
        checks=[],
    )
    worker = SandboxBotWorker(_settings(), adapter, risk_engine=risk)
    result = worker.run_cycle(signal="bullish")
    assert result.decision_status == "risk_rejected"
    adapter.dry_run_equity_order.assert_not_called()


def test_submit_only_after_dry_run_with_confirm():
    adapter = _clean_adapter()
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
    adapter.fetch_order_status_summary.return_value = {
        "broker_order_id": "99",
        "broker_status": "Live",
        "status": "submitted",
    }
    worker = SandboxBotWorker(_settings(), adapter, executor=executor)
    result = worker.run_cycle(signal="bullish", confirm_submit=True)
    assert result.decision_status == "submitted"
    adapter.dry_run_equity_order.assert_called_once()
    executor.execute.assert_called_once()


def test_no_submit_without_confirm():
    adapter = _clean_adapter()
    executor = MagicMock()
    worker = SandboxBotWorker(_settings(), adapter, executor=executor)
    result = worker.run_cycle(signal="bullish", confirm_submit=False)
    assert result.decision_status == "dry_run_passed"
    executor.execute.assert_not_called()


def test_dry_run_failure_blocks_submit():
    adapter = _clean_adapter(
        dry_run_equity_order=MagicMock(side_effect=SandboxApiError(422, "failed"))
    )
    executor = MagicMock()
    worker = SandboxBotWorker(_settings(), adapter, executor=executor)
    result = worker.run_cycle(signal="bullish", confirm_submit=True)
    assert result.decision_status == "dry_run_failed"
    executor.execute.assert_not_called()
