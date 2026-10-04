#!/usr/bin/env python3
"""
Checkpoint 2.14 — Recommended shadow collection windows (prints guidance only).

Shows the current US equity session, the recommended collection windows and
example commands for an enforced shadow run and for analytics that exclude bad
session windows. It loads no credentials, makes no network calls and cannot
place, route or submit orders.

Exit codes:
  0 printed
  2 invalid arguments
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.market_session import (
    AVOID_NOTE,
    RECOMMENDED_SHADOW_WINDOWS,
    SessionConfigError,
    active_recommended_window,
    get_session_status,
    parse_session_now_override,
    to_eastern,
)
from backend.shadow_mode.runner import MAX_CYCLES

CONFIG_EXIT_CODE = 2
PYTHON = "python3.11"


def cycles_that_fit(window_minutes: float, seconds_per_cycle: float, pause_seconds: float) -> int:
    if seconds_per_cycle <= 0:
        return 0
    return max(0, min(MAX_CYCLES, math.floor((window_minutes * 60.0 + pause_seconds) / seconds_per_cycle)))


def shadow_command(
    run_id: str,
    *,
    cycles: int,
    signal_duration_seconds: float,
    followup_seconds: float,
    pause_seconds: float,
    min_minutes_before_close: float,
) -> str:
    return (
        f"{PYTHON} scripts/run_shadow_signal_logger.py --cycles {cycles} "
        f"--signal-duration-seconds {signal_duration_seconds:g} --followup-seconds {followup_seconds:g} "
        f"--pause-seconds {pause_seconds:g} --with-db --enforce-market-session "
        f"--min-minutes-before-close {min_minutes_before_close:g} --run-id {run_id}"
    )


def analytics_command(run_ids: Sequence[str]) -> str:
    return (
        f"{PYTHON} scripts/analyze_shadow_signal_logs.py --run-id {','.join(run_ids)} --limit 500 "
        "--exclude-bad-session-windows --simulate-thresholds --min-entry 60,65,70 --max-opposing 30,40,50 "
        "--min-score-gap 10,15,20 --min-followup-move-pct 0.03"
    )


def build_recommendations(
    now: datetime,
    *,
    signal_duration_seconds: float = 30.0,
    followup_seconds: float = 60.0,
    pause_seconds: float = 10.0,
    min_minutes_before_close: float = 20.0,
) -> Dict[str, Any]:
    status = get_session_status(now, near_close_minutes=min_minutes_before_close)
    eastern, _ = to_eastern(now)
    day = eastern.strftime("%Y-%m-%d")
    per_cycle = signal_duration_seconds + followup_seconds + pause_seconds
    active = active_recommended_window(now)

    windows: List[Dict[str, Any]] = []
    for window in RECOMMENDED_SHADOW_WINDOWS:
        cycles = cycles_that_fit(window.minutes(), per_cycle, pause_seconds)
        run_id = f"shadow-{day}-{window.name.replace('_', '-')}"
        windows.append(
            {
                "name": window.name,
                "window_et": window.label,
                "minutes": window.minutes(),
                "purpose": window.purpose,
                "suggested_cycles": cycles,
                "run_id": run_id,
                "command": shadow_command(
                    run_id,
                    cycles=max(cycles, 1),
                    signal_duration_seconds=signal_duration_seconds,
                    followup_seconds=followup_seconds,
                    pause_seconds=pause_seconds,
                    min_minutes_before_close=min_minutes_before_close,
                ),
                "active_now": active is not None and active.name == window.name,
            }
        )
    return {
        "session": status.to_dict(),
        "active_recommended_window": active.name if active else None,
        "seconds_per_cycle": per_cycle,
        "recommended_windows": windows,
        "avoid": AVOID_NOTE,
        "analytics_command": analytics_command([w["run_id"] for w in windows]),
        "report_command": f"{PYTHON} scripts/report_shadow_signal_logs.py --limit 500 --exclude-bad-session-windows",
        "orders_submitted": 0,
        "note": "guidance only; shadow runs never place orders and thresholds are unchanged",
    }


def _print(doc: Dict[str, Any]) -> None:
    s = doc["session"]
    print("--- recommended shadow collection windows (guidance only; no orders) ---")
    print(f"now_et: {s['now_eastern']}")
    print(f"session_label: {s['session_label']}  minutes_to_close: {s['session_minutes_to_close'] or 'n/a'}")
    print(f"active_recommended_window: {doc['active_recommended_window'] or 'none'}")
    print(f"seconds_per_cycle: {doc['seconds_per_cycle']:g} (signal + follow-up + pause)")
    for w in doc["recommended_windows"]:
        marker = "  <- now" if w["active_now"] else ""
        print(f"{w['window_et']}  {w['name']}: {w['purpose']} (fits ~{w['suggested_cycles']} cycles){marker}")
    print(f"avoid: {doc['avoid']}")
    print("--- example: safe enforced shadow run (one per window) ---")
    for w in doc["recommended_windows"]:
        print(w["command"])
    print("--- example: analytics excluding bad session windows ---")
    print(doc["analytics_command"])
    print(doc["report_command"])
    print(f"note: {doc['note']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--signal-duration-seconds", type=float, default=30.0)
    parser.add_argument("--followup-seconds", type=float, default=60.0)
    parser.add_argument("--pause-seconds", type=float, default=10.0)
    parser.add_argument("--min-minutes-before-close", type=float, default=20.0)
    parser.add_argument("--now-override", default=None, help="ISO datetime with offset (tests / planning)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.signal_duration_seconds <= 0 or args.followup_seconds < 0 or args.pause_seconds < 0:
        print("error: durations must be positive (follow-up / pause may be 0)")
        return CONFIG_EXIT_CODE
    try:
        now = parse_session_now_override(args.now_override) or datetime.now(timezone.utc)
        doc = build_recommendations(
            now,
            signal_duration_seconds=args.signal_duration_seconds,
            followup_seconds=args.followup_seconds,
            pause_seconds=args.pause_seconds,
            min_minutes_before_close=args.min_minutes_before_close,
        )
    except SessionConfigError as exc:
        print(f"error: {exc}")
        return CONFIG_EXIT_CODE
    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=True, default=str))
    else:
        _print(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
