#!/usr/bin/env python3
"""Close one small sandbox position (dry-run first, explicit confirm)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_auth import SandboxAuthError
from backend.adapters.broker.sandbox_order_verification import (
    extract_broker_order_id,
    find_equity_position,
    format_order_status_summary,
    position_quantity,
)
from backend.adapters.broker.tastytrade_sandbox import SandboxApiError, TastytradeSandboxAdapter
from backend.config.settings import ConfigurationError
from scripts.sandbox_smoke_common import (
    fetch_account_state,
    print_api_error,
    print_auth_error,
    print_env_check,
    validate_sandbox_env,
    verify_position_after_close,
)

WARNING = (
    "Sandbox close smoke only. Requires an existing long position. "
    "Does not use OrderExecutor or public trading routes."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tastytrade sandbox close-position smoke")
    parser.add_argument("--symbol", default="TNA", choices=["TNA", "TZA"])
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--order-type", default="Market", choices=["Market", "Limit"])
    parser.add_argument("--limit-price", type=float, default=None)
    parser.add_argument(
        "--confirm-sandbox-close",
        action="store_true",
        help="Submit sandbox close after dry-run passes",
    )
    args = parser.parse_args(argv)

    if args.quantity != 1:
        print("error: sandbox close quantity must be 1 in Phase 2", file=sys.stderr)
        return 2

    if args.order_type == "Limit" and args.limit_price is None:
        print("error: --limit-price is required when --order-type Limit", file=sys.stderr)
        return 2

    print(f"warning: {WARNING}")

    try:
        settings = validate_sandbox_env(script_name="smoke_tastytrade_sandbox_close")
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

    account_number = balance.get("account_number")
    position = find_equity_position(positions, args.symbol)
    if not position or position_quantity(position) < args.quantity:
        print(
            f"error: no open {args.symbol} position with qty >= {args.quantity}",
            file=sys.stderr,
        )
        return 1

    print(f"selected_account: {account_number}")
    print(f"open_position_qty: {position_quantity(position)}")

    order_type = args.order_type.strip().capitalize()
    try:
        adapter.dry_run_equity_close(
            account_number,
            args.symbol,
            args.quantity,
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

    if not args.confirm_sandbox_close:
        print("info: dry-run passed; re-run with --confirm-sandbox-close to submit", file=sys.stderr)
        return 0

    try:
        submit_response = adapter.submit_equity_close(
            account_number,
            args.symbol,
            args.quantity,
            order_type=order_type,
            limit_price=args.limit_price,
        )
    except SandboxApiError as exc:
        print_api_error(exc)
        return 1
    except SandboxAuthError as exc:
        print_auth_error(exc)
        return 1

    broker_order_id = extract_broker_order_id(submit_response)
    print(f"success: True")
    print(f"broker_order_id: {broker_order_id}")
    print(f"symbol: {args.symbol}")
    print(f"side: sell")
    print(f"quantity: {args.quantity}")
    print(f"trading_mode: sandbox")

    if broker_order_id:
        try:
            summary = adapter.fetch_order_status_summary(account_number, broker_order_id)
            print(format_order_status_summary(summary))
        except (SandboxApiError, SandboxAuthError) as exc:
            if isinstance(exc, SandboxApiError):
                print_api_error(exc)
            else:
                print_auth_error(exc)

    verify_position_after_close(adapter, account_number, args.symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
