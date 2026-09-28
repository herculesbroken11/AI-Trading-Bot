#!/usr/bin/env python3
"""List and optionally cancel live sandbox orders (explicit confirm required).

API budget per run (throttled by the shared sandbox rate limiter):
  list mode:            oauth + accounts + live_orders
  preview (--order-id): oauth + accounts + get_order
  exact cancel:         oauth + accounts + cancel + (delay) + one live_orders verification
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.oauth_diagnostics import oauth_next_step_hint
from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.sandbox_cooldown import RATE_LIMITED_EXIT_CODE, print_cooldown_advice
from backend.adapters.broker.sandbox_order_verification import (
    format_live_orders_summary,
    format_order_status_summary,
    partition_orders_for_cancel_list,
    summarize_live_orders_response,
    summarize_order_response,
)
from backend.adapters.broker.sandbox_rate_limiter import (
    get_sandbox_rate_limiter,
    rate_limit_info_from,
)
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError
from backend.repositories.order_repository import OrderRepository
from scripts.sandbox_smoke_common import (
    print_db_order_summary,
    print_env_check,
    print_sandbox_error,
    validate_sandbox_env,
)

WARNING = (
    "Sandbox cancel smoke only. Lists live orders and cancels with explicit "
    "--confirm-sandbox-cancel. Does not submit new orders."
)
LIST_COMMAND = "py -3.11 scripts/smoke_tastytrade_sandbox_cancel.py"
DEFAULT_VERIFY_DELAY_SECONDS = 10.0


def _build_order_repo(settings, with_db: bool) -> Optional[OrderRepository]:
    if not with_db:
        return None
    from backend.db.session import configure_engine, get_db_session

    configure_engine(settings.database_url, sql_echo=settings.sql_echo)
    return OrderRepository(get_db_session())


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


def _account_number_from(accounts: List[Dict[str, Any]]) -> Optional[str]:
    if not accounts:
        return None
    first = accounts[0]
    number = first.get("account-number") or first.get("account_number")
    if not number and isinstance(first.get("account"), dict):
        number = first["account"].get("account-number")
    return str(number) if number else None


def _oauth_and_account_preflight(
    adapter: TastytradeSandboxAdapter,
    *,
    next_command: str,
) -> tuple[Optional[str], int]:
    """Validate OAuth and resolve account before any cancel. Returns (account, exit_code)."""
    try:
        adapter._auth.ensure_authenticated()
    except SandboxAuthError as exc:
        code = print_sandbox_error(exc, next_command=next_command)
        print(
            "error: OAuth validation failed — cancel aborted before any cancel attempt",
            file=sys.stderr,
        )
        if code != RATE_LIMITED_EXIT_CODE:
            if exc.diagnostics:
                print(oauth_next_step_hint(exc.diagnostics), file=sys.stderr)
            else:
                print(
                    "Next step: run scripts/check_tastytrade_oauth.py and fix credentials.",
                    file=sys.stderr,
                )
        return None, code

    try:
        accounts = adapter.get_accounts()
    except (SandboxApiError, SandboxAuthError) as exc:
        code = print_sandbox_error(exc, next_command=next_command)
        print("error: account read failed — cancel aborted", file=sys.stderr)
        return None, code

    account_number = _account_number_from(accounts)
    if not account_number:
        print("error: no sandbox accounts returned — cancel aborted", file=sys.stderr)
        print(
            "Next step: confirm sandbox customer/account exists for this OAuth grant.",
            file=sys.stderr,
        )
        return None, 1
    return account_number, 0


def _list_orders(adapter: TastytradeSandboxAdapter, account_number: str) -> tuple[list, int]:
    try:
        live_items = adapter.list_live_orders(account_number)
    except (SandboxApiError, SandboxAuthError) as exc:
        return [], print_sandbox_error(exc, next_command=LIST_COMMAND)
    summaries = summarize_live_orders_response({"data": {"items": live_items}})
    print(format_live_orders_summary(summaries))
    return summaries, 0


def _preview_cancel(adapter: TastytradeSandboxAdapter, account_number: str, order_id: str) -> int:
    print(f"cancel_preview: order_id={order_id}")
    try:
        response = adapter.get_order(account_number, order_id)
    except (SandboxApiError, SandboxAuthError) as exc:
        return print_sandbox_error(exc, next_command=f"{LIST_COMMAND} --order-id {order_id}")
    summary = summarize_order_response(response)
    print(format_order_status_summary(summary))
    print(
        "info: re-run with --confirm-sandbox-cancel to cancel this order",
        file=sys.stderr,
    )
    return 0


def _verify_after_cancel(
    adapter: TastytradeSandboxAdapter,
    account_number: str,
    order_id: str,
    *,
    delay_seconds: float,
) -> int:
    if delay_seconds > 0:
        print(f"info: waiting {delay_seconds:.0f}s before post-cancel verification")
        get_sandbox_rate_limiter().pause(delay_seconds)

    try:
        live_items = adapter.list_live_orders(account_number)
    except (SandboxApiError, SandboxAuthError) as exc:
        info = rate_limit_info_from(exc)
        if info:
            print("cancel_attempted: true")
            print("post_cancel_verify: delayed_rate_limited")
            print(
                "info: cancel request was accepted; verification is delayed (not failed). "
                "Do not re-send the cancel."
            )
            print_cooldown_advice(info, next_command=LIST_COMMAND, stream=sys.stdout)
            return 0
        print_sandbox_error(exc, next_command=LIST_COMMAND)
        print("warning: post-cancel live order re-fetch failed", file=sys.stderr)
        return 1

    summaries = summarize_live_orders_response({"data": {"items": live_items}})
    print(format_live_orders_summary(summaries))
    active_live, _history = partition_orders_for_cancel_list(summaries)
    active_count = len(active_live)
    target_still_live = any(
        str(order.get("broker_order_id")) == str(order_id) for order in active_live
    )
    print(f"post_cancel_active_live_orders_count: {active_count}")
    print(f"target_order_still_live: {str(target_still_live).lower()}")
    if target_still_live:
        print(
            f"warning: order {order_id} is still Live after cancel; check again later "
            f"with: {LIST_COMMAND}",
            file=sys.stderr,
        )
        return 1
    if active_count == 0:
        print("post_cancel_verify: active live orders cleared")
    else:
        print(
            f"post_cancel_verify: target cleared; {active_count} other active live order(s) remain"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tastytrade sandbox live-order list/cancel smoke")
    parser.add_argument("--order-id", default=None, help="Broker order id to preview/cancel")
    parser.add_argument(
        "--confirm-sandbox-cancel",
        action="store_true",
        help="Cancel the order specified by --order-id",
    )
    parser.add_argument(
        "--verify-delay-seconds",
        type=float,
        default=DEFAULT_VERIFY_DELAY_SECONDS,
        help="Delay before the single post-cancel verification call",
    )
    parser.add_argument("--with-db", action="store_true", help="Update DB cancel status if matched")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args(argv)

    if args.with_db and args.no_db:
        print("error: use either --with-db or --no-db, not both", file=sys.stderr)
        return 2

    if args.confirm_sandbox_cancel and not args.order_id:
        print("error: --confirm-sandbox-cancel requires --order-id", file=sys.stderr)
        return 2

    with_db = bool(args.with_db) and not args.no_db

    print(f"warning: {WARNING}")

    try:
        settings = validate_sandbox_env(script_name="smoke_tastytrade_sandbox_cancel")
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not print_env_check(settings):
        print("error: sandbox env check failed", file=sys.stderr)
        return 2

    rerun_command = LIST_COMMAND
    if args.order_id:
        rerun_command = f"{LIST_COMMAND} --order-id {args.order_id}"
        if args.confirm_sandbox_cancel:
            rerun_command += " --confirm-sandbox-cancel"

    adapter = TastytradeSandboxAdapter(settings)
    account_number, preflight_code = _oauth_and_account_preflight(
        adapter,
        next_command=rerun_command,
    )
    if preflight_code != 0 or not account_number:
        return preflight_code

    print(f"selected_account: {account_number}")

    if not args.order_id:
        _summaries, list_code = _list_orders(adapter, account_number)
        return list_code

    if not args.confirm_sandbox_cancel:
        return _preview_cancel(adapter, account_number, args.order_id)

    order_repo = _build_order_repo(settings, with_db)
    try:
        cancel_response = adapter.cancel_order(account_number, args.order_id)
    except (SandboxApiError, SandboxAuthError) as exc:
        code = print_sandbox_error(exc, next_command=rerun_command)
        message = exc.message if isinstance(exc, SandboxApiError) else str(exc)
        _update_db_cancel_status(
            order_repo,
            args.order_id,
            success=False,
            message=message,
            raw={
                "route": "sandbox",
                "cancelled": False,
                "rate_limited": code == RATE_LIMITED_EXIT_CODE,
            },
        )
        return code

    print("cancel_success: True")
    print(f"broker_order_id: {args.order_id}")
    _update_db_cancel_status(
        order_repo,
        args.order_id,
        success=True,
        raw={"route": "sandbox", "cancelled": True, "response": cancel_response},
    )
    return _verify_after_cancel(
        adapter,
        account_number,
        args.order_id,
        delay_seconds=max(0.0, args.verify_delay_seconds),
    )


if __name__ == "__main__":
    raise SystemExit(main())
