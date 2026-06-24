#!/usr/bin/env python3
"""Validate local .env shape for Tastytrade sandbox (no HTTP calls)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.sandbox_env import format_sandbox_env_report, sandbox_env_flags
from backend.config.settings import load_settings, reset_settings_cache


def main() -> int:
    reset_settings_cache()
    settings = load_settings()
    flags = sandbox_env_flags(settings)
    print(format_sandbox_env_report(flags))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
