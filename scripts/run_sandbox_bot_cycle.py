#!/usr/bin/env python3
"""Run one sandbox bot worker cycle from CLI (no continuous loop)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_cooldown import COOLDOWN_STATUS_COMMAND, RATE_LIMITED_EXIT_CODE
from backend.bot_worker.sandbox_worker import SandboxBotCycleResult, SandboxBotWorker
from backend.config.settings import ConfigurationError, reset_settings_cache
from backend.market_data.config import MarketDataConfigError, validate_market_data_settings
from backend.market_data.dxlink_stream import DXLinkQuoteTokenProvider
from backend.market_data.tastytrade_market_data import MarketDataError
from backend.signals.dxlink_signal_source import (
    DEFAULT_SIGNAL_SYMBOLS,
    build_pre_submit_check,
    collect_signal_from_dxlink,
)
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine
from scripts.sandbox_smoke_common import print_env_check, validate_sandbox_env

WARNING = (
    "Sandbox bot worker runs one cycle only. No continuous loop. "
    "Orders are dry-run by default; submit requires --confirm-sandbox-submit."
)

# Injection points for tests (DXLink mode only).
TOKEN_PROVIDER_FACTORY = lambda config: DXLinkQuoteTokenProvider(config)  # noqa: E731
STREAM_FACTORY = None


def _print_summary(result) -> None:
    summary = result.to_safe_summary()
    for key, value in summary.items():
        if key == "warnings" and not value:
            continue
        print(f"{key}: {value}")


def _print_signal(decision) -> None:
    print("--- dxlink signal decision (read-only market data) ---")
    print(f"decision: {decision.selected_symbol or 'SKIP'} ({decision.decision.value})")
    print(f"freshness_gate_passed: {str(decision.freshness_gate_passed).lower()}")
    print(f"confidence_score: {decision.confidence_score:g}")
    print(f"bullish_score: {decision.bullish_score:g}")
    print(f"bearish_score: {decision.bearish_score:g}")
    print(f"skip_reason: {decision.skip_reason.value if decision.skip_reason else 'n/a'}")
    print(f"market_regime: {decision.market_regime}")
    print(f"explanation: {decision.explanation}")
    for warning in decision.warnings:
        print(f"signal_warning: {warning}")


def _run_dxlink_cycle(args, settings, worker_factory) -> "tuple[Optional[SandboxBotCycleResult], int]":
    """Returns (result, exit_code_override). result None means exit with the override."""
    try:
        config = validate_market_data_settings(settings)
    except MarketDataConfigError as exc:
        for problem in exc.problems:
            print(f"problem: {problem}", file=sys.stderr)
        print("error: market-data configuration invalid for --signal-source dxlink", file=sys.stderr)
        return None, 2
    try:
        max_age = args.max_age_seconds if args.max_age_seconds is not None else settings.stream_max_quote_age_seconds
        engine = TnaTzaSignalEngine(SignalEngineConfig.from_settings(settings, max_quote_age_seconds=max_age))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 2

    provider = TOKEN_PROVIDER_FACTORY(config)
    try:
        signal_result = collect_signal_from_dxlink(
            config,
            engine=engine,
            symbols=DEFAULT_SIGNAL_SYMBOLS,
            duration_seconds=args.signal_duration_seconds,
            provider=provider,
            stream_factory=STREAM_FACTORY,
        )
    except MarketDataError as exc:
        print(exc.format_safe(), file=sys.stderr)
        if exc.reason == "rate_limited":
            return None, RATE_LIMITED_EXIT_CODE
        return None, 1

    decision = signal_result.decision
    _print_signal(decision)

    if not decision.is_trade_signal:
        reason = decision.skip_reason.value if decision.skip_reason else "skip"
        return (
            SandboxBotCycleResult(
                success=True,
                decision_status="skipped_no_signal",
                signal="none",
                message=f"Signal engine returned SKIP ({reason}); worker not invoked, no order",
            ),
            0,
        )

    selected = signal_result.snapshot.get(decision.selected_symbol or "")
    reference_price = selected.price if selected and selected.price else 50.0
    pre_submit_check = None
    if args.confirm_sandbox_submit:
        pre_submit_check = build_pre_submit_check(
            config,
            engine=engine,
            expected=decision.decision,
            provider=provider,
            symbols=DEFAULT_SIGNAL_SYMBOLS,
            stream_factory=STREAM_FACTORY,
        )
    worker = worker_factory()
    result = worker.run_cycle(
        signal=decision.worker_signal,
        order_type=args.order_type,
        limit_price=args.limit_price if args.order_type == "Limit" else None,
        reference_price=reference_price,
        confirm_submit=args.confirm_sandbox_submit,
        signal_source="dxlink",
        market_data_healthy=decision.freshness_gate_passed,
        pre_submit_check=pre_submit_check,
    )
    return result, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one sandbox bot worker cycle")
    parser.add_argument("--signal", default="none", choices=["bullish", "bearish", "none"])
    parser.add_argument(
        "--signal-source",
        default="manual",
        choices=["manual", "dxlink"],
        help="manual uses --signal; dxlink asks the read-only Signal Engine (TNA/TZA/SKIP)",
    )
    parser.add_argument("--signal-duration-seconds", type=float, default=30.0)
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=None,
        help="dxlink freshness gate (default: STREAM_MAX_QUOTE_AGE_SECONDS or 1.0)",
    )
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
    if args.signal_source == "dxlink" and args.signal != "none":
        print("error: --signal cannot be combined with --signal-source dxlink", file=sys.stderr)
        return 2
    if args.signal_source == "dxlink" and not 0 < args.signal_duration_seconds <= 300:
        print("error: --signal-duration-seconds must be between 0 and 300", file=sys.stderr)
        return 2

    print(f"warning: {WARNING}")
    print(f"signal_source: {args.signal_source}")

    try:
        reset_settings_cache()
        settings = validate_sandbox_env(script_name="run_sandbox_bot_cycle")
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not print_env_check(settings):
        print("error: sandbox env check failed", file=sys.stderr)
        return 2

    if args.signal_source == "dxlink":
        result, override = _run_dxlink_cycle(
            args,
            settings,
            lambda: SandboxBotWorker.from_settings(settings, with_db=args.with_db),
        )
        if result is None:
            return override
    else:
        worker = SandboxBotWorker.from_settings(settings, with_db=args.with_db)
        result = worker.run_cycle(
            signal=args.signal,
            order_type=args.order_type,
            limit_price=args.limit_price if args.order_type == "Limit" else None,
            confirm_submit=args.confirm_sandbox_submit,
        )
    _print_summary(result)

    if result.decision_status == "skipped_rate_limited":
        cooldown = result.cooldown_seconds or 60
        print(
            f"recommended_wait_seconds: {cooldown}\n"
            "warning: do NOT retry immediately — repeated calls extend the Tastytrade sandbox "
            "rate limit.\n"
            f"check_cooldown_command: {COOLDOWN_STATUS_COMMAND}\n"
            f"safe_next_command (after {cooldown}s): "
            f"py -3.11 scripts/run_sandbox_bot_cycle.py --signal {args.signal}",
            file=sys.stderr,
        )
        return RATE_LIMITED_EXIT_CODE

    if result.decision_status in {
        "skipped_no_signal",
        "skipped_active_live_order_exists",
        "skipped_position_exists",
        "skipped_oauth_unhealthy",
        "skipped_account_unavailable",
        "skipped_market_data_gate",
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
