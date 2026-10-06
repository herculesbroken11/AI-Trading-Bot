#!/usr/bin/env python3
"""
Checkpoint 2.15 — Replay stored shadow logs through the current Signal Engine (analytics only).

Compares the decision logged at the time (old engine) with what the v2 quality-gated
engine would decide on the same stored snapshot: old vs new TNA / TZA / SKIP counts,
false signals filtered, good signals preserved, missed winners and the hypothetical
correctness of the new decisions where follow-up data exists.

Read-only: new decisions are never written back to the database, no setting is
changed and nothing here can place an order. Secrets are never printed.

Exit codes:
  0 comparison printed (also when there are no rows)
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
from backend.shadow_mode.engine_replay import REPLAY_LABEL, compare_engine_versions
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


def _print_example(e: Dict[str, Any]) -> None:
    print(
        f"run={e['run_id']} cycle={e['cycle_number']} old={e['old_symbol'] or e['old_decision']} "
        f"(conf {_num(e['old_confidence'])}, correct={e['old_correct']}) -> new={e['new_decision']} "
        f"[{e['new_skip_reason'] or '-'}] continuation={_num(e['continuation_score'])} "
        f"confirmation={_num(e['confirmation_score'])} chop_risk={_num(e['chop_risk_score'])} "
        f"pullback_risk={_num(e['pullback_risk_score'])} iwm_window={_num(e['iwm_window_move_pct'], '{:+.4f}')}% "
        f"iwm_followup={_num(e['iwm_followup_move_pct'], '{:+.4f}')}% "
        f"expected_move_tag={e['expected_move_tag'] or '-'}"
    )


def _print_comparison(c: Dict[str, Any]) -> None:
    old, new = c["old"], c["new"]
    print(f"engine_version (new): {c['engine_version']}")
    print(
        f"rows_evaluated: {c['rows_evaluated']}  rows_replayed: {c['rows_replayed']}  "
        f"rows_not_replayed: {c['rows_not_replayed']}  min_followup_move_pct: {c['min_followup_move_pct']:g}"
    )
    print("--- old stored decisions ---")
    print(f"old_bullish: {old['bullish_count']}  old_bearish: {old['bearish_count']}  old_skip: {old['skip_count']}")
    print(
        f"old_correct: {old['correct_count']}/{old['scored_count']} ({_num(old['correct_pct'])}%)  "
        f"old_hypothetical_correct_pct: {_num(old['hypothetical_correct_pct'])}% "
        f"(scored {old['hypothetical_scored_count']})"
    )
    print("--- new decisions (hypothetical, not stored) ---")
    print(f"new_bullish: {new['bullish_count']}  new_bearish: {new['bearish_count']}  new_skip: {new['skip_count']}")
    print(
        f"new_hypothetical_correct: {new['correct_count']}/{new['scored_count']} "
        f"({_num(new['hypothetical_correct_pct'])}%)"
    )
    if new["skip_reasons"]:
        print("new_skip_reasons: " + ", ".join(f"{k}={v}" for k, v in new["skip_reasons"].items()))
    print("--- comparison ---")
    print(
        f"signals_filtered: {c['signals_filtered']}  false_signals_filtered: {c['false_signals_filtered']}  "
        f"good_signals_preserved: {c['good_signals_preserved']}  missed_winners: {c['missed_winners']}  "
        f"false_signals_kept: {c['false_signals_kept']}  new_signals_from_old_skips: {c['new_signals_from_old_skips']}"
    )
    if c["filtered_by_reason"]:
        print("filtered_by_reason: " + ", ".join(f"{k}={v}" for k, v in c["filtered_by_reason"].items()))
    em = c["expected_move_diagnostic"]
    print(
        f"expected_move_diagnostic: {em['tag']} tagged_rows={em['tagged_rows']} "
        f"tagged_new_trades={em['tagged_new_trades']} applied_to_decisions={str(em['applied_to_decisions']).lower()}"
    )
    print("--- examples: filtered false TNA signals ---")
    if not c["filtered_false_tna_examples"]:
        print("none")
    for e in c["filtered_false_tna_examples"]:
        _print_example(e)
    if c["missed_winner_examples"]:
        print("--- examples: missed winners ---")
        for e in c["missed_winner_examples"]:
            _print_example(e)
    print("orders_submitted: 0  writes_to_database: false")


def run_replay(
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
        warnings.append(f"row limit {limit} reached; older rows were not replayed")

    comparison = compare_engine_versions(rows, min_move_pct=min_followup_move_pct)
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
            "comparison": comparison,
            "warnings": warnings,
            "writes_to_database": False,
            "orders_submitted": 0,
            "note": REPLAY_LABEL,
        }
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
        return 0

    print("--- signal engine version replay (analytics only; no orders; no DB writes) ---")
    print(f"TRADING_MODE: {settings.trading_mode}  TASTYTRADE_ENV: {settings.tastytrade_env}  "
          f"LIVE_TRADING_ENABLED: {str(bool(settings.live_trading_enabled)).lower()}")
    print("production_order_execution: blocked")
    print(f"run_ids: {','.join(run_ids) if run_ids else 'latest rows (all runs)'}")
    print(f"limit: {limit}")
    for warning in warnings:
        print(f"warning: {warning}")
    _print_comparison(comparison)
    print(f"note: {REPLAY_LABEL}")
    return 0


def _parse_run_ids(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return list(dict.fromkeys(ids)) or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", default=None, help="one run id or a comma-separated list (combined replay)")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-followup-move-pct", type=float, default=DEFAULT_MIN_FOLLOWUP_MOVE_PCT)
    args = parser.parse_args(argv)

    reset_settings_cache()
    try:
        settings = load_settings(env_path=_REPO_ROOT / ".env", override=True)
    except ConfigurationError as exc:
        return _error(str(exc), args.json)
    return run_replay(
        settings,
        run_ids=_parse_run_ids(args.run_id),
        limit=args.limit,
        json_output=args.json,
        min_followup_move_pct=args.min_followup_move_pct,
    )


if __name__ == "__main__":
    sys.exit(main())
