"""Tests for scripts/check_tastytrade_sandbox_env.py (no secret values printed)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from backend.adapters.broker.sandbox_env import format_sandbox_env_report, sandbox_env_flags
from backend.config.settings import Settings, reset_settings_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_tastytrade_sandbox_env.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_env", CHECK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_sandbox_env_flags_no_values():
    settings = Settings(
        trading_mode="sandbox",
        tastytrade_env="sandbox",
        tastytrade_client_id="my-client-id",
        tastytrade_client_secret="my-secret",
        tastytrade_refresh_token="my-refresh",
        tastytrade_redirect_uri="https://localhost",
        tastytrade_oauth_scopes="read trade",
    )
    flags = sandbox_env_flags(settings)
    report = format_sandbox_env_report(flags)
    assert "my-client-id" not in report
    assert "my-secret" not in report
    assert "my-refresh" not in report
    assert "TRADING_MODE is sandbox: true" in report
    assert "client_secret configured: true" in report


def test_checker_script_prints_only_booleans(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("TASTYTRADE_CLIENT_ID", "visible-id-should-not-print")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "visible-secret-should-not-print")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "visible-refresh-should-not-print")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    checker = _load_checker()
    assert checker.main() == 0
    out = capsys.readouterr().out
    assert "visible-id-should-not-print" not in out
    assert "visible-secret-should-not-print" not in out
    assert "visible-refresh-should-not-print" not in out
    assert "configured: true" in out


def test_checker_subprocess_no_secrets():
    env = {
        **dict(__import__("os").environ),
        "TRADING_MODE": "sandbox",
        "TASTYTRADE_ENV": "sandbox",
        "TASTYTRADE_CLIENT_SECRET": "subprocess-secret-xyz",
        "TASTYTRADE_REFRESH_TOKEN": "subprocess-refresh-xyz",
    }
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "subprocess-secret-xyz" not in result.stdout
    assert "subprocess-refresh-xyz" not in result.stdout
