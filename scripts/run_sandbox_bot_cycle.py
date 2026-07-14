#!/usr/bin/env python3
"""Run one sandbox bot worker cycle from CLI (no continuous loop)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.bot_worker.sandbox_worker import SandboxBotWorker
from backend.config.settings import ConfigurationError, reset_settings_cache
from scripts.sandbox_smoke_common import print_env_check, validate_sandbox_env

WARNING = (
    "Sandbox bot worker runs one cycle only. No continuous loop. "
    "Orders are dry-run by default; submit requires --confirm-sandbox-submit."
)


def _print_summary(result) -> None:
    summary = result.to_safe_summary()
    for key, value in summary.items():
        if key == "warnings" and not value:
            continue
        print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one sandbox bot worker cycle")
    parser.add_argument("--signal", default="none", choices=["bullish", "bearish", "none"])
    parser.add_argument("--order-type", default="Limit", choices=["Limit", "Market"])
    parser.add_argument("--limit-price", type=float, default=2.0)
    parser.add_argument("--with-db", action="store_true")
    parser.add_argument(
        "--confirm-sandbox-submit",
        action="store_true",
        help="Submit sandbox order after dry-run passes",
    )
    args = parser.parse_args(argv)

    if args.order_type == "Limit" and args.limit_price is None:
        print("error: --limit-price is required when --order-type Limit", file=sys.stderr)
        return 2

    print(f"warning: {WARNING}")

    try:
        reset_settings_cache()
        settings = validate_sandbox_env(script_name="run_sandbox_bot_cycle")
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not print_env_check(settings):
        print("error: sandbox env check failed", file=sys.stderr)
        return 2

    worker = SandboxBotWorker.from_settings(settings, with_db=args.with_db)
    result = worker.run_cycle(
        signal=args.signal,
        order_type=args.order_type,
        limit_price=args.limit_price if args.order_type == "Limit" else None,
        confirm_submit=args.confirm_sandbox_submit,
    )
    _print_summary(result)

    if result.decision_status in {
        "skipped_no_signal",
        "skipped_live_order_exists",
        "skipped_position_exists",
        "dry_run_passed",
        "submitted",
    }:
        return 0 if result.success else 1
    if result.decision_status in {
        "skipped_emergency_halt",
        "skipped_bot_running",
        "skipped_active_trade_exists",
        "risk_rejected",
        "dry_run_failed",
        "submit_failed",
        "configuration_error",
        "sandbox_error",
        "error",
        "invalid_signal",
        "invalid_order",
    }:
        return 1 if not result.success else 0
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
