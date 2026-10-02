#!/usr/bin/env python3
"""
Checkpoint 2.13 — Shadow analytics and offline threshold calibration (analytics only).

Reads shadow_signal_log rows (read-only), reports what actually happened, detects
flat/choppy market windows, compares runs and — with --simulate-thresholds —
replays stored snapshots through the Signal Engine under alternative SAFE
thresholds. Hypothetical results are never written back, never applied to live
or sandbox settings, and nothing here can place an order. Secrets are never printed.

Exit codes:
  0 analysis printed (also when there are no rows)
  2 configuration / database / unsafe arguments
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
from backend.config.tastytrade_urls import SANDBOX_BASE_URL
from backend.market_data.config import MarketDataConfigError, validate_execution_still_sandbox
from backend.shadow_mode.analytics import (
    DEFAULT_MIN_FOLLOWUP_MOVE_PCT,
    DIAGNOSTIC_LABEL,
    AnalyticsConfigError,
    analyze_rows,
    build_threshold_grid,
    compare_runs,
    load_rows,
    parse_float_list,
    simulate_thresholds,
)
from backend.shadow_mode.models import TRACKED_SYMBOLS
from backend.shadow_mode.runner import production_execution_block_checks

CONFIG_EXIT_CODE = 2
MAX_LIMIT = 10000

RowsLoader = Callable[[Settings, Optional[List[str]], int], Iterable[Any]]


def _default_rows_loader(settings: Settings, run_ids: Optional[List[str]], limit: int):
    from backend.repositories.shadow_signal_repository import open_shadow_repository

    repo = open_shadow_repository(settings.database_url, create_table=False, sql_echo=settings.sql_echo)
    return repo.list_signals(run_ids=run_ids, limit=limit)


def _num(value: Optional[float], fmt: str = "{:g}") -> str:
    return "n/a" if value is None else fmt.format(value)


def _error(message: str, json_output: bool, *, next_step: str = "") -> int:
    if json_output:
        print(json.dumps({"error": message, "next_step": next_step, "exit_code": CONFIG_EXIT_CODE}, indent=2))
    else:
        print(f"error: {message}")
        if next_step:
            print(f"next_step: {next_step}")
    return CONFIG_EXIT_CODE


def _print_analysis(analysis: Dict[str, Any]) -> None:
    print("--- actual logged decisions ---")
    print(f"total_cycles: {analysis['total_cycles']}")
    d = analysis["decisions"]
    print(f"bullish: {d['bullish']}  bearish: {d['bearish']}  skip: {d['skip']}")
    if analysis["skip_reasons"]:
        print("skip_reasons: " + ", ".join(f"{k}={v}" for k, v in analysis["skip_reasons"].items()))
    print(f"freshness_pass_pct: {_num(analysis['freshness_pass_pct'])}")
    ages = analysis["average_quote_age_seconds"]
    print("average_quote_age_seconds: " + " ".join(f"{s}={_num(ages.get(s), '{:.3f}')}" for s in TRACKED_SYMBOLS))
    after = analysis["after_skips"]
    print("--- movement after SKIP decisions ---")
    print(f"skips_with_followup: {after['skips_with_followup']}")
    print(f"avg_iwm_move_pct: {_num(after['avg_iwm_move_pct'], '{:+.4f}')}")
    print(f"avg_abs_iwm_move_pct: {_num(after['avg_abs_iwm_move_pct'], '{:.4f}')}")
    print(f"avg_tna_move_pct: {_num(after['avg_tna_move_pct'], '{:+.4f}')}")
    print(f"avg_tza_move_pct: {_num(after['avg_tza_move_pct'], '{:+.4f}')}")
    print(f"enough_movement_after_skip: {after['enough_movement_after_skip']}")
    window = analysis["market_window"]
    print("--- market window ---")
    print(f"market_window_status: {window['market_window_status']}")
    print(
        f"cycles_with_followup: {window['cycles_with_followup']}  flat_cycles: {window['flat_cycles']} "
        f"({_num(window['flat_pct'])}%)  enough_movement_cycles: {window['enough_movement_cycles']}  "
        f"direction_changes: {window['direction_changes']}"
    )
    print(f"min_followup_move_pct: {window['min_followup_move_pct']:g}")
    print(f"recommendation: {window['recommendation']}")
    profile = analysis["score_profile"]
    print("--- score profile (gate-passed cycles) ---")
    print(
        f"gate_passed_cycles: {profile['gate_passed_cycles']}  avg_bullish: {_num(profile['avg_bullish_score'])}  "
        f"avg_bearish: {_num(profile['avg_bearish_score'])}  max_bullish: {_num(profile['max_bullish_score'])}  "
        f"max_bearish: {_num(profile['max_bearish_score'])}"
    )
    print(
        f"avg_score_gap: {_num(profile['avg_score_gap'])}  near_miss_cycles (within 10 of entry "
        f"{_num(profile['stored_entry_threshold'])}): {profile['near_miss_cycles']}"
    )
    print(f"diagnosis: {analysis['diagnosis']}")


def _print_comparison(runs: List[Dict[str, Any]]) -> None:
    print("--- run comparison ---")
    for run in runs:
        d = run["decisions"]
        print(
            f"{run['run_id']}: cycles={run['cycles']} bull={d['bullish']} bear={d['bearish']} skip={d['skip']} "
            f"top_skip={run['top_skip_reason'] or '-'} fresh={_num(run['freshness_pass_pct'])}% "
            f"avg_abs_iwm={_num(run['avg_abs_iwm_move_pct'], '{:.4f}')}% window={run['market_window_status']} "
            f"near_miss={run['near_miss_cycles']}"
        )


def _print_simulation(sim: Dict[str, Any]) -> None:
    print("--- offline threshold simulation (hypothetical, diagnostic only) ---")
    print(f"rows_evaluated: {sim['rows_evaluated']}  sets_evaluated: {sim['sets_evaluated']}")
    print(f"min_followup_move_pct: {sim['min_followup_move_pct']:g}")
    for rejected in sim["rejected_unsafe_sets"]:
        print(
            f"rejected_unsafe: entry={rejected['entry_score_threshold']:g} "
            f"opposing={rejected['opposing_score_max']:g} gap={rejected['min_score_gap']:g} ({rejected['reason']})"
        )
    print(f"replay_mismatches_with_logged_thresholds: {sim['replay_mismatches_with_logged_thresholds']}")
    for r in sim["results"]:
        marker = " [current default]" if r["is_current_default"] else ""
        print(
            f"{r['label']}{marker}: bullish={r['bullish_count']} bearish={r['bearish_count']} "
            f"skip={r['skip_count']} scored={r['scored_count']} correct={r['direction_correct_count']} "
            f"incorrect={r['direction_incorrect_count']} correct_pct={_num(r['correct_pct'])}"
        )
    examples = [(r["label"], e) for r in sim["results"] for e in r["false_signal_examples"]]
    if examples:
        print("--- false signal examples (hypothetical) ---")
        for label, e in examples[:10]:
            print(
                f"[{label}] run={e['run_id']} cycle={e['cycle_number']} {e['hypothetical_symbol']} "
                f"({e['hypothetical_decision']}) bull={_num(e['bullish_score'])} bear={_num(e['bearish_score'])} "
                f"selected_move={_num(e['selected_move_pct'], '{:+.4f}')}% iwm_move={_num(e['iwm_move_pct'], '{:+.4f}')}%"
            )
    best = sim["best_candidate"]
    print("--- best candidate threshold set (DIAGNOSTIC ONLY — NOT APPLIED) ---")
    print(f"best_candidate: {best['label'] or 'none'}")
    if best.get("correct_pct") is not None:
        print(f"correct_pct: {best['correct_pct']:g}  scored_count: {best['scored_count']}")
    print(f"reason: {best['reason']}")
    if best.get("warning"):
        print(f"warning: {best['warning']}")
    print(f"note: {best['note']}")
    print("orders_submitted: 0  writes_to_database: false")


def run_analysis(
    settings: Settings,
    *,
    run_ids: Optional[List[str]] = None,
    limit: int = 500,
    json_output: bool = False,
    simulate: bool = False,
    min_entry: str = "60,65,70",
    max_opposing: str = "30,40,50",
    min_score_gap: str = "10,15,20",
    min_followup_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT,
    rows_loader: Optional[RowsLoader] = None,
) -> int:
    try:
        validate_execution_still_sandbox(settings)
    except MarketDataConfigError as exc:
        return _error(str(exc), json_output, next_step="execution must stay sandbox-only")
    checks = production_execution_block_checks()
    if not all(checks.values()):
        return _error(f"production order execution is not blocked: {checks}", json_output)
    if not 1 <= limit <= MAX_LIMIT:
        return _error(f"--limit must be in [1, {MAX_LIMIT}]", json_output)
    if not min_followup_move_pct > 0:
        return _error("--min-followup-move-pct must be > 0", json_output)

    grid, rejected = [], []
    if simulate:
        try:
            grid, rejected = build_threshold_grid(
                parse_float_list(min_entry, name="--min-entry"),
                parse_float_list(max_opposing, name="--max-opposing"),
                parse_float_list(min_score_gap, name="--min-score-gap"),
            )
        except AnalyticsConfigError as exc:
            return _error(str(exc), json_output, next_step="candidate thresholds cannot go below safe minimums")

    try:
        rows = load_rows((rows_loader or _default_rows_loader)(settings, run_ids, limit))
    except Exception as exc:
        message = str(exc) if "alembic upgrade" in str(exc) else f"database unavailable ({type(exc).__name__})"
        return _error(message, json_output, next_step="check DATABASE_URL; run: alembic upgrade head")

    warnings: List[str] = []
    found = {str(r.get("run_id")) for r in rows}
    for rid in run_ids or []:
        if rid not in found:
            warnings.append(f"run_id not found: {rid}")
    if len(rows) >= limit:
        warnings.append(f"row limit {limit} reached; older rows were not analysed")

    analysis = analyze_rows(rows, min_move_pct=min_followup_move_pct)
    comparison = compare_runs(rows, min_move_pct=min_followup_move_pct)
    simulation = (
        simulate_thresholds(
            rows,
            grid,
            min_move_pct=min_followup_move_pct,
            rejected=rejected,
            market_window_status=analysis["market_window"]["market_window_status"],
        )
        if simulate
        else None
    )

    execution = {
        "trading_mode": settings.trading_mode,
        "tastytrade_env": settings.tastytrade_env,
        "live_trading_enabled": bool(settings.live_trading_enabled),
        "order_execution_base_url": SANDBOX_BASE_URL,
        "production_order_execution": "blocked",
        "checks": checks,
    }
    if json_output:
        doc = {
            "analytics_only": True,
            "execution": execution,
            "run_ids": run_ids,
            "limit": limit,
            "analysis": analysis,
            "run_comparison": comparison,
            "threshold_simulation": simulation,
            "warnings": warnings,
            "note": DIAGNOSTIC_LABEL,
        }
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
        return 0

    print("--- shadow analytics (analytics only; no orders) ---")
    print(f"TRADING_MODE: {settings.trading_mode}  TASTYTRADE_ENV: {settings.tastytrade_env}  "
          f"LIVE_TRADING_ENABLED: {str(bool(settings.live_trading_enabled)).lower()}")
    print("production_order_execution: blocked")
    print(f"run_ids: {','.join(run_ids) if run_ids else 'latest rows (all runs)'}")
    print(f"limit: {limit}")
    for warning in warnings:
        print(f"warning: {warning}")
    _print_analysis(analysis)
    _print_comparison(comparison)
    if simulation is not None:
        _print_simulation(simulation)
    print(f"note: {DIAGNOSTIC_LABEL}")
    return 0


def _parse_run_ids(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return list(dict.fromkeys(ids)) or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", default=None, help="one run id or a comma-separated list to compare")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--simulate-thresholds", action="store_true")
    parser.add_argument("--min-entry", default="60,65,70")
    parser.add_argument("--max-opposing", default="30,40,50")
    parser.add_argument("--min-score-gap", default="10,15,20")
    parser.add_argument("--min-followup-move-pct", type=float, default=DEFAULT_MIN_FOLLOWUP_MOVE_PCT)
    args = parser.parse_args(argv)

    reset_settings_cache()
    try:
        settings = load_settings(env_path=_REPO_ROOT / ".env", override=True)
    except ConfigurationError as exc:
        return _error(str(exc), args.json)
    return run_analysis(
        settings,
        run_ids=_parse_run_ids(args.run_id),
        limit=args.limit,
        json_output=args.json,
        simulate=args.simulate_thresholds,
        min_entry=args.min_entry,
        max_opposing=args.max_opposing,
        min_score_gap=args.min_score_gap,
        min_followup_move_pct=args.min_followup_move_pct,
    )


if __name__ == "__main__":
    sys.exit(main())
