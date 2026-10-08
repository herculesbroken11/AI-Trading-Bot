"""Quote-age checks for shadow warmup and preflight. No broker or order access."""

from __future__ import annotations

from typing import Mapping, Optional

from backend.signals.models import REQUIRED_SIGNAL_SYMBOLS

QUOTE_FRESH_SECONDS = 1.0
WARMUP_FRESH_SECONDS = 3.0
WARMUP_TIMEOUT_SECONDS = 30.0
PREFLIGHT_WAIT_SECONDS = 5.0
STALE_FEED_SECONDS = 20.0
STALE_FEED_MIN_SYMBOLS = 3


def quotes_are_fresh(ages: Mapping[str, Optional[float]], *, max_age_seconds: float = QUOTE_FRESH_SECONDS) -> bool:
    """Every required symbol has a quote no older than max_age_seconds."""
    for symbol in REQUIRED_SIGNAL_SYMBOLS:
        age = ages.get(symbol)
        if age is None or age > max_age_seconds:
            return False
    return True


def is_stale_feed(ages: Mapping[str, Optional[float]], *, stale_seconds: float = STALE_FEED_SECONDS) -> bool:
    """Several required symbols have been quiet for about 20 seconds or more."""
    stale = [
        symbol
        for symbol in REQUIRED_SIGNAL_SYMBOLS
        if ages.get(symbol) is not None and float(ages[symbol]) >= stale_seconds
    ]
    return len(stale) >= STALE_FEED_MIN_SYMBOLS
