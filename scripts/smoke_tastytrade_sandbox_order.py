#!/usr/bin/env python3
"""Submit one small sandbox order via OrderExecutor (explicit confirmation required)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.tastytrade_sandbox import TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, assert_sandbox_base_url
from backend.execution.execution_router import ExecutionRouter
from backend.execution.order_executor import OrderExecutor
from backend.risk.models import OrderIntent, RiskContext
from backend.risk.risk_engine import RiskEngine

WARNING = (
    "This is sandbox only. Sandbox market orders may fill at $1 and do not "
    "represent real market fills."
)


def _validate_env() -> tuple:
    reset_settings_cache()
    settings = load_settings()
    if settings.trading_mode.strip().lower() != "sandbox":
        raise ConfigurationError("Sandbox order smoke requires TRADING_MODE=sandbox")
    if settings.tastytrade_env.strip().lower() != "sandbox":
        raise ConfigurationError("Sandbox order smoke requires TASTYTRADE_ENV=sandbox")
    if settings.live_trading_enabled:
        raise ConfigurationError("LIVE_TRADING_ENABLED must be false")
    if settings.emergency_halt:
        raise ConfigurationError("EMERGENCY_HALT must be false")
    assert_sandbox_base_url(SANDBOX_BASE_URL)
    return settings


def _build_context(settings, price: float) -> RiskContext:
    return RiskContext(
        trading_mode="sandbox",
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


def _build_executor(settings, with_db: bool) -> OrderExecutor:
    adapter = TastytradeSandboxAdapter(settings)
    router = ExecutionRouter(sandbox_adapter=adapter)
    risk = RiskEngine()

    if not with_db:
        return OrderExecutor(risk_engine=risk, router=router)

    from backend.db.session import configure_engine, get_db_session
    from backend.repositories.decision_repository import DecisionRepository
    from backend.repositories.error_repository import ErrorRepository
    from backend.repositories.order_repository import OrderRepository

    configure_engine(settings.database_url, sql_echo=settings.sql_echo)
    session = get_db_session()
    return OrderExecutor(
        risk_engine=risk,
        router=router,
        order_repository=OrderRepository(session),
        decision_repository=DecisionRepository(session),
        error_repository=ErrorRepository(session),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tastytrade sandbox order smoke (OrderExecutor path)")
    parser.add_argument("--symbol", default="TNA", choices=["TNA", "TZA"])
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--price", type=float, default=50.0, help="Reference price for risk checks only")
    parser.add_argument("--confirm-sandbox-order", action="store_true", required=True)
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--with-db", action="store_true")
    args = parser.parse_args(argv)

    if args.quantity != 1:
        print("error: sandbox smoke quantity must be 1 in Phase 2", file=sys.stderr)
        return 2

    with_db = args.with_db and not args.no_db

    print(f"warning: {WARNING}")

    try:
        settings = _validate_env()
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    intent = OrderIntent(
        symbol=args.symbol,
        side="buy",
        quantity=1,
        trading_mode="sandbox",
        source="smoke_tastytrade_sandbox_order",
        reason="checkpoint 2.4 sandbox order smoke",
        current_price=args.price,
    )
    context = _build_context(settings, args.price)
    executor = _build_executor(settings, with_db)
    result = executor.execute(intent, context)

    print(f"status: {result.status}")
    print(f"success: {result.success}")
    print(f"order_id: {result.order_id}")
    print(f"symbol: {result.symbol}")
    print(f"side: {result.side}")
    print(f"quantity: {result.quantity}")
    print(f"fill_price: {result.fill_price}")
    print(f"trading_mode: {result.trading_mode}")
    print(f"message: {result.message}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
