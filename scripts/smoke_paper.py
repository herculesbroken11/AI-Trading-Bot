"""Safe local paper-order smoke test (no broker, no Flask, no bot loop)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo root on path when run as script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.execution.order_executor import OrderExecutor
from backend.execution.paper_simulator import PaperSimulator
from backend.execution.execution_router import ExecutionRouter
from backend.risk.models import OrderIntent, RiskContext
from backend.risk.risk_engine import RiskEngine


def _validate_environment(settings: Settings) -> None:
    if settings.trading_mode.strip().lower() != "paper":
        raise ConfigurationError("smoke_paper requires TRADING_MODE=paper")
    if settings.live_trading_enabled:
        raise ConfigurationError("smoke_paper blocked: LIVE_TRADING_ENABLED=true")
    settings.validate_startup()


def _build_intent(symbol: str, price: float, quantity: int) -> OrderIntent:
    return OrderIntent(
        symbol=symbol.upper(),
        side="buy",
        quantity=quantity,
        trading_mode="paper",
        order_type="Market",
        source="smoke_paper",
        reason="checkpoint 2.3 smoke test",
        current_price=price,
    )


def _build_context(settings: Settings, price: float) -> RiskContext:
    return RiskContext(
        trading_mode="paper",
        live_trading_enabled=False,
        tastytrade_env=settings.tastytrade_env,
        emergency_halt=settings.emergency_halt,
        buying_power=10_000.0,
        current_price=price,
        open_positions_count=0,
        pending_orders_count=0,
        trades_today_count=0,
        daily_pnl=0.0,
        market_data_healthy=True,
        max_trades_per_day=3,
        max_daily_loss_usd=settings.max_daily_loss_usd,
        buying_power_reserve_pct=0.08,
        max_position_pct_of_buying_power=0.25,
        single_position_mode=True,
        ai_decision_valid=True,
        strategy_signal_valid=True,
    )


def _build_executor(*, with_db: bool) -> OrderExecutor:
    paper = PaperSimulator()
    router = ExecutionRouter(paper_simulator=paper)
    risk = RiskEngine()

    if not with_db:
        return OrderExecutor(risk_engine=risk, router=router)

    from backend.db.session import configure_engine, get_db_session
    from backend.repositories.decision_repository import DecisionRepository
    from backend.repositories.error_repository import ErrorRepository
    from backend.repositories.order_repository import OrderRepository

    settings = load_settings()
    configure_engine(settings.database_url, sql_echo=settings.sql_echo)
    session = get_db_session()
    return OrderExecutor(
        risk_engine=risk,
        router=router,
        order_repository=OrderRepository(session),
        decision_repository=DecisionRepository(session),
        error_repository=ErrorRepository(session),
    )


def _print_safe_summary(result) -> int:
    summary = {
        "status": result.status,
        "success": result.success,
        "order_id": result.order_id,
        "symbol": result.symbol,
        "side": result.side,
        "quantity": result.quantity,
        "fill_price": result.fill_price,
        "trading_mode": result.trading_mode,
        "message": result.message,
    }
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0 if result.success and result.status == "filled" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2 paper-only execution smoke test")
    parser.add_argument("--symbol", default="TNA", choices=["TNA", "TZA"])
    parser.add_argument("--price", type=float, default=50.0)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--no-db", action="store_true", help="Run without database logging")
    parser.add_argument("--with-db", action="store_true", help="Persist logs to DATABASE_URL")
    args = parser.parse_args(argv)

    if args.with_db and args.no_db:
        print("error: use either --no-db or --with-db, not both", file=sys.stderr)
        return 2

    with_db = args.with_db and not args.no_db
    if not args.no_db and not args.with_db:
        with_db = False

    reset_settings_cache()
    try:
        settings = load_settings()
        _validate_environment(settings)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    intent = _build_intent(args.symbol, args.price, args.quantity)
    context = _build_context(settings, args.price)
    executor = _build_executor(with_db=with_db)
    result = executor.execute(intent, context)
    return _print_safe_summary(result)


if __name__ == "__main__":
    raise SystemExit(main())
