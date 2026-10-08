#!/usr/bin/env python3
"""
Checkpoint 2.12 — Shadow signal log report (read-only DB query; no broker calls).

Summarises shadow_signal_log rows: decision counts, skip reasons, average
confidence and quote ages, simple follow-up direction stats, latest decisions.
Diagnostic only — not a profit calculation.

Checkpoint 2.14: breakdown by session_label, market_window_quality (good / weak /
bad) and --exclude-bad-session-windows.

Exit codes:
  0 report printed (also when there are no rows)
  2 configuration / database unavailable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.shadow_mode.models import TRACKED_SYMBOLS
from backend.shadow_mode.report import row_to_dict, summarize_shadow_logs
from backend.shadow_mode.session_quality import split_bad_session_windows

CONFIG_EXIT_CODE = 2
LATEST_IN_TEXT = 10

RowsLoader = Callable[[Settings, Optional[str], int], Iterable[Any]]


def _default_rows_loader(settings: Settings, run_id: Optional[str], limit: int):
    from backend.repositories.shadow_signal_repository import open_shadow_repository

    repo = open_shadow_repository(settings.database_url, create_table=False, sql_echo=settings.sql_echo)
    return repo.list_signals(run_id=run_id, limit=limit)


def _num(value: Optional[float], fmt: str = "{:g}") -> str:
    return "n/a" if value is None else fmt.format(value)


def _print_report(report: Dict[str, Any], *, run_id: Optional[str], limit: int) -> None:
    print("--- shadow signal log report (diagnostic only) ---")
    print(f"run_id: {run_id or 'all runs'}")
    print(f"limit: {limit}")
    print(f"total_signals: {report['total_signals']}")
    print(f"bullish_count: {report['bullish_count']}")
    print(f"bearish_count: {report['bearish_count']}")
    print(f"skip_count: {report['skip_count']}")
    print(f"stale_or_missing_data_skip_count: {report['stale_or_missing_data_skip_count']}")
    print(f"unclear_market_skip_count: {report['unclear_market_skip_count']}")
    if report["skip_reasons"]:
        print("skip_reasons: " + ", ".join(f"{k}={v}" for k, v in report["skip_reasons"].items()))
    print(f"average_confidence_all: {_num(report['average_confidence_all'])}")
    print(f"average_confidence_trade_signals: {_num(report['average_confidence_trade_signals'])}")
    print(f"stream_not_ready_count: {report['stream_not_ready_count']}")
    print(f"stale_followup_count: {report['stale_followup_count']}")
    print(f"high_volatility_skip_count: {report['high_volatility_skip_count']}")
    print(f"vix_penalty_count: {report['vix_penalty_count']}")
    print(f"vix_hard_block_count: {report['vix_hard_block_count']}")
    print(f"average_vix: {_num(report['average_vix'], '{:.3f}')}")
    print(f"max_vix: {_num(report['max_vix'], '{:.3f}')}")
    print(f"average_vix_change_pct: {_num(report['average_vix_change_pct'], '{:+.3f}')}")
    print("vix_blocked_examples:")
    if not report["vix_blocked_examples"]:
        print("  none")
    for example in report["vix_blocked_examples"]:
        print(
            f"  cycle={example.get('cycle_number')} score={_num(example.get('candidate_score'))} "
            f"vix={_num(example.get('vix_last'), '{:.3f}')} "
            f"change={_num(example.get('vix_change_pct'), '{:+.3f}')}% "
            f"skip={example.get('skip_reason') or '-'}"
        )
    print(f"freshness_gate_pass_pct: {_num(report['freshness_gate_pass_pct'])}")
    print(f"average_quote_age_seconds_overall: {_num(report['average_quote_age_seconds_overall'], '{:.3f}')}")
    print(f"valid_for_signal_evaluation: {str(report['valid_for_signal_evaluation']).lower()}")
    if report["evaluation_block_reasons"]:
        print("evaluation_block_reasons: " + "; ".join(report["evaluation_block_reasons"]))
    ages = report["average_quote_age_seconds"]
    print("average_quote_age_seconds: " + " ".join(f"{s}={_num(ages.get(s), '{:.3f}')}" for s in TRACKED_SYMBOLS))

    outcomes = report["outcomes"]
    print("--- follow-up outcomes (direction check, not P&L) ---")
    print(f"with_followup: {outcomes['with_followup']}")
    strict = report["strict_direction_outcome"]
    meaningful = report["meaningful_move_outcome"]
    print(
        f"strict_scored_count: {strict['scored']}  strict_correct: {strict['correct']}  "
        f"strict_incorrect: {strict['incorrect']}  strict_correct_pct: {_num(strict['correct_pct'])}"
    )
    print(
        f"meaningful_scored_count: {meaningful['scored']}  meaningful_correct: {meaningful['correct']}  "
        f"meaningful_incorrect: {meaningful['incorrect']}  meaningful_correct_pct: {_num(meaningful['correct_pct'])}"
    )
    print(
        f"scored: {outcomes['scored']}  correct: {outcomes['correct']}  incorrect: {outcomes['incorrect']}  "
        f"correct_pct: {_num(outcomes['correct_pct'])}"
    )
    for direction in ("bullish", "bearish"):
        stats = outcomes[direction]
        print(
            f"{direction}: scored={stats['scored']} correct={stats['correct']} "
            f"correct_pct={_num(stats['correct_pct'])} "
            f"avg_selected_move_pct={_num(stats['avg_selected_symbol_move_pct'], '{:+.4f}')} "
            f"avg_iwm_move_pct={_num(stats['avg_iwm_move_pct'], '{:+.4f}')}"
        )
    print(f"skip_avg_iwm_move_pct: {_num(outcomes['skip_avg_iwm_move_pct'], '{:+.4f}')}")
    print("--- session breakdown ---")
    for label, s in report["session_breakdown"].items():
        print(
            f"{label}: cycles={s['cycles']} fresh={_num(s['freshness_pass_pct'])}% "
            f"stale_skips={s['stale_skip_count']} avg_quote_age={_num(s['avg_quote_age_seconds'], '{:.3f}')}s "
            f"quality={s['window_quality']}"
        )
    quality = report["market_window_quality"]
    print(f"market_window_quality: {quality['market_window_quality']}")
    print("quality_reasons: " + "; ".join(quality["reasons"]))
    print(f"submitted_count: {report['submitted_count']}")
    print(f"production_execution_blocked_all: {str(report['production_execution_blocked_all']).lower()}")

    print(f"--- latest decisions (up to {LATEST_IN_TEXT}) ---")
    for d in report["latest_decisions"][:LATEST_IN_TEXT]:
        label = d.get("selected_symbol") or "SKIP"
        correct = d.get("direction_was_correct")
        correct_text = "n/a" if correct is None else str(correct).lower()
        print(
            f"{d.get('created_at')} run={d.get('run_id')} cycle={d.get('cycle_number')} "
            f"{label} ({d.get('decision')}) conf={_num(d.get('confidence_score'))} "
            f"skip={d.get('skip_reason') or '-'} correct={correct_text}"
        )
    print(f"note: {report['note']}")


def run_report(
    settings: Settings,
    *,
    run_id: Optional[str] = None,
    limit: int = 100,
    json_output: bool = False,
    exclude_bad_session_windows: bool = False,
    rows_loader: Optional[RowsLoader] = None,
) -> int:
    if limit < 1:
        return _error("--limit must be >= 1", json_output)
    try:
        rows = [row_to_dict(r) for r in (rows_loader or _default_rows_loader)(settings, run_id, limit)]
    except Exception as exc:
        message = str(exc) if "alembic upgrade" in str(exc) else f"database unavailable ({type(exc).__name__})"
        return _error(message, json_output, next_step="check DATABASE_URL; run: alembic upgrade head")

    rows_before = len(rows)
    excluded: List[Dict[str, Any]] = []
    if exclude_bad_session_windows:
        rows, excluded = split_bad_session_windows(rows)

    report = summarize_shadow_logs(rows, latest=limit)
    if json_output:
        doc = {
            "run_id": run_id,
            "limit": limit,
            "exclude_bad_session_windows": exclude_bad_session_windows,
            "rows_before_exclusion": rows_before,
            "excluded_session_windows": excluded,
            "report": report,
        }
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
    else:
        if exclude_bad_session_windows:
            print("--- excluded bad session windows ---")
            print(f"rows_before_exclusion: {rows_before}  rows_after_exclusion: {len(rows)}")
            if not excluded:
                print("excluded_windows: none")
            for w in excluded:
                print(
                    f"excluded: run={w['run_id']} session={w['session_label']} cycles={w['cycles']} "
                    f"fresh={_num(w['freshness_pass_pct'])}% reason={'; '.join(w['reasons'])}"
                )
        _print_report(report, run_id=run_id, limit=limit)
    return 0


def _error(message: str, json_output: bool, *, next_step: str = "") -> int:
    if json_output:
        print(json.dumps({"error": message, "next_step": next_step, "exit_code": CONFIG_EXIT_CODE}, indent=2))
    else:
        print(f"error: {message}")
        if next_step:
            print(f"next_step: {next_step}")
    return CONFIG_EXIT_CODE


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--exclude-bad-session-windows",
        action="store_true",
        help="drop after-hours / weekend / pre-market windows and windows with freshness < 70%%",
    )
    args = parser.parse_args(argv)

    reset_settings_cache()
    try:
        settings = load_settings(env_path=_REPO_ROOT / ".env", override=True)
    except ConfigurationError as exc:
        return _error(str(exc), args.json)
    return run_report(
        settings,
        run_id=args.run_id,
        limit=args.limit,
        json_output=args.json,
        exclude_bad_session_windows=args.exclude_bad_session_windows,
    )


if __name__ == "__main__":
    sys.exit(main())
