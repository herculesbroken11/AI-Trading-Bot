"""Human-readable cooldown advice after a sandbox 429 (no secrets, no API calls)."""

from __future__ import annotations

import math
import sys
from typing import Optional, TextIO

from backend.adapters.broker.sandbox_rate_limiter import RateLimitInfo

RATE_LIMITED_EXIT_CODE = 3
COOLDOWN_STATUS_COMMAND = "py -3.11 scripts/sandbox_cooldown_status.py"


def recommended_wait_seconds(info: RateLimitInfo) -> int:
    return max(1, int(math.ceil(info.cooldown_seconds)))


def format_cooldown_advice(info: RateLimitInfo, *, next_command: Optional[str] = None) -> str:
    wait = recommended_wait_seconds(info)
    lines = [
        "--- sandbox rate limit ---",
        info.format_safe(),
        f"recommended_wait_seconds: {wait}",
        "warning: do NOT retry immediately — repeated calls extend the Tastytrade sandbox rate limit.",
        f"check_cooldown_command: {COOLDOWN_STATUS_COMMAND}",
    ]
    if next_command:
        lines.append(f"safe_next_command (after {wait}s): {next_command}")
    return "\n".join(lines)


def print_cooldown_advice(
    info: RateLimitInfo,
    *,
    next_command: Optional[str] = None,
    stream: Optional[TextIO] = None,
) -> None:
    print(format_cooldown_advice(info, next_command=next_command), file=stream or sys.stderr)
