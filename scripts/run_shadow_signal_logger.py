#!/usr/bin/env python3
"""
Checkpoint 2.12 — Shadow Mode Signal Logger (observation only, no orders).

Runs a fixed number of read-only DXLink Signal Engine cycles (manual CLI run,
no daemon, no infinite loop). Each cycle logs one TNA / TZA / SKIP decision and,
optionally, the market movement over a follow-up window. Nothing here can
submit, route or place an order. Secrets are never printed.

Checkpoint 2.14 — market session guard: the session (weekend / pre_market /
regular_hours / near_close / after_hours) is always printed and logged per
cycle. By default it only warns; --enforce-market-session blocks poor windows.

Exit codes:
  0 run completed (any mix of TNA / TZA / SKIP decisions)
  1 auth / stream failure (or no cycle could collect data)
  2 configuration invalid
  3 rate limited
  4 blocked by the market session guard (--enforce-market-session)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_cooldown import RATE_LIMITED_EXIT_CODE
from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL
from backend.market_data.config import MarketDataConfig, MarketDataConfigError
from backend.market_data.dxlink_stream import DXLinkQuoteTokenProvider
from backend.market_session import (
    MarketSessionGuard,
    SessionConfigError,
    SessionGuardConfig,
    SessionGuardDecision,
    offset_clock,
    parse_session_now_override,
)
from backend.shadow_mode.logger import ShadowSignalLogger
from backend.shadow_mode.models import TRACKED_SYMBOLS, FollowupOutcome, ShadowCycleRecord
from backend.shadow_mode.report import summarize_shadow_logs
from backend.shadow_mode.runner import (
    ShadowModeRunner,
    ShadowRunConfig,
    new_run_id,
    production_execution_block_checks,
    validate_shadow_environment,
)
from backend.signals.dxlink_signal_source import DEFAULT_SIGNAL_SYMBOLS, StreamFactory
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine

CONFIG_EXIT_CODE = 2
SESSION_BLOCKED_EXIT_CODE = 4
COMPLETED = "shadow_signal_logger: completed"
FAILED = "shadow_signal_logger: failed"

TokenProviderFactory = Callable[[MarketDataConfig], DXLinkQuoteTokenProvider]
RepositoryFactory = Callable[[Settings], Any]


def _default_repository_factory(settings: Settings):
    from backend.repositories.shadow_signal_repository import open_shadow_repository

    return open_shadow_repository(settings.database_url, create_table=True, sql_echo=settings.sql_echo)


class _Output:
    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode
        self.document: Dict[str, Any] = {}

    def line(self, text: str = "") -> None:
        if not self.json_mode:
            print(text, flush=True)

    def finish(self, status: str, exit_code: int) -> int:
        if self.json_mode:
            self.document["shadow_signal_logger"] = status
            self.document["exit_code"] = exit_code
            print(json.dumps(self.document, indent=2, sort_keys=True, default=str))
        else:
            print(COMPLETED if status == "completed" else FAILED)
        return exit_code


def _fail(out: _Output, step: str, message: str, *, exit_code: int, next_step: str = "") -> int:
    out.line(f"error_step: {step}")
    out.line(f"error: {message}")
    if next_step:
        out.line(f"next_step: {next_step}")
    out.document["error"] = {"step": step, "message": message, "next_step": next_step}
    return out.finish("failed", exit_code)


def _num(value: Optional[float], fmt: str = "{:g}") -> str:
    return "n/a" if value is None else fmt.format(value)


def _print_record(out: _Output, record: ShadowCycleRecord) -> None:
    label = record.selected_symbol or "SKIP"
    out.line(
        f"decision: {label} ({record.decision}) confidence={record.confidence_score:g} "
        f"bullish={record.bullish_score:g} bearish={record.bearish_score:g}"
    )
    out.line(
        f"skip_reason: {record.skip_reason or 'n/a'}  market_regime: {record.market_regime}  "
        f"freshness_gate_passed: {str(record.freshness_gate_passed).lower()}"
    )
    out.line("quote_ages: " + " ".join(f"{s}={_num(record.quote_ages.get(s), '{:.3f}s')}" for s in TRACKED_SYMBOLS))
    out.line(
        "mids: "
        + " ".join(f"{s}={_num(record.mids.get(s), '{:.4f}')}" for s in TRACKED_SYMBOLS)
        + f" VIX={_num(record.vix_last)}"
    )
    quality = (json.loads(record.raw_score_json or "{}") or {}).get("quality") or {}
    if quality.get("evaluated"):
        out.line(
            f"quality: gate_passed={str(quality.get('quality_gate_passed')).lower()} "
            f"reason={quality.get('quality_gate_reason') or '-'} "
            f"continuation={_num(quality.get('continuation_score'))} confirmation={_num(quality.get('confirmation_score'))} "
            f"chop_risk={_num(quality.get('chop_risk_score'))} pullback_risk={_num(quality.get('pullback_risk_score'))}"
        )
    out.line(f"explanation: {record.explanation}")
    out.line(f"logged: {'db id=' + str(record.db_id) if record.db_id is not None else 'memory only'}")
    out.line("submitted: false (shadow mode never submits orders)")


def _bool(value: Optional[bool]) -> str:
    return "n/a" if value is None else str(value).lower()


def _print_session_status(
    out: _Output, decision: SessionGuardDecision, config: SessionGuardConfig, run_config: ShadowRunConfig
) -> None:
    status = decision.status
    out.line("--- market session (US equities, America/New_York) ---")
    out.line(f"session_now_et: {status.now_eastern.strftime('%Y-%m-%d %H:%M:%S %Z')} ({status.now_eastern.strftime('%A')})")
    out.line(f"session_label: {status.label.value}")
    out.line(f"session_is_regular_hours: {_bool(status.is_regular_hours)}")
    out.line(f"session_is_near_close: {_bool(status.is_near_close)}")
    out.line(f"session_minutes_to_close: {_num(status.minutes_to_close, '{:.1f}')}")
    out.line(f"min_minutes_before_close: {config.min_minutes_before_close:g}")
    out.line(f"seconds_needed_per_cycle: {run_config.seconds_per_cycle:g} (signal + follow-up + pause)")
    out.line(f"estimated_run_seconds: {run_config.estimated_run_seconds:g}")
    out.line(f"enforce_market_session: {_bool(config.enforce)}")
    out.line(f"allow_near_close: {_bool(config.allow_near_close)}  allow_after_hours: {_bool(config.allow_after_hours)}")
    out.line(f"session_guard: {decision.mode} ({decision.reason})")
    for warning in decision.warnings:
        out.line(f"warning: {warning}")
    if status.is_regular_hours and status.minutes_to_close is not None:
        usable = (status.minutes_to_close - config.min_minutes_before_close) * 60.0
        if run_config.estimated_run_seconds > usable:
            out.line(
                f"warning: estimated run ({run_config.estimated_run_seconds:g}s) is longer than the time before "
                f"the near-close buffer ({max(usable, 0):.0f}s); "
                + ("the guard will stop the run early" if config.enforce else "later cycles will be labelled near_close")
            )


def _print_followup(out: _Output, record: ShadowCycleRecord, outcome: FollowupOutcome) -> None:
    moves = []
    for symbol in TRACKED_SYMBOLS:
        before, after = record.mids.get(symbol), outcome.mids_after.get(symbol)
        moves.append(f"{symbol} {_num(before, '{:.4f}')}->{_num(after, '{:.4f}')}")
    out.line(f"followup ({outcome.followup_seconds:g}s): " + " | ".join(moves))
    correct = "n/a" if outcome.direction_was_correct is None else str(outcome.direction_was_correct).lower()
    out.line(
        f"selected_symbol_move_pct: {_num(outcome.selected_symbol_move_pct, '{:+.4f}%')}  "
        f"iwm_move_pct: {_num(outcome.iwm_move_pct, '{:+.4f}%')}  direction_was_correct: {correct}"
    )
    out.line(f"outcome_note: {outcome.outcome_note}")


def run_shadow(
    settings: Settings,
    *,
    cycles: int = 5,
    signal_duration_seconds: float = 30.0,
    pause_seconds: float = 10.0,
    followup_seconds: float = 60.0,
    max_age_seconds: Optional[float] = None,
    symbols: Sequence[str] = DEFAULT_SIGNAL_SYMBOLS,
    with_db: bool = False,
    json_output: bool = False,
    run_id: Optional[str] = None,
    token_provider_factory: Optional[TokenProviderFactory] = None,
    stream_factory: Optional[StreamFactory] = None,
    repository_factory: Optional[RepositoryFactory] = None,
    sleep: Optional[Callable[[float], None]] = None,
    enforce_market_session: bool = False,
    allow_near_close: bool = False,
    allow_after_hours: bool = False,
    min_minutes_before_close: float = 20.0,
    session_now_override: Optional[str] = None,
    session_clock: Optional[Callable[[], datetime]] = None,
) -> int:
    out = _Output(json_output)
    checks = production_execution_block_checks()
    execution = {
        "trading_mode": settings.trading_mode,
        "tastytrade_env": settings.tastytrade_env,
        "live_trading_enabled": bool(settings.live_trading_enabled),
        "order_execution_base_url": SANDBOX_BASE_URL,
        "production_order_execution": "blocked" if all(checks.values()) else "NOT_BLOCKED",
        "market_data_read_only": settings.market_data_read_only is True,
        "checks": checks,
    }
    out.document["execution"] = execution
    out.line("--- execution environment (must stay sandbox) ---")
    out.line(f"TRADING_MODE: {settings.trading_mode}")
    out.line(f"TASTYTRADE_ENV: {settings.tastytrade_env}")
    out.line(f"LIVE_TRADING_ENABLED: {str(bool(settings.live_trading_enabled)).lower()}")
    out.line(f"order_execution_base_url: {SANDBOX_BASE_URL}")
    out.line(f"production_order_execution: {execution['production_order_execution']}")
    out.line(f"market_data_read_only: {str(execution['market_data_read_only']).lower()}")
    out.line("order_submission: disabled (shadow mode)")

    try:
        config = validate_shadow_environment(settings)
    except MarketDataConfigError as exc:
        out.document["config_problems"] = list(exc.problems)
        for problem in exc.problems:
            out.line(f"problem: {problem}")
        return _fail(
            out,
            "config",
            "market-data / execution configuration is unsafe or incomplete",
            next_step="fix .env market-data settings; execution must stay sandbox",
            exit_code=CONFIG_EXIT_CODE,
        )
    except ConfigurationError as exc:
        return _fail(out, "config", str(exc), exit_code=CONFIG_EXIT_CODE)

    try:
        max_age = max_age_seconds if max_age_seconds is not None else settings.stream_max_quote_age_seconds
        engine = TnaTzaSignalEngine(SignalEngineConfig.from_settings(settings, max_quote_age_seconds=max_age))
        run_config = ShadowRunConfig(
            cycles=cycles,
            signal_duration_seconds=signal_duration_seconds,
            pause_seconds=pause_seconds,
            followup_seconds=followup_seconds,
            symbols=tuple(s.strip().upper() for s in symbols if s and s.strip()),
        ).validate()
    except ConfigurationError as exc:
        return _fail(out, "config", str(exc), exit_code=CONFIG_EXIT_CODE)

    try:
        session_config = SessionGuardConfig(
            enforce=enforce_market_session,
            allow_near_close=allow_near_close,
            allow_after_hours=allow_after_hours,
            min_minutes_before_close=min_minutes_before_close,
        ).validate()
        override = parse_session_now_override(session_now_override)
        clock = session_clock or (offset_clock(override, time.monotonic) if override else None)
        session_guard = MarketSessionGuard(session_config, clock=clock)
    except SessionConfigError as exc:
        return _fail(out, "config", str(exc), exit_code=CONFIG_EXIT_CODE)

    start_session = session_guard.check(run_config.seconds_per_cycle)
    out.document["session_guard"] = {**session_config.to_dict(), "session_now_override": override is not None}
    out.document["market_session"] = start_session.to_dict()
    out.document["regular_market_hours"] = start_session.status.is_regular_hours
    _print_session_status(out, start_session, session_config, run_config)
    if start_session.blocked:
        return _fail(
            out,
            "market_session",
            f"blocked by market session guard: {start_session.reason}",
            next_step=(
                "run during 09:45-10:30, 11:00-14:30 or 15:00-15:30 ET (see scripts/recommend_shadow_windows.py); "
                "use --allow-near-close / --allow-after-hours only to test those windows deliberately"
            ),
            exit_code=SESSION_BLOCKED_EXIT_CODE,
        )

    repository = None
    db_info: Dict[str, Any] = {"enabled": with_db}
    if with_db:
        from backend.repositories.shadow_signal_repository import safe_database_label

        db_info["database"] = safe_database_label(settings.database_url)
        try:
            repository = (repository_factory or _default_repository_factory)(settings)
        except Exception as exc:
            return _fail(
                out,
                "database",
                f"could not open shadow_signal_log ({type(exc).__name__})",
                next_step="check DATABASE_URL and run: alembic upgrade head",
                exit_code=CONFIG_EXIT_CODE,
            )
    signal_logger = ShadowSignalLogger(repository)

    resolved_run_id = run_id or new_run_id()
    out.document.update(
        {
            "market_data_config": config.safe_summary(),
            "engine_config": engine.config.to_dict(),
            "run_config": {
                "run_id": resolved_run_id,
                "cycles": run_config.cycles,
                "signal_duration_seconds": run_config.signal_duration_seconds,
                "pause_seconds": run_config.pause_seconds,
                "followup_seconds": run_config.followup_seconds,
                "max_quote_age_seconds": engine.config.max_quote_age_seconds,
                "symbols": list(run_config.symbols),
            },
            "db": db_info,
        }
    )
    out.line("--- market data config (production, read-only) ---")
    for key, value in config.safe_summary().items():
        out.line(f"{key}: {value}")
    out.line("--- shadow run ---")
    out.line(f"run_id: {resolved_run_id}")
    out.line(f"cycles: {run_config.cycles}")
    out.line(f"signal_duration_seconds: {run_config.signal_duration_seconds:g}")
    out.line(f"followup_seconds: {run_config.followup_seconds:g}")
    out.line(f"pause_seconds: {run_config.pause_seconds:g}")
    out.line(f"max_quote_age_seconds: {engine.config.max_quote_age_seconds:g}")
    out.line(f"symbols: {','.join(run_config.symbols)}")
    out.line(f"database_logging: {'enabled' if with_db else 'disabled (memory only)'}")
    if not start_session.status.is_regular_hours:
        out.line("warning: outside regular US market hours; expect stale-data SKIP decisions")

    def on_event(kind: str, payload: Dict[str, Any]) -> None:
        if kind == "cycle_start":
            out.line(f"--- cycle {payload['cycle']}/{payload['cycles']} ---")
        elif kind == "session":
            decision: SessionGuardDecision = payload["session"]
            out.line(
                f"session: {decision.status.label.value} "
                f"minutes_to_close={_num(decision.status.minutes_to_close, '{:.1f}')} "
                f"guard={decision.mode} ({decision.reason_code})"
            )
        elif kind == "session_blocked":
            out.line(f"session_guard: blocked before cycle {payload['cycle']}: {payload['session'].reason}")
        elif kind == "decision":
            _print_record(out, payload["record"])
        elif kind == "followup":
            _print_followup(out, payload["record"], payload["outcome"])
        elif kind == "cycle_error":
            error = payload["error"]
            out.line(f"cycle_error: step={error.step} reason={error.reason} fatal={str(error.fatal).lower()}")
            if not error.fatal:
                out.line("continuing to next cycle")
        elif kind == "pause":
            out.line(f"pausing {payload['seconds']:g}s before next cycle")

    runner_kwargs: Dict[str, Any] = {}
    if sleep is not None:
        runner_kwargs["sleep"] = sleep
    runner = ShadowModeRunner(
        config,
        engine,
        signal_logger,
        run_config,
        run_id=resolved_run_id,
        provider=(token_provider_factory or (lambda cfg: DXLinkQuoteTokenProvider(cfg)))(config),
        stream_factory=stream_factory,
        on_event=on_event,
        session_guard=session_guard,
        **runner_kwargs,
    )
    summary = runner.run()

    report = summarize_shadow_logs(summary.records, latest=0)
    out.document["run"] = summary.to_dict()
    out.document["cycles"] = [record.to_dict() for record in summary.records]
    out.document["summary"] = {k: v for k, v in report.items() if k != "latest_decisions"}
    out.document["warnings"] = list(signal_logger.warnings)

    out.line("--- shadow run summary ---")
    out.line(f"run_id: {summary.run_id}")
    out.line(f"cycles_requested: {summary.cycles_requested}")
    out.line(f"cycles_logged: {summary.cycles_completed}")
    out.line(
        f"bullish: {report['bullish_count']}  bearish: {report['bearish_count']}  skip: {report['skip_count']} "
        f"(stale/missing: {report['stale_or_missing_data_skip_count']}, unclear: {report['unclear_market_skip_count']})"
    )
    outcomes = report["outcomes"]
    out.line(
        f"direction_scored: {outcomes['scored']}  correct: {outcomes['correct']}  "
        f"incorrect: {outcomes['incorrect']}  correct_pct: {_num(outcomes['correct_pct'])}"
    )
    out.line(f"cycle_errors: {len(summary.errors)}")
    sessions: Dict[str, int] = {}
    for record in summary.records:
        sessions[record.session_label or "unknown"] = sessions.get(record.session_label or "unknown", 0) + 1
    out.document["summary"]["session_labels"] = sessions
    out.line("session_labels: " + (", ".join(f"{k}={v}" for k, v in sorted(sessions.items())) or "none"))
    if summary.session_blocked:
        out.line(f"warning: run stopped early by market session guard: {summary.session_block_reason}")
    for warning in signal_logger.warnings:
        out.line(f"warning: {warning}")
    out.line("orders_submitted: 0")

    if summary.aborted:
        reason = summary.abort_reason or "unknown"
        code = RATE_LIMITED_EXIT_CODE if reason == "rate_limited" else 1
        return _fail(
            out,
            "market_data",
            f"run stopped: {reason} (auth/rate-limit/guard failures are not retried)",
            next_step="wait for cooldown" if code == RATE_LIMITED_EXIT_CODE else "check market-data credentials",
            exit_code=code,
        )
    if summary.cycles_completed == 0 and summary.session_blocked:
        return _fail(
            out,
            "market_session",
            f"blocked by market session guard: {summary.session_block_reason}",
            exit_code=SESSION_BLOCKED_EXIT_CODE,
        )
    if summary.cycles_completed == 0:
        return _fail(out, "stream", "no cycle collected market data", exit_code=1)
    return out.finish("completed", 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--signal-duration-seconds", type=float, default=30.0)
    parser.add_argument("--pause-seconds", type=float, default=10.0)
    parser.add_argument("--followup-seconds", type=float, default=60.0, help="0 disables follow-up")
    parser.add_argument("--max-age-seconds", type=float, default=None)
    parser.add_argument("--no-vix", action="store_true")
    parser.add_argument("--with-db", action="store_true", help="persist to shadow_signal_log")
    parser.add_argument("--json", action="store_true", help="print one redacted JSON document only")
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--enforce-market-session",
        action="store_true",
        help="block weekends, pre-market, after-hours, near-close and cycles that cannot finish before the close",
    )
    parser.add_argument("--allow-near-close", action="store_true", help="with --enforce-market-session: allow near-close")
    parser.add_argument("--allow-after-hours", action="store_true", help="with --enforce-market-session: allow after-hours")
    parser.add_argument("--min-minutes-before-close", type=float, default=20.0)
    parser.add_argument(
        "--session-now-override",
        default=None,
        help="tests / dry checks only: ISO datetime with offset, e.g. 2026-10-02T11:00:00-04:00",
    )
    args = parser.parse_args(argv)

    env_path = _REPO_ROOT / ".env"
    if not args.json:
        print("--- shadow mode signal logger (observation only, no orders) ---")
    reset_settings_cache()
    try:
        settings = load_settings(env_path=env_path, override=True)
    except ConfigurationError as exc:
        return _fail(_Output(args.json), "load_settings", str(exc), exit_code=CONFIG_EXIT_CODE)

    symbols: List[str] = list(DEFAULT_SIGNAL_SYMBOLS)
    if args.no_vix:
        symbols = [s for s in symbols if s != "VIX"]
    return run_shadow(
        settings,
        cycles=args.cycles,
        signal_duration_seconds=args.signal_duration_seconds,
        pause_seconds=args.pause_seconds,
        followup_seconds=args.followup_seconds,
        max_age_seconds=args.max_age_seconds,
        symbols=symbols,
        with_db=args.with_db,
        json_output=args.json,
        run_id=args.run_id,
        enforce_market_session=args.enforce_market_session,
        allow_near_close=args.allow_near_close,
        allow_after_hours=args.allow_after_hours,
        min_minutes_before_close=args.min_minutes_before_close,
        session_now_override=args.session_now_override,
    )


if __name__ == "__main__":
    sys.exit(main())
