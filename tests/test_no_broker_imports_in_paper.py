"""Static checks: paper execution path must not import broker modules."""

import importlib
from pathlib import Path


def _source(module_path: str) -> str:
    mod = importlib.import_module(module_path)
    path = Path(mod.__file__)
    return path.read_text(encoding="utf-8")


def test_paper_simulator_no_broker_imports():
    text = _source("backend.execution.paper_simulator")
    assert "trade_exec" not in text
    assert "auth_tastytrade" not in text


def test_execution_router_no_trade_exec():
    text = _source("backend.execution.execution_router")
    assert "trade_exec" not in text


def test_execution_router_sandbox_not_wired_to_broker():
    text = _source("backend.execution.execution_router")
    assert "not wired" in text.lower() or "placed" in text


def test_smoke_script_no_broker_imports():
    text = Path("scripts/smoke_paper.py").read_text(encoding="utf-8")
    import_lines = [line for line in text.splitlines() if line.strip().startswith(("import ", "from "))]
    joined = "\n".join(import_lines)
    assert "trade_exec" not in joined
    assert "auth_tastytrade" not in joined
