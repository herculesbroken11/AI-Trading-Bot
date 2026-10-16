#!/usr/bin/env python3
"""
Checkpoint 2.22 — print a multi-session validation dataset (analytics only).

Rebuilds current balanced_v3_shadow candidates from stored shadow logs and
summarizes follow-up labels. Continuation and quality ranking stay labels.
Nothing is written back and no order is submitted.

Exit codes:
  0 report printed
  2 configuration / database / unsafe arguments
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.market_data.config import MarketDataConfigError, validate_execution_still_sandbox
from backend.shadow_mode.analytics import DEFAULT_MIN_FOLLOWUP_MOVE_PCT, load_rows
from backend.shadow_mode.runner import production_execution_block_checks
from backend.shadow_mode.semiconductor_context import semiconductor_report_lines
from backend.shadow_mode.validation_dataset import build_validation_dataset

CONFIG_EXIT_CODE = 2
MAX_LIMIT = 10000
RowsLoader = Callable[..., Iterable[Any]]


def _default_rows_loader(settings: Settings, run_ids: Optional[List[str]], limit: int, session_dates=None):
    from backend.repositories.shadow_signal_repository import open_shadow_repository

    repo = open_shadow_repository(settings.database_url, create_table=False, sql_echo=settings.sql_echo)
    return repo.list_signals(run_ids=run_ids, limit=limit, session_dates=session_dates)


def _num(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:g}"


def _error(message: str, json_output: bool) -> int:
    if json_output:
        print(json.dumps({"error": message, "exit_code": CONFIG_EXIT_CODE, "orders_submitted": 0, "writes_to_database": False}))
    else:
        print(f"error: {message}")
    return CONFIG_EXIT_CODE


def _parse_run_ids(raw: Optional[Any]) -> Optional[List[str]]:
    if not raw:
        return None
    parts = raw if isinstance(raw, (list, tuple)) else [raw]
    ids: List[str] = []
    for part in parts:
        for piece in str(part).split(","):
            text = piece.strip()
            if text and text not in ids:
                ids.append(text)
    return ids or None


def _print_accuracy(title: str, bucket: Mapping[str, Any]) -> None:
    print(
        f"{title}: count={bucket['count']} correct={bucket['correct']} "
        f"incorrect={bucket['incorrect']} accuracy={_num(bucket['accuracy'])}"
    )


def _print_groups(title: str, groups: Mapping[str, Any]) -> None:
    print(title)
    if not groups:
        print("  none")
        return
    for name, bucket in groups.items():
        _print_accuracy(f"  {name}", bucket)


def _print_filter(title: str, tally: Mapping[str, Any]) -> None:
    print(title)
    print(f"  accepted: {tally['accepted']}")
    print(f"  rejected: {tally['rejected']}")
    print(f"  winners_accepted: {tally['winners_accepted']}")
    print(f"  losers_accepted: {tally['losers_accepted']}")
    print(f"  winners_rejected: {tally['winners_rejected']}")
    print(f"  losers_rejected: {tally['losers_rejected']}")
    print(f"  retention_pct: {_num(tally['retention_pct'])}")


def print_validation_report(report: Mapping[str, Any]) -> None:
    print("=== validation dataset summary ===")
    print(f"runs: {report['runs']}")
    print(f"session_dates: {', '.join(report['session_dates']) if report['session_dates'] else 'n/a'}")
    print(f"total_cycles: {report['total_cycles']}")
    print(f"total_candidates: {report['total_candidates']}")
    print(f"labeled_candidates_30s: {report['labeled_candidates_30s']}")
    print(f"labeled_candidates_60s: {report['labeled_candidates_60s']}")
    print(f"labeled_candidates_120s: {report['labeled_candidates_120s']}")
    print(f"TNA candidates: {report['tna_candidates']}")
    print(f"TZA candidates: {report['tza_candidates']}")
    print("baseline accuracy:")
    for horizon in ("30s", "60s", "120s"):
        _print_accuracy(f"  {horizon}", report["baseline_accuracy"][horizon])
    groups = report["accuracy_by"]
    _print_groups("accuracy by candidate score band:", groups["candidate_score_band"])
    _print_groups("accuracy by signal reason:", groups["signal_reason"])
    _print_groups("accuracy by symbol:", groups["symbol"])
    _print_groups("accuracy by day part:", groups["day_part"])
    _print_groups("accuracy by VIX range:", groups["vix_range"])
    _print_groups("accuracy by momentum phase:", groups["momentum_phase"])
    _print_groups("accuracy by predictive probability band:", groups["predictive_probability_band"])
    _print_groups("accuracy by quality tier:", groups["quality_tier"])
    print("candidate_retention_by_filter:")
    filters = report["candidate_retention_by_filter"]
    _print_filter("predictive continuation:", filters["predictive_continuation"])
    _print_filter("quality ranking:", filters["quality_ranking"])
    print(f"minimum_labeled_candidates: {report['minimum_labeled_candidates']}")
    print(f"sample_size_status: {report['sample_size_status']}")
    print(f"generalization_status: {report['generalization_status']}")
    feed = report.get("feed_health") or {}
    print("feed_health:")
    print(f"  healthy_cycles: {feed.get('healthy_cycles', 0)}")
    print(f"  quiet_symbol_cycles: {feed.get('quiet_symbol_cycles', 0)}")
    print(f"  degraded_cycles: {feed.get('degraded_cycles', 0)}")
    print(f"  stalled_cycles: {feed.get('stalled_cycles', 0)}")
    print(f"legacy_freshness_pass_pct: {feed.get('legacy_freshness_pass_pct', 'n/a')}")
    print(f"soft_fresh_pct: {feed.get('soft_fresh_pct', 'n/a')}")
    print(f"hard_fresh_pct: {feed.get('hard_fresh_pct', 'n/a')}")
    print(f"new_liveness_valid_for_evaluation: {str(bool(feed.get('new_liveness_valid_for_evaluation'))).lower()}")
    for line in semiconductor_report_lines(report.get("semiconductor_context")):
        print(line)
    if report.get("excluded_run_ids"):
        print("excluded_run_ids: " + ",".join(report["excluded_run_ids"]))
    if report["sample_size_status"] == "insufficient":
        print("note: sample_size_status is insufficient; do not treat accuracy as a result")
    elif report["generalization_status"] == "insufficient_sessions":
        print("note: fewer than 3 trading dates; generalization_status stays insufficient_sessions")
    print("orders_submitted: 0")
    print("writes_to_database: false")
    print("analytics_only: true")


def run_report(
    settings: Settings,
    *,
    run_ids: Optional[List[str]] = None,
    limit: int = 10000,
    json_output: bool = False,
    min_followup_move_pct: float = DEFAULT_MIN_FOLLOWUP_MOVE_PCT,
    rows_loader: Optional[RowsLoader] = None,
    exclude_invalid: bool = True,
) -> int:
    try:
        validate_execution_still_sandbox(settings)
    except MarketDataConfigError as exc:
        return _error(str(exc), json_output)
    checks = production_execution_block_checks()
    if not all(checks.values()):
        return _error(f"production order execution is not blocked: {checks}", json_output)
    if not 1 <= limit <= MAX_LIMIT:
        return _error(f"--limit must be in [1, {MAX_LIMIT}]", json_output)
    if not min_followup_move_pct > 0:
        return _error("--min-followup-move-pct must be > 0", json_output)
    loader = rows_loader or _default_rows_loader
    try:
        parameters = inspect.signature(loader).parameters
        if "session_dates" in parameters:
            loaded = loader(settings, run_ids, limit, session_dates=None)
        else:
            loaded = loader(settings, run_ids, limit)
        rows = load_rows(loaded)
    except Exception as exc:
        message = str(exc) if "alembic upgrade" in str(exc) else f"database unavailable ({type(exc).__name__})"
        return _error(message, json_output)
    report = build_validation_dataset(rows, min_move_pct=min_followup_move_pct, exclude_invalid=exclude_invalid)
    report["orders_submitted"] = 0
    report["writes_to_database"] = False
    report["analytics_only"] = True
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    print_validation_report(report)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", action="append", default=None, help="one run id, repeated, or comma-separated")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-followup-move-pct", type=float, default=DEFAULT_MIN_FOLLOWUP_MOVE_PCT)
    parser.add_argument(
        "--include-invalid",
        action="store_true",
        help="keep after-hours and low-freshness runs in the dataset",
    )
    args = parser.parse_args(argv)
    reset_settings_cache()
    try:
        settings = load_settings(env_path=_REPO_ROOT / ".env", override=True)
    except ConfigurationError as exc:
        return _error(str(exc), args.json)
    return run_report(
        settings,
        run_ids=_parse_run_ids(args.run_id),
        limit=args.limit,
        json_output=args.json,
        min_followup_move_pct=args.min_followup_move_pct,
        exclude_invalid=not args.include_invalid,
    )


if __name__ == "__main__":
    sys.exit(main())
