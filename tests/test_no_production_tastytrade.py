"""Ensure new sandbox code does not import legacy trade_exec or use production URLs."""

import importlib
from pathlib import Path


SANDBOX_NEW_MODULES = [
    "backend.adapters.broker.tastytrade_sandbox",
    "backend.adapters.broker.sandbox_auth",
    "backend.execution.execution_router",
    "scripts/smoke_tastytrade_sandbox_read.py",
    "scripts/smoke_tastytrade_sandbox_order.py",
]


def _import_lines(path: Path) -> str:
    if path.suffix == ".py" and not path.exists():
        path = Path(str(path).replace("scripts/", "scripts/"))
    text = path.read_text(encoding="utf-8")
    return "\n".join(l for l in text.splitlines() if l.strip().startswith(("import ", "from ")))


def test_no_trade_exec_imports_in_sandbox_modules():
    for mod_path in [
        Path("backend/adapters/broker/tastytrade_sandbox.py"),
        Path("backend/adapters/broker/sandbox_auth.py"),
        Path("backend/execution/execution_router.py"),
    ]:
        imports = _import_lines(mod_path)
        assert "trade_exec" not in imports


def test_smoke_scripts_no_trade_exec():
    for script in [
        Path("scripts/smoke_tastytrade_sandbox_read.py"),
        Path("scripts/smoke_tastytrade_sandbox_order.py"),
        Path("scripts/smoke_tastytrade_sandbox_close.py"),
        Path("scripts/smoke_tastytrade_sandbox_cancel.py"),
    ]:
        imports = _import_lines(script)
        assert "trade_exec" not in imports


def test_sandbox_adapter_module_loads_without_trade_exec():
    mod = importlib.import_module("backend.adapters.broker.tastytrade_sandbox")
    imports = _import_lines(Path(mod.__file__))
    assert "trade_exec" not in imports


def test_production_host_not_in_sandbox_adapter():
    source = Path("backend/adapters/broker/tastytrade_sandbox.py").read_text(encoding="utf-8")
    assert "https://api.tastyworks.com" not in source.replace("cert.tastyworks", "")
    assert "api.tastytrade.com" not in source
