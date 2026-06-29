#!/usr/bin/env python3
"""Submit one small sandbox order via OrderExecutor (dry-run first, explicit confirm)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError
from backend.execution.execution_router import ExecutionRouter
from backend.execution.order_executor import OrderExecutor
from backend.repositories.order_repository import OrderRepository
from backend.risk.models import OrderIntent, RiskContext
from backend.risk.risk_engine import RiskEngine
from scripts.sandbox_smoke_common import (
    fetch_account_state,
    print_api_error,
    print_auth_error,
    print_db_order_summary,
    print_env_check,
    validate_sandbox_env,
    verify_order_after_submit,
    verify_position_after_buy,
)

WARNING = (
    "This is sandbox only. Sandbox market orders may fill at $1 and do not "
    "represent real market fills. Limit orders below $3 fill immediately in sandbox."
)


def _coerce_balance_amount(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_context(settings, price: float, balance: dict, positions_count: int) -> RiskContext:
    buying_power = _coerce_balance_amount(balance.get("buying_power"))
    if buying_power is None or buying_power <= 0:
        buying_power = _coerce_balance_amount(balance.get("cash_balance")) or 10_000.0
    return RiskContext(
        trading_mode="sandbox",
        live_trading_enabled=False,
        tastytrade_env=settings.tastytrade_env,
        emergency_halt=settings.emergency_halt,
        buying_power=float(buying_power),
        current_price=price,
        open_positions_count=positions_count,
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


def _build_executor(
    settings,
    adapter: TastytradeSandboxAdapter,
    with_db: bool,
) -> tuple[OrderExecutor, Optional[OrderRepository]]:
    router = ExecutionRouter(sandbox_adapter=adapter)
    risk = RiskEngine()

    if not with_db:
        return OrderExecutor(risk_engine=risk, router=router), None

    from backend.db.session import configure_engine, get_db_session
    from backend.repositories.decision_repository import DecisionRepository
    from backend.repositories.error_repository import ErrorRepository

    configure_engine(settings.database_url, sql_echo=settings.sql_echo)
    session = get_db_session()
    order_repo = OrderRepository(session)
    return (
        OrderExecutor(
            risk_engine=risk,
            router=router,
            order_repository=order_repo,
            decision_repository=DecisionRepository(session),
            error_repository=ErrorRepository(session),
        ),
        order_repo,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tastytrade sandbox order smoke (dry-run first)")
    parser.add_argument("--symbol", default="TNA", choices=["TNA", "TZA"])
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--price", type=float, default=50.0, help="Reference price for risk checks only")
    parser.add_argument("--order-type", default="Market", choices=["Market", "Limit"])
    parser.add_argument("--limit-price", type=float, default=None)
    parser.add_argument(
        "--confirm-sandbox-order",
        action="store_true",
        help="Submit real sandbox order after dry-run passes",
    )
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--with-db", action="store_true")
    args = parser.parse_args(argv)

    if args.with_db and args.no_db:
        print("error: use either --with-db or --no-db, not both", file=sys.stderr)
        return 2

    if args.quantity != 1:
        print("error: sandbox smoke quantity must be 1 in Phase 2", file=sys.stderr)
        return 2

    if args.order_type == "Limit" and args.limit_price is None:
        print("error: --limit-price is required when --order-type Limit", file=sys.stderr)
        return 2

    with_db = args.with_db and not args.no_db

    print(f"warning: {WARNING}")

    try:
        settings = validate_sandbox_env(script_name="smoke_tastytrade_sandbox_order")
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not print_env_check(settings):
        print("error: sandbox env check failed", file=sys.stderr)
        return 2

    adapter = TastytradeSandboxAdapter(settings)

    try:
        balance, positions = fetch_account_state(adapter)
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
        else:
            print_auth_error(exc)
        return 1

    positions_count = len(positions)
    print(f"selected_account: {balance.get('account_number')}")
    print(f"buying_power: {balance.get('buying_power')}")
    print(f"cash_balance: {balance.get('cash_balance')}")
    print(f"positions_count: {positions_count}")

    order_type = args.order_type.strip().capitalize()
    intent = OrderIntent(
        symbol=args.symbol,
        side="buy",
        quantity=1,
        trading_mode="sandbox",
        order_type=order_type,
        limit_price=args.limit_price,
        source="smoke_tastytrade_sandbox_order",
        reason="sandbox order smoke",
        current_price=args.price,
    )
    context = _build_context(settings, args.price, balance, positions_count)
    risk = RiskEngine()
    approval = risk.approve(intent, context)
    if not approval.approved:
        print(f"error: risk rejected order: {approval.reason}", file=sys.stderr)
        return 1

    try:
        adapter.dry_run_equity_order(
            balance.get("account_number"),
            args.symbol,
            "buy",
            1,
            order_type=order_type,
            limit_price=args.limit_price,
        )
    except SandboxApiError as exc:
        print_api_error(exc)
        return 1
    except SandboxAuthError as exc:
        print_auth_error(exc)
        return 1

    print("dry_run: passed")

    if not args.confirm_sandbox_order:
        print("info: dry-run passed; re-run with --confirm-sandbox-order to submit", file=sys.stderr)
        return 0

    executor, order_repo = _build_executor(settings, adapter, with_db)
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

    if not result.success:
        return 1

    exit_code = verify_order_after_submit(
        adapter,
        balance.get("account_number"),
        order_id=result.order_id,
        raw=result.raw,
    )
    verify_position_after_buy(adapter, balance.get("account_number"), args.symbol)

    if with_db and order_repo and result.order_id:
        print_db_order_summary(order_repo, result.order_id)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
