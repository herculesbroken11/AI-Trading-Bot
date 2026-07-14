"""CLI tests for run_sandbox_bot_cycle.py."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.bot_worker.sandbox_worker import SandboxBotCycleResult
from backend.config.settings import reset_settings_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
CYCLE_SCRIPT = REPO_ROOT / "scripts" / "run_sandbox_bot_cycle.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_sandbox_bot_cycle", CYCLE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "configured")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "configured")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()


def test_script_signal_none(monkeypatch, capsys):
    _env(monkeypatch)
    mod = _load_module()

    class FakeWorker:
        @classmethod
        def from_settings(cls, settings, with_db=False):
            worker = MagicMock()
            worker.run_cycle.return_value = SandboxBotCycleResult(
                success=True,
                decision_status="skipped_no_signal",
                signal="none",
                message="ok",
            )
            return worker

    monkeypatch.setattr(mod, "SandboxBotWorker", FakeWorker)
    assert mod.main(["--signal", "none"]) == 0
    out = capsys.readouterr().out
    assert "decision_status: skipped_no_signal" in out


def test_script_submit_requires_confirm_flag(monkeypatch):
    _env(monkeypatch)
    mod = _load_module()
    submit_called = {"value": False}

    class FakeWorker:
        @classmethod
        def from_settings(cls, settings, with_db=False):
            worker = MagicMock()

            def run_cycle(**kwargs):
                submit_called["value"] = kwargs.get("confirm_submit", False)
                return SandboxBotCycleResult(
                    success=True,
                    decision_status="dry_run_passed" if not kwargs.get("confirm_submit") else "submitted",
                    signal=kwargs.get("signal", "none"),
                    dry_run_passed=True,
                    submitted=kwargs.get("confirm_submit", False),
                )

            worker.run_cycle.side_effect = run_cycle
            return worker

    monkeypatch.setattr(mod, "SandboxBotWorker", FakeWorker)
    mod.main(["--signal", "bullish"])
    assert submit_called["value"] is False

    mod.main(["--signal", "bullish", "--confirm-sandbox-submit"])
    assert submit_called["value"] is True


def test_script_rejects_live_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    reset_settings_cache()
    mod = _load_module()
    assert mod.main([]) != 0


def test_script_rejects_live_trading_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    reset_settings_cache()
    mod = _load_module()
    assert mod.main([]) != 0


def test_script_no_trade_exec_import():
    text = CYCLE_SCRIPT.read_text(encoding="utf-8")
    imports = [line for line in text.splitlines() if line.strip().startswith(("import ", "from "))]
    joined = "\n".join(imports)
    assert "trade_exec" not in joined
    assert "bot_manager" not in joined


def test_script_never_prints_secrets(monkeypatch, capsys):
    _env(monkeypatch)
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "super-secret-client-secret")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "super-secret-refresh-token")
    mod = _load_module()

    class FakeWorker:
        @classmethod
        def from_settings(cls, settings, with_db=False):
            worker = MagicMock()
            worker.run_cycle.return_value = SandboxBotCycleResult(
                success=True,
                decision_status="skipped_no_signal",
                signal="none",
            )
            return worker

    monkeypatch.setattr(mod, "SandboxBotWorker", FakeWorker)
    mod.main(["--signal", "none"])
    output = capsys.readouterr().out + capsys.readouterr().err
    assert "super-secret-client-secret" not in output
    assert "super-secret-refresh-token" not in output
    assert "Bearer" not in output


def test_public_routes_remain_blocked(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    reset_settings_cache()
    from backend.app_factory import create_app

    client = create_app(skip_db_init=True, defer_heavy_services=True).test_client()
    trade = client.post("/trade/execute", json={"symbol": "TNA"})
    assert trade.status_code == 423
    bot = client.post("/bot/start")
    assert bot.status_code == 423
