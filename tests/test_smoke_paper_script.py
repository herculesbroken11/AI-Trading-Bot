"""Smoke paper script tests."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from backend.config.settings import reset_settings_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_paper.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("smoke_paper", SMOKE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_smoke(extra_env: dict | None = None, extra_args: list | None = None):
    env = {"TRADING_MODE": "paper", "TASTYTRADE_ENV": "sandbox", "LIVE_TRADING_ENABLED": "false"}
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, str(SMOKE_SCRIPT), "--symbol", "TNA", "--price", "50", "--quantity", "1", "--no-db"]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env={**__import__("os").environ, **env},
        capture_output=True,
        text=True,
    )


def test_smoke_script_file_exists():
    assert SMOKE_SCRIPT.is_file()


def test_smoke_script_no_broker_imports():
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    import_lines = [line for line in text.splitlines() if line.strip().startswith(("import ", "from "))]
    joined = "\n".join(import_lines)
    assert "trade_exec" not in joined
    assert "auth_tastytrade" not in joined


def test_smoke_succeeds_no_db(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    reset_settings_cache()

    smoke = _load_smoke_module()
    assert smoke.main(["--symbol", "TNA", "--price", "50", "--quantity", "1", "--no-db"]) == 0


def test_smoke_rejects_live_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    reset_settings_cache()
    smoke = _load_smoke_module()
    assert smoke.main(["--no-db"]) != 0


def test_smoke_rejects_live_trading_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    reset_settings_cache()
    smoke = _load_smoke_module()
    assert smoke.main(["--no-db"]) != 0


def test_smoke_rejects_production_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "production")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    reset_settings_cache()
    smoke = _load_smoke_module()
    assert smoke.main(["--no-db"]) != 0


def test_smoke_subprocess_tza_no_db():
    result = _run_smoke(extra_args=["--symbol", "TZA", "--price", "30"])
    assert result.returncode == 0
    assert "status: filled" in result.stdout
    assert "symbol: TZA" in result.stdout
