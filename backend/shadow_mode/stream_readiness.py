"""Quote-age checks for shadow warmup and preflight. No broker or order access.

Warmup still uses the legacy 1-second fresh check. Cycle readiness uses
assess_stream_health(), which separates a quiet symbol from a stalled feed.
Signal-engine thresholds are not read or changed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from backend.signals.models import REQUIRED_SIGNAL_SYMBOLS

QUOTE_FRESH_SECONDS = 1.0
WARMUP_FRESH_SECONDS = 3.0
WARMUP_TIMEOUT_SECONDS = 30.0
PREFLIGHT_WAIT_SECONDS = 5.0
STALE_FEED_SECONDS = 20.0
STALE_FEED_MIN_SYMBOLS = 3
HARD_STALE_SECONDS = 5.0
QUIET_AFTER_SECONDS = 1.0

# Shadow validation only. Not signal-engine entry thresholds.
SYMBOL_FRESHNESS_POLICY: Dict[str, Tuple[float, float]] = {
    "IWM": (2.0, HARD_STALE_SECONDS),
    "SPY": (2.0, HARD_STALE_SECONDS),
    "QQQ": (2.0, HARD_STALE_SECONDS),
    "TNA": (3.0, HARD_STALE_SECONDS),
    "TZA": (3.0, HARD_STALE_SECONDS),
}


class StreamHealthState(str, Enum):
    HEALTHY = "HEALTHY"
    QUIET_SYMBOL = "QUIET_SYMBOL"
    DEGRADED = "DEGRADED"
    STALLED = "STALLED"


@dataclass(frozen=True)
class StreamHealth:
    state: StreamHealthState
    feed_event_age: Optional[float]
    quiet_symbols: Tuple[str, ...] = ()
    hard_stale_symbols: Tuple[str, ...] = ()
    degraded_symbols: Tuple[str, ...] = ()
    connected: bool = True
    cycle_blocked: bool = False
    evaluable: bool = True
    feed_reconnect_reason: Optional[str] = None
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, object]:
        return {
            "stream_health": self.state.value,
            "feed_event_age": self.feed_event_age,
            "quiet_symbols": list(self.quiet_symbols),
            "hard_stale_symbols": list(self.hard_stale_symbols),
            "degraded_symbols": list(self.degraded_symbols),
            "connected": self.connected,
            "cycle_blocked": self.cycle_blocked,
            "evaluable": self.evaluable,
            "feed_reconnect_reason": self.feed_reconnect_reason,
            "orders_submitted": 0,
            "writes_to_database": False,
            "analytics_only": True,
        }


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


def _policy(symbol: str, policies: Optional[Mapping[str, Tuple[float, float]]]) -> Tuple[float, float]:
    table = policies or SYMBOL_FRESHNESS_POLICY
    soft, hard = table.get(symbol, (2.0, HARD_STALE_SECONDS))
    return float(soft), float(hard)


def assess_stream_health(
    ages: Mapping[str, Optional[float]],
    *,
    connected: bool = True,
    policies: Optional[Mapping[str, Tuple[float, float]]] = None,
    stall_min_symbols: int = STALE_FEED_MIN_SYMBOLS,
    symbols: Sequence[str] = REQUIRED_SIGNAL_SYMBOLS,
) -> StreamHealth:
    """Classify feed liveness from quote ages. Missing prices are not invented.

    A symbol older than the legacy 1s mark, while other required symbols are
    still updating, is quiet or degraded. The cycle is blocked only for a hard
    stale required symbol, a stalled feed, or a disconnected socket.
    """
    known = [float(ages[symbol]) for symbol in symbols if ages.get(symbol) is not None]
    feed_event_age = min(known) if known else None
    if not connected:
        return StreamHealth(
            state=StreamHealthState.STALLED,
            feed_event_age=feed_event_age,
            connected=False,
            cycle_blocked=True,
            evaluable=False,
            feed_reconnect_reason="websocket_disconnected",
            warnings=("websocket disconnected",),
        )

    quiet: list[str] = []
    degraded: list[str] = []
    hard: list[str] = []
    missing: list[str] = []
    for symbol in symbols:
        age = ages.get(symbol)
        soft, hard_limit = _policy(symbol, policies)
        if age is None:
            missing.append(symbol)
            hard.append(symbol)
            continue
        age = float(age)
        if age > hard_limit:
            hard.append(symbol)
        elif age > soft:
            degraded.append(symbol)
        elif age > QUIET_AFTER_SECONDS:
            quiet.append(symbol)

    no_events = feed_event_age is None or len(missing) >= stall_min_symbols
    stalled = len(hard) >= stall_min_symbols or no_events
    if stalled:
        reason = "websocket_disconnected" if not connected else "feed_stalled"
        if no_events and not hard:
            reason = "no_market_events"
        return StreamHealth(
            state=StreamHealthState.STALLED,
            feed_event_age=feed_event_age,
            quiet_symbols=tuple(quiet),
            hard_stale_symbols=tuple(hard),
            degraded_symbols=tuple(degraded),
            cycle_blocked=True,
            evaluable=False,
            feed_reconnect_reason=reason,
            warnings=(reason,),
        )
    if hard:
        return StreamHealth(
            state=StreamHealthState.DEGRADED,
            feed_event_age=feed_event_age,
            quiet_symbols=tuple(quiet),
            hard_stale_symbols=tuple(hard),
            degraded_symbols=tuple(degraded),
            cycle_blocked=True,
            evaluable=False,
            warnings=("required symbol hard stale",),
        )
    if degraded:
        return StreamHealth(
            state=StreamHealthState.DEGRADED,
            feed_event_age=feed_event_age,
            quiet_symbols=tuple(quiet),
            degraded_symbols=tuple(degraded),
            cycle_blocked=False,
            evaluable=True,
        )
    if quiet:
        return StreamHealth(
            state=StreamHealthState.QUIET_SYMBOL,
            feed_event_age=feed_event_age,
            quiet_symbols=tuple(quiet),
            cycle_blocked=False,
            evaluable=True,
        )
    return StreamHealth(state=StreamHealthState.HEALTHY, feed_event_age=feed_event_age)


def summarize_liveness(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """Classify stored cycles from logged quote ages. Does not write rows."""
    counts = {state.value: 0 for state in StreamHealthState}
    classified = 0
    hard_stale_cycles = 0
    soft_fresh = 0
    hard_fresh = 0
    for row in rows:
        health = health_from_logged_ages(row)
        if health is None:
            continue
        classified += 1
        counts[health.state.value] += 1
        if health.hard_stale_symbols and health.state != StreamHealthState.STALLED:
            hard_stale_cycles += 1
        if health.state in (StreamHealthState.HEALTHY, StreamHealthState.QUIET_SYMBOL):
            soft_fresh += 1
        if health.state != StreamHealthState.STALLED and not health.hard_stale_symbols:
            hard_fresh += 1
    stalled = counts[StreamHealthState.STALLED.value]
    blocking = hard_stale_cycles + stalled
    valid = classified > 0 and (blocking / classified) * 100.0 <= 10.0
    return {
        "classified_cycles": classified,
        "healthy_cycles": counts[StreamHealthState.HEALTHY.value],
        "quiet_symbol_cycles": counts[StreamHealthState.QUIET_SYMBOL.value],
        "degraded_cycles": counts[StreamHealthState.DEGRADED.value],
        "stalled_cycles": stalled,
        "hard_stale_cycles": hard_stale_cycles,
        "feed_stalled_cycles": stalled,
        "soft_fresh_pct": None if not classified else round(soft_fresh / classified * 100.0, 1),
        "hard_fresh_pct": None if not classified else round(hard_fresh / classified * 100.0, 1),
        "new_liveness_valid_for_evaluation": valid,
        "orders_submitted": 0,
        "writes_to_database": False,
    }


def health_from_logged_ages(row: Mapping[str, object]) -> Optional[StreamHealth]:
    """Reclassify a stored cycle from the quote ages it already logged. Does not write."""
    ages = {symbol: row.get(f"quote_age_{symbol.lower()}") for symbol in REQUIRED_SIGNAL_SYMBOLS}
    if all(age is None for age in ages.values()):
        return None
    typed = {symbol: None if age is None else float(age) for symbol, age in ages.items()}
    return assess_stream_health(typed)
