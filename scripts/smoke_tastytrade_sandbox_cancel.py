#!/usr/bin/env python3
"""List and optionally cancel live sandbox orders (explicit confirm required)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.sandbox_order_verification import (
    format_live_orders_summary,
    format_order_status_summary,
    summarize_live_orders_response,
    summarize_order_response,
)
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError
from backend.repositories.order_repository import OrderRepository
from scripts.sandbox_smoke_common import (
    fetch_account_state,
    print_api_error,
    print_auth_error,
    print_db_order_summary,
    print_env_check,
    validate_sandbox_env,
)

WARNING = (
    "Sandbox cancel smoke only. Lists live orders and cancels with explicit "
    "--confirm-sandbox-cancel. Does not submit new orders."
)


def _build_order_repo(settings, with_db: bool) -> Optional[OrderRepository]:
    if not with_db:
        return None
    from backend.db.session import configure_engine, get_db_session

    configure_engine(settings.database_url, sql_echo=settings.sql_echo)
    return OrderRepository(get_db_session())


def _preview_cancel(adapter: TastytradeSandboxAdapter, account_number: str, order_id: str) -> int:
    print(f"cancel_preview: order_id={order_id}")
    try:
        response = adapter.get_order(account_number, order_id)
        summary = summarize_order_response(response)
        print(format_order_status_summary(summary))
        print("info: re-run with --confirm-sandbox-cancel to cancel this order", file=sys.stderr)
        return 0
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
        else:
            print_auth_error(exc)
        return 1


def _update_db_cancel_status(
    repo: Optional[OrderRepository],
    order_id: str,
    *,
    success: bool,
    message: str = "",
    raw: Optional[dict] = None,
) -> None:
    if not repo:
        return
    if success:
        record = repo.mark_order_cancelled(broker_order_id=order_id, raw=raw)
    else:
        record = repo.mark_cancel_failed(
            broker_order_id=order_id,
            message=message or "Sandbox cancel failed",
            raw=raw,
        )
    if record:
        print_db_order_summary(repo, record.order_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tastytrade sandbox live-order list/cancel smoke")
    parser.add_argument("--order-id", default=None, help="Broker order id to preview/cancel")
    parser.add_argument(
        "--confirm-sandbox-cancel",
        action="store_true",
        help="Cancel the order specified by --order-id",
    )
    parser.add_argument("--with-db", action="store_true")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args(argv)

    if args.with_db and args.no_db:
        print("error: use either --with-db or --no-db, not both", file=sys.stderr)
        return 2

    if args.confirm_sandbox_cancel and not args.order_id:
        print("error: --confirm-sandbox-cancel requires --order-id", file=sys.stderr)
        return 2

    with_db = args.with_db and not args.no_db

    print(f"warning: {WARNING}")

    try:
        settings = validate_sandbox_env(script_name="smoke_tastytrade_sandbox_cancel")
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not print_env_check(settings):
        print("error: sandbox env check failed", file=sys.stderr)
        return 2

    adapter = TastytradeSandboxAdapter(settings)
    order_repo = _build_order_repo(settings, with_db)

    try:
        balance, _positions = fetch_account_state(adapter)
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
        else:
            print_auth_error(exc)
        return 1

    account_number = balance.get("account_number")
    print(f"selected_account: {account_number}")

    try:
        live_items = adapter.list_live_orders(account_number)
        summaries = summarize_live_orders_response({"data": {"items": live_items}})
        print(format_live_orders_summary(summaries))
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
        else:
            print_auth_error(exc)
        return 1

    if not args.order_id:
        return 0

    if not args.confirm_sandbox_cancel:
        return _preview_cancel(adapter, account_number, args.order_id)

    try:
        cancel_response = adapter.cancel_order(account_number, args.order_id)
        print(f"cancel_success: True")
        print(f"broker_order_id: {args.order_id}")
        _update_db_cancel_status(
            order_repo,
            args.order_id,
            success=True,
            raw={"route": "sandbox", "cancelled": True, "response": cancel_response},
        )
        return 0
    except (SandboxApiError, SandboxAuthError) as exc:
        if isinstance(exc, SandboxApiError):
            print_api_error(exc)
            message = exc.message
        else:
            print_auth_error(exc)
            message = str(exc)
        _update_db_cancel_status(
            order_repo,
            args.order_id,
            success=False,
            message=message,
            raw={"route": "sandbox", "cancelled": False},
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
