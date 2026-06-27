"""Sandbox smoke script safety tests."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from backend.config.settings import reset_settings_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
READ_SCRIPT = REPO_ROOT / "scripts" / "smoke_tastytrade_sandbox_read.py"
ORDER_SCRIPT = REPO_ROOT / "scripts" / "smoke_tastytrade_sandbox_order.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_read_script_imports_no_trade_exec():
    text = READ_SCRIPT.read_text(encoding="utf-8")
    imports = [l for l in text.splitlines() if l.strip().startswith(("import ", "from "))]
    joined = "\n".join(imports)
    assert "trade_exec" not in joined
    assert "auth_tastytrade" not in joined


def test_order_script_requires_confirm_flag():
    result = subprocess.run(
        [sys.executable, str(ORDER_SCRIPT), "--symbol", "TNA"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_read_script_rejects_paper_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    reset_settings_cache()
    mod = _load_module(READ_SCRIPT, "smoke_read")
    assert mod.main([]) != 0


def test_order_script_rejects_paper_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    reset_settings_cache()
    mod = _load_module(ORDER_SCRIPT, "smoke_order")
    assert mod.main(["--confirm-sandbox-order", "--no-db"]) != 0


def test_order_script_rejects_live_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    reset_settings_cache()
    mod = _load_module(ORDER_SCRIPT, "smoke_order_live")
    assert mod.main(["--confirm-sandbox-order", "--no-db"]) != 0


def test_order_script_rejects_live_trading_enabled(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    reset_settings_cache()
    mod = _load_module(ORDER_SCRIPT, "smoke_order_lte")
    assert mod.main(["--confirm-sandbox-order", "--no-db"]) != 0


def test_order_script_rejects_production_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "production")
    reset_settings_cache()
    mod = _load_module(ORDER_SCRIPT, "smoke_order_prod")
    assert mod.main(["--confirm-sandbox-order", "--no-db"]) != 0


def test_read_script_does_not_submit_orders():
    text = READ_SCRIPT.read_text(encoding="utf-8")
    assert "submit_equity_order" not in text
    assert "OrderExecutor" not in text
    assert "/orders" not in text


def test_read_script_runs_env_checker_first():
    text = READ_SCRIPT.read_text(encoding="utf-8")
    assert "sandbox_env_flags" in text or "format_sandbox_env_report" in text
    assert "ensure_authenticated" in text
    assert "get_customers_me" in text
    env_pos = text.find("sandbox_env")
    auth_pos = text.find("ensure_authenticated")
    customers_pos = text.find("get_customers_me")
    accounts_pos = text.find("get_accounts")
    assert env_pos < auth_pos < customers_pos < accounts_pos


def test_read_script_rejects_unsafe_env_missing_secret(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.delenv("TASTYTRADE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TASTYTRADE_REFRESH_TOKEN", raising=False)
    reset_settings_cache()
    mod = _load_module(READ_SCRIPT, "smoke_read_env")
    assert mod.main([]) != 0


def test_read_smoke_403_output_includes_step_name(monkeypatch, capsys):
    from backend.adapters.broker.sandbox_step_diagnostics import StepFailureDiagnostics
    from backend.adapters.broker.tastytrade_sandbox import SandboxApiError

    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "configured")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "configured")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    mod = _load_module(READ_SCRIPT, "smoke_read_403")

    class FakeAuth:
        is_authenticated = True

        def ensure_authenticated(self):
            return None

    diag = StepFailureDiagnostics(
        step="get_customers_me",
        status_code=403,
        endpoint_path="/customers/me",
        authorization_present=True,
        user_agent_present=True,
        provider_message="Forbidden",
    )
    exc = SandboxApiError(403, "forbidden", step_diagnostics=diag)

    class FakeAdapter:
        _auth = FakeAuth()

        def get_customers_me(self):
            raise exc

    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: FakeAdapter())
    assert mod.main([]) == 1

    err = capsys.readouterr().err
    assert "step: get_customers_me" in err
    assert "status_code: 403" in err
    assert "403 usually means" in err
    assert "Authorization header present: true" in err
    assert "Bearer secret" not in err


def test_read_smoke_never_prints_authorization_value(monkeypatch, capsys):
    from backend.adapters.broker.sandbox_step_diagnostics import StepFailureDiagnostics
    from backend.adapters.broker.tastytrade_sandbox import SandboxApiError

    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "super-secret-client-secret")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "super-secret-refresh-token")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    mod = _load_module(READ_SCRIPT, "smoke_read_secrets")

    class FakeAuth:
        is_authenticated = True

        def ensure_authenticated(self):
            return None

    diag = StepFailureDiagnostics(
        step="get_accounts",
        status_code=403,
        endpoint_path="/customers/me/accounts",
        authorization_present=True,
        user_agent_present=True,
        provider_message="Not allowed",
    )

    class FakeAdapter:
        _auth = FakeAuth()

        def get_customers_me(self):
            return {}

        def get_accounts(self):
            raise SandboxApiError(403, "forbidden", step_diagnostics=diag)

    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: FakeAdapter())
    mod.main([])

    err = capsys.readouterr().err
    assert "super-secret-client-secret" not in err
    assert "super-secret-refresh-token" not in err
    assert "Authorization: Bearer" not in err
