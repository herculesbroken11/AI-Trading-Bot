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


def test_order_script_dry_run_without_confirm_does_not_submit(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "configured")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "configured")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    mod = _load_module(ORDER_SCRIPT, "smoke_order_dry_only")
    submit_called = {"value": False}
    execute_called = {"value": False}

    class FakeAuth:
        is_authenticated = True

        def ensure_authenticated(self):
            return None

    class FakeAdapter:
        _auth = FakeAuth()

        def get_customers_me(self):
            return {}

        def get_accounts(self):
            return [{"account-number": "5WM30541"}]

        def get_balance(self):
            return {
                "account_number": "5WM30541",
                "buying_power": 100000.0,
                "cash_balance": 100000.0,
            }

        def get_positions(self, account_number):
            return []

        def dry_run_equity_order(self, *args, **kwargs):
            return {"data": {}}

        def submit_equity_order(self, *args, **kwargs):
            submit_called["value"] = True
            return {"data": {}}

    class FakeExecutor:
        def execute(self, intent, context):
            execute_called["value"] = True
            return None

    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: FakeAdapter())
    monkeypatch.setattr(mod, "OrderExecutor", lambda **kwargs: FakeExecutor())

    assert mod.main(["--symbol", "TNA", "--no-db"]) == 0
    assert submit_called["value"] is False
    assert execute_called["value"] is False
    out = capsys.readouterr().out
    assert "dry_run: passed" in out


def test_order_script_submit_requires_confirm_flag(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "configured")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "configured")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    mod = _load_module(ORDER_SCRIPT, "smoke_order_confirm")
    execute_called = {"value": False}

    class FakeAuth:
        is_authenticated = True

        def ensure_authenticated(self):
            return None

    class FakeAdapter:
        _auth = FakeAuth()

        def get_customers_me(self):
            return {}

        def get_accounts(self):
            return [{"account-number": "5WM30541"}]

        def get_balance(self):
            return {
                "account_number": "5WM30541",
                "buying_power": 100000.0,
                "cash_balance": 100000.0,
            }

        def get_positions(self, account_number):
            return []

        def dry_run_equity_order(self, *args, **kwargs):
            return {"data": {}}

    class FakeExecutor:
        def execute(self, intent, context):
            execute_called["value"] = True
            from backend.risk.models import ExecutionResult

            return ExecutionResult(
                success=True,
                status="filled",
                order_id="SANDBOX-1",
                symbol="TNA",
                side="buy",
                quantity=1,
                trading_mode="sandbox",
                message="ok",
            )

    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: FakeAdapter())
    monkeypatch.setattr(mod, "OrderExecutor", lambda **kwargs: FakeExecutor())

    mod.main(["--symbol", "TNA", "--confirm-sandbox-order", "--no-db"])
    assert execute_called["value"] is True


def test_order_script_stops_when_dry_run_fails(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "configured")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "configured")
    monkeypatch.setenv("TASTYTRADE_OAUTH_SCOPES", "read trade")
    reset_settings_cache()

    from backend.adapters.broker.sandbox_step_diagnostics import StepFailureDiagnostics
    from backend.adapters.broker.tastytrade_sandbox import SandboxApiError

    mod = _load_module(ORDER_SCRIPT, "smoke_order_dry_fail")
    execute_called = {"value": False}

    diag = StepFailureDiagnostics(
        step="dry_run_order",
        status_code=422,
        endpoint_path="/accounts/5WM30541/orders/dry-run",
        authorization_present=True,
        user_agent_present=True,
        provider_message="One or more preflight checks failed",
        provider_error_code="preflight_check_failure",
        redacted_response_json='{"error":{"code":"preflight_check_failure"}}',
    )

    class FakeAuth:
        is_authenticated = True

        def ensure_authenticated(self):
            return None

    class FakeAdapter:
        _auth = FakeAuth()

        def get_customers_me(self):
            return {}

        def get_accounts(self):
            return [{"account-number": "5WM30541"}]

        def get_balance(self):
            return {
                "account_number": "5WM30541",
                "buying_power": 0.0,
                "cash_balance": 100000.0,
            }

        def get_positions(self, account_number):
            return []

        def dry_run_equity_order(self, *args, **kwargs):
            raise SandboxApiError(422, "failed", step_diagnostics=diag)

    class FakeExecutor:
        def execute(self, intent, context):
            execute_called["value"] = True

    monkeypatch.setattr(mod, "TastytradeSandboxAdapter", lambda settings: FakeAdapter())
    monkeypatch.setattr(mod, "OrderExecutor", lambda **kwargs: FakeExecutor())

    assert mod.main(["--symbol", "TNA", "--confirm-sandbox-order", "--no-db"]) == 1
    assert execute_called["value"] is False
    err = capsys.readouterr().err
    assert "step: dry_run_order" in err
    assert "response_json:" in err
    assert "preflight_check_failure" in err


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
    assert mod.main(["--no-db"]) != 0


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


def test_order_script_build_context_handles_string_buying_power():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.smoke_tastytrade_sandbox_order import _build_context
    from backend.config.settings import Settings

    settings = Settings(trading_mode="sandbox", tastytrade_env="sandbox")
    context = _build_context(
        settings,
        50.0,
        {"buying_power": "0.0", "cash_balance": "100000.0"},
        0,
    )
    assert context.buying_power == 100000.0


def test_order_script_uses_dry_run_before_submit():
    text = ORDER_SCRIPT.read_text(encoding="utf-8")
    assert "dry_run_equity_order" in text
    dry_pos = text.find("dry_run_equity_order")
    submit_pos = text.find("executor.execute")
    assert dry_pos < submit_pos


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
    from backend.config.settings import Settings

    monkeypatch.setenv("TRADING_MODE", "sandbox")
    monkeypatch.setenv("TASTYTRADE_ENV", "sandbox")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    reset_settings_cache()
    mod = _load_module(READ_SCRIPT, "smoke_read_env")

    def _settings_without_secrets(**kwargs):
        return Settings(
            trading_mode="sandbox",
            tastytrade_env="sandbox",
            live_trading_enabled=False,
            tastytrade_client_secret="",
            tastytrade_refresh_token="",
            tastytrade_oauth_scopes="",
        )

    monkeypatch.setattr(mod, "load_settings", _settings_without_secrets)
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
