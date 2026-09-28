#!/usr/bin/env python3
"""Show sandbox throttle/cooldown state without calling Tastytrade."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_rate_limiter import (
    ENDPOINT_GROUPS,
    build_rate_limiter_from_env,
)
from backend.config.settings import load_settings, reset_settings_cache


def main(argv: list[str] | None = None) -> int:
    reset_settings_cache()
    try:
        load_settings()
    except Exception:
        pass
    limiter = build_rate_limiter_from_env()
    remaining = limiter.cooldown_remaining()
    print("--- sandbox rate limit status (no API call) ---")
    print(f"state_file: {limiter.state_path or 'disabled'}")
    print(f"cooldown_active: {str(remaining > 0).lower()}")
    print(f"cooldown_remaining_seconds: {int(round(remaining))}")
    for group in ENDPOINT_GROUPS:
        print(
            f"{group}: min_interval={limiter.interval_for(group):.0f}s "
            f"wait_now={limiter.wait_seconds_for(group):.0f}s"
        )
    if remaining > 0:
        print("warning: do NOT run sandbox scripts until cooldown_remaining_seconds reaches 0.")
        return 3
    print("ok: no active cooldown; scripts will still self-throttle per endpoint group.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
