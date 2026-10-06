#!/usr/bin/env python3
"""
Checkpoint 2.16 — Skip-opportunity analysis of stored shadow logs (DIAGNOSTIC ONLY).

Reads shadow_signal_log rows (read-only) and analyses SKIP rows only: what the
market did after each skip, which skip reasons blocked moves, missed bullish /
bearish opportunities, and whether broad_market_disagreement avoided bad trades
or blocked winners.

No DB writes, no orders, live thresholds and live signal logic unchanged.
Secrets are never printed.

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
from backend.shadow_mode.analytics import DEFAULT_MIN_FOLLOWUP_MOVE_PCT, load_rows
from backend.shadow_mode.runner import production_execution_block_checks
from backend.shadow_mode.skip_opportunity_analysis import DIAGNOSTIC_NOTE, analyze_skip_opportunities

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


def _print_groups(title: str, groups: Dict[str, Dict[str, Any]]) -> None:
    print(f"--- {title} ---")
    if not groups:
        print("none")
    for key, s in groups.items():
        print(
            f"{key}: skips={s['skips']} followup={s['with_followup']} bull={s['bullish']} bear={s['bearish']} "
            f"flat={s['flat']} mixed={s['mixed']} opportunity={_num(s['opportunity_pct'])}% "
            f"lean_validated={s['lean_validated']} lean_contradicted={s['lean_contradicted']} "
            f"avg_abs_iwm_followup={_num(s['avg_abs_iwm_followup_pct'], '{:.4f}')}%"
        )


def _print_example(e: Dict[str, Any]) -> None:
    line = (
        f"run={e['run_id']} cycle={e['cycle_number']} skip={e['skip_reason']} "
        f"bull={_num(e['bullish_score'])} bear={_num(e['bearish_score'])} followup={e['followup_class']} "
        f"iwm_after={_num(e['iwm_followup_move_pct'], '{:+.4f}')}% tna_after={_num(e['tna_followup_move_pct'], '{:+.4f}')}% "
        f"tza_after={_num(e['tza_followup_move_pct'], '{:+.4f}')}% iwm_window={_num(e['iwm_window_move_pct'], '{:+.4f}')}%"
    )
    broad = e.get("broad")
    if broad:
        line += (
            f" | iwm={broad['iwm_direction']} pair={broad['tna_tza_window_confirmation']} "
            f"spy_level={broad['spy_level_direction']} qqq_level={broad['qqq_level_direction']} "
            f"spy_window={broad['spy_window_direction']} qqq_window={broad['qqq_window_direction']} "
            f"outcome={broad['blocked_trade_outcome']}"
        )
    print(line)


def _print_examples(title: str, examples: List[Dict[str, Any]]) -> None:
    print(f"--- {title} ---")
    if not examples:
        print("none")
    for e in examples:
        _print_example(e)


def _print_analysis(a: Dict[str, Any]) -> None:
    print(f"total_skips: {a['total_skips']}  (of {a['total_rows']} rows)")
    print(f"skips_with_followup: {a['skips_with_followup']}  actionable_skips: {a['actionable_skips']}  "
          f"data_quality_skips_with_movement: {a['data_quality_skips_with_movement']}")
    print(f"min_followup_move_pct: {a['min_followup_move_pct']:g}")
    if a["skip_reasons"]:
        print("skip_reasons: " + ", ".join(f"{k}={v}" for k, v in a["skip_reasons"].items()))
    f = a["followup_direction"]
    print(
        f"followup_direction: bullish_followup={f['bullish_followup']} bearish_followup={f['bearish_followup']} "
        f"flat_followup={f['flat_followup']} mixed_followup={f['mixed_followup']} no_followup={f['no_followup']}"
    )
    _print_groups("opportunity by skip reason", a["opportunity_by_skip_reason"])
    _print_groups("opportunity by score profile (actionable skips; evidence lean / best score)", a["opportunity_by_score_profile"])
    _print_groups("opportunity by quality scores (actionable skips)", a["opportunity_by_quality_scores"])
    for side in ("bullish", "bearish"):
        m = a[f"missed_{side}_opportunities"]
        reasons = ", ".join(f"{k}={v}" for k, v in m["by_skip_reason"].items()) or "-"
        print(f"missed_{side}_opportunities: {m['count']}  evidence_leaned_same_way: {m['evidence_leaned_same_way']}  "
              f"by_skip_reason: {reasons}")
    v = a["virtual_candidates"]
    print(
        f"virtual_candidates (hindsight, diagnostic only): would_have_preferred_tna={v['would_have_preferred_tna']} "
        f"would_have_preferred_tza={v['would_have_preferred_tza']} would_have_skipped={v['would_have_skipped']}"
    )
    b = a["broad_market_disagreement"]
    print("--- broad_market_disagreement diagnostics ---")
    print(
        f"rows: {b['rows']}  with_followup: {b['with_followup']}  avoided_bad_trade: {b['avoided_bad_trade']}  "
        f"blocked_winner: {b['blocked_winner']}  flat_or_unscored: {b['flat_or_unscored']}"
    )
    print(
        f"iwm_followup_validated_iwm_direction: {b['iwm_followup_validated_iwm_direction']}  "
        f"contradicted: {b['iwm_followup_contradicted_iwm_direction']}  "
        f"etf_followup_validated: {b['etf_followup_validated']}  contradicted: {b['etf_followup_contradicted']}"
    )
    pair = ", ".join(f"{k}={v}" for k, v in b["tna_tza_window_confirmation"].items())
    print(f"spy_qqq_also_disagreed_in_window: {b['spy_qqq_also_disagreed_in_window']}  tna_tza_window_confirmation: {pair}")
    _print_examples("examples: broad_market_disagreement rows that later moved enough", a["broad_market_disagreement_moved_examples"])
    _print_examples("examples: insufficient_signal_strength rows that later moved enough", a["insufficient_signal_strength_moved_examples"])
    _print_examples("examples: SKIP was correct (follow-up flat/choppy)", a["correct_skip_examples"])
    print("orders_submitted: 0  writes_to_database: false")


def run_skip_analysis(
    settings: Settings,
    *,
    run_ids: Optional[List[str]] = None,
    limit: int = 500,
    json_output: bool = False,
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

    analysis = analyze_skip_opportunities(rows, min_move_pct=min_followup_move_pct)
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
            "diagnostic_only": True,
            "execution": execution,
            "run_ids": run_ids,
            "limit": limit,
            "skip_opportunities": analysis,
            "warnings": warnings,
            "writes_to_database": False,
            "orders_submitted": 0,
            "live_thresholds_unchanged": True,
            "live_signal_logic_unchanged": True,
            "note": DIAGNOSTIC_NOTE,
        }
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
        return 0

    print("--- skip opportunity analysis (DIAGNOSTIC ONLY; no DB writes; no orders) ---")
    print("live thresholds unchanged (entry=70 opposing=40 gap=20); live signal logic unchanged")
    print(f"TRADING_MODE: {settings.trading_mode}  TASTYTRADE_ENV: {settings.tastytrade_env}  "
          f"LIVE_TRADING_ENABLED: {str(bool(settings.live_trading_enabled)).lower()}")
    print("production_order_execution: blocked")
    print(f"run_ids: {','.join(run_ids) if run_ids else 'latest rows (all runs)'}")
    print(f"limit: {limit}")
    for warning in warnings:
        print(f"warning: {warning}")
    _print_analysis(analysis)
    print(f"note: {DIAGNOSTIC_NOTE}")
    return 0


def _parse_run_ids(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return list(dict.fromkeys(ids)) or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", default=None, help="one run id or a comma-separated list (combined analysis)")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--min-followup-move-pct", type=float, default=DEFAULT_MIN_FOLLOWUP_MOVE_PCT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    reset_settings_cache()
    try:
        settings = load_settings(env_path=_REPO_ROOT / ".env", override=True)
    except ConfigurationError as exc:
        return _error(str(exc), args.json)
    return run_skip_analysis(
        settings,
        run_ids=_parse_run_ids(args.run_id),
        limit=args.limit,
        json_output=args.json,
        min_followup_move_pct=args.min_followup_move_pct,
    )


if __name__ == "__main__":
    sys.exit(main())
