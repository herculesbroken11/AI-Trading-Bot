"""
DXLink protocol frames, COMPACT event parsing and per-symbol streaming quote state.

Read-only market data (Checkpoint 2.10). Freshness is strict: a quote older than
STREAM_MAX_QUOTE_AGE_SECONDS (default 1.0s) is stale and must never be traded on.
Nothing in this module trades; usable_for_trading is a diagnostic flag only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

DXLINK_PROTOCOL_VERSION = "0.1-DXF-JS/0.3.0"
DEFAULT_KEEPALIVE_TIMEOUT_SECONDS = 60
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 30.0
CONTROL_CHANNEL = 0
FEED_CHANNEL = 3
DEFAULT_AGGREGATION_PERIOD_SECONDS = 0.1
COMPACT_FORMAT = "COMPACT"

DEFAULT_STREAM_MAX_QUOTE_AGE_SECONDS = 1.0
DEFAULT_STREAM_SYMBOLS: Tuple[str, ...] = ("TNA", "TZA", "IWM", "SPY", "QQQ", "VIX")
# Index/volatility symbols have no tradable bid/ask; diagnostic only.
DIAGNOSTIC_ONLY_SYMBOLS = frozenset({"VIX"})

DEFAULT_EVENT_TYPES: Tuple[str, ...] = ("Quote", "Trade", "Summary")
DEFAULT_EVENT_FIELDS: Dict[str, List[str]] = {
    "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
    "Trade": ["eventType", "eventSymbol", "price", "dayVolume", "size"],
    "Summary": [
        "eventType",
        "eventSymbol",
        "dayOpenPrice",
        "dayHighPrice",
        "dayLowPrice",
        "prevDayClosePrice",
    ],
}


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def build_setup_frame(
    *,
    keepalive_timeout: int = DEFAULT_KEEPALIVE_TIMEOUT_SECONDS,
    version: str = DXLINK_PROTOCOL_VERSION,
) -> Dict[str, Any]:
    return {
        "type": "SETUP",
        "channel": CONTROL_CHANNEL,
        "version": version,
        "keepaliveTimeout": keepalive_timeout,
        "acceptKeepaliveTimeout": keepalive_timeout,
    }


def build_auth_frame(token: str) -> Dict[str, Any]:
    if not token:
        raise ValueError("quote token is required for DXLink AUTH")
    return {"type": "AUTH", "channel": CONTROL_CHANNEL, "token": token}


def build_keepalive_frame() -> Dict[str, Any]:
    return {"type": "KEEPALIVE", "channel": CONTROL_CHANNEL}


def build_channel_request_frame(channel: int = FEED_CHANNEL) -> Dict[str, Any]:
    return {
        "type": "CHANNEL_REQUEST",
        "channel": channel,
        "service": "FEED",
        "parameters": {"contract": "AUTO"},
    }


def build_feed_setup_frame(
    *,
    channel: int = FEED_CHANNEL,
    event_fields: Optional[Mapping[str, Sequence[str]]] = None,
    aggregation_period: float = DEFAULT_AGGREGATION_PERIOD_SECONDS,
) -> Dict[str, Any]:
    fields = event_fields or DEFAULT_EVENT_FIELDS
    return {
        "type": "FEED_SETUP",
        "channel": channel,
        "acceptAggregationPeriod": aggregation_period,
        "acceptDataFormat": COMPACT_FORMAT,
        "acceptEventFields": {name: list(values) for name, values in fields.items()},
    }


def build_feed_subscription_frame(
    symbols: Iterable[str],
    *,
    channel: int = FEED_CHANNEL,
    event_types: Iterable[str] = DEFAULT_EVENT_TYPES,
    reset: bool = True,
) -> Dict[str, Any]:
    types = list(event_types)
    add = [{"type": event_type, "symbol": symbol} for symbol in symbols for event_type in types]
    if not add:
        raise ValueError("at least one symbol/event type is required")
    return {"type": "FEED_SUBSCRIPTION", "channel": channel, "reset": reset, "add": add}


# ---------------------------------------------------------------------------
# COMPACT parsing
# ---------------------------------------------------------------------------


def parse_number(value: Any) -> Optional[float]:
    """DXLink sends numbers, 'NaN', 'Infinity' or null. Non-finite -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _rows(event_type: str, values: Sequence[Any], fields: Sequence[str]) -> List[Dict[str, Any]]:
    width = len(fields)
    if width == 0:
        return []
    events = []
    for start in range(0, len(values) - width + 1, width):
        event = dict(zip(fields, values[start : start + width]))
        event.setdefault("eventType", event_type)
        events.append(event)
    return events


def parse_compact_feed_data(
    data: Any,
    event_fields: Mapping[str, Sequence[str]],
) -> List[Dict[str, Any]]:
    """
    Parse FEED_DATA 'data' into event dicts.

    COMPACT: ["Quote", [row..., row...], "Trade", [...]] (rows flattened in
    FEED_CONFIG field order). Also accepts [["Quote", [...]], ...] and FULL
    format (list of dicts) defensively. Unknown event types are skipped.
    """
    if not isinstance(data, list) or not data:
        return []
    events: List[Dict[str, Any]] = []
    if all(isinstance(item, dict) for item in data):
        return [dict(item) for item in data if item.get("eventSymbol")]
    if isinstance(data[0], list):
        for item in data:
            events.extend(parse_compact_feed_data(item, event_fields))
        return events
    index = 0
    while index + 1 < len(data):
        event_type, values = data[index], data[index + 1]
        index += 2
        if not isinstance(event_type, str) or not isinstance(values, list):
            continue
        fields = event_fields.get(event_type)
        if fields:
            events.extend(_rows(event_type, values, fields))
    return events


# ---------------------------------------------------------------------------
# Per-symbol state
# ---------------------------------------------------------------------------


@dataclass
class SymbolStreamState:
    symbol: str
    diagnostic_only: bool = False
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last_price: Optional[float] = None
    last_size: Optional[float] = None
    day_volume: Optional[float] = None
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    prev_close: Optional[float] = None
    # First valid bid/ask mid seen this session (baseline for short-window momentum).
    first_mid: Optional[float] = None
    first_mid_received_at: Optional[float] = None
    quote_updates: int = 0
    trade_updates: int = 0
    summary_updates: int = 0
    last_event_received_at: Optional[float] = None
    last_quote_received_at: Optional[float] = None
    last_trade_received_at: Optional[float] = None
    last_summary_received_at: Optional[float] = None
    last_event_wall_time: Optional[datetime] = None
    last_event_type: Optional[str] = None
    event_types_seen: Set[str] = field(default_factory=set)
    is_realtime: bool = True
    age_samples: int = 0
    fresh_samples: int = 0
    age_sum: float = 0.0
    age_max: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2

    @property
    def total_updates(self) -> int:
        return self.quote_updates + self.trade_updates + self.summary_updates

    @property
    def has_bid_ask(self) -> bool:
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    def _freshness_reference(self) -> Optional[float]:
        # Tradable symbols are only as fresh as their last Quote; diagnostic
        # symbols (VIX) fall back to any event because they have no bid/ask.
        if self.diagnostic_only:
            return self.last_quote_received_at if self.has_bid_ask else self.last_event_received_at
        return self.last_quote_received_at

    def quote_age_seconds(self, now: float) -> Optional[float]:
        reference = self._freshness_reference()
        if reference is None:
            return None
        return max(0.0, now - reference)

    def is_stale(self, now: float, max_age_seconds: float) -> bool:
        age = self.quote_age_seconds(now)
        return age is None or age > max_age_seconds

    def usable_for_trading(self, now: float, max_age_seconds: float) -> Tuple[bool, str]:
        """Diagnostic only. Stale (> max age), one-sided or crossed quotes are never usable."""
        if self.diagnostic_only:
            return False, "volatility_diagnostic_only"
        if self.last_quote_received_at is None:
            return False, "no_quote"
        if self.is_stale(now, max_age_seconds):
            return False, "stale_quote"
        if not self.has_bid_ask:
            return False, "missing_bid_ask"
        if self.ask < self.bid:  # type: ignore[operator]
            return False, "crossed_market"
        return True, "ok"

    def record_age_sample(self, now: float, max_age_seconds: float) -> None:
        age = self.quote_age_seconds(now)
        if age is None:
            return
        self.age_samples += 1
        self.age_sum += age
        self.age_max = age if self.age_max is None else max(self.age_max, age)
        if age <= max_age_seconds:
            self.fresh_samples += 1

    @property
    def avg_age_seconds(self) -> Optional[float]:
        if self.age_samples == 0:
            return None
        return self.age_sum / self.age_samples

    def stayed_fresh(self, max_age_seconds: float) -> bool:
        return self.age_samples > 0 and self.age_max is not None and self.age_max <= max_age_seconds

    def apply(self, event: Mapping[str, Any], *, received_at: float, wall_time: Optional[datetime]) -> bool:
        event_type = str(event.get("eventType") or "")
        if event_type == "Quote":
            self.bid = parse_number(event.get("bidPrice"))
            self.ask = parse_number(event.get("askPrice"))
            self.bid_size = parse_number(event.get("bidSize"))
            self.ask_size = parse_number(event.get("askSize"))
            self.quote_updates += 1
            self.last_quote_received_at = received_at
            if self.first_mid is None and self.mid is not None:
                self.first_mid = self.mid
                self.first_mid_received_at = received_at
        elif event_type == "Trade":
            self.last_price = _keep(self.last_price, event.get("price"))
            self.last_size = _keep(self.last_size, event.get("size"))
            self.day_volume = _keep(self.day_volume, event.get("dayVolume"))
            self.trade_updates += 1
            self.last_trade_received_at = received_at
        elif event_type == "Summary":
            self.day_open = _keep(self.day_open, event.get("dayOpenPrice"))
            self.day_high = _keep(self.day_high, event.get("dayHighPrice"))
            self.day_low = _keep(self.day_low, event.get("dayLowPrice"))
            self.prev_close = _keep(self.prev_close, event.get("prevDayClosePrice"))
            self.summary_updates += 1
            self.last_summary_received_at = received_at
        else:
            return False
        self.last_event_received_at = received_at
        self.last_event_wall_time = wall_time
        self.last_event_type = event_type
        self.event_types_seen.add(event_type)
        return True

    def to_safe_dict(self, now: float, max_age_seconds: float) -> Dict[str, Any]:
        age = self.quote_age_seconds(now)
        usable, reason = self.usable_for_trading(now, max_age_seconds)
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "last_trade_price": self.last_price,
            "day_open": self.day_open,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "previous_close": self.prev_close,
            "volume": self.day_volume,
            "quote_updates": self.quote_updates,
            "trade_updates": self.trade_updates,
            "summary_updates": self.summary_updates,
            "event_types_seen": sorted(self.event_types_seen),
            "last_event_type": self.last_event_type,
            "last_event_received_at": (
                self.last_event_wall_time.isoformat() if self.last_event_wall_time else None
            ),
            "quote_age_seconds": None if age is None else round(age, 3),
            "max_quote_age_seconds": None if self.age_max is None else round(self.age_max, 3),
            "avg_quote_age_seconds": (
                None if self.avg_age_seconds is None else round(self.avg_age_seconds, 3)
            ),
            "fresh_sample_pct": (
                None
                if self.age_samples == 0
                else round(100.0 * self.fresh_samples / self.age_samples, 1)
            ),
            "is_realtime": self.is_realtime,
            "stale": self.is_stale(now, max_age_seconds),
            "stayed_fresh": self.stayed_fresh(max_age_seconds),
            "usable_for_trading": usable,
            "usable_reason": reason,
            "diagnostic_only": self.diagnostic_only,
        }


def _keep(current: Optional[float], raw: Any) -> Optional[float]:
    parsed = parse_number(raw)
    return current if parsed is None else parsed


class StreamState:
    """Latest streaming state for the subscribed symbols."""

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        diagnostic_only: Iterable[str] = DIAGNOSTIC_ONLY_SYMBOLS,
    ) -> None:
        diag = {s.upper() for s in diagnostic_only}
        self.symbols: Dict[str, SymbolStreamState] = {}
        for raw in symbols:
            symbol = raw.strip().upper()
            if symbol and symbol not in self.symbols:
                self.symbols[symbol] = SymbolStreamState(symbol, diagnostic_only=symbol in diag)
        self.events_applied = 0
        self.events_ignored = 0

    def __getitem__(self, symbol: str) -> SymbolStreamState:
        return self.symbols[symbol.upper()]

    def set_realtime(self, is_realtime: bool) -> None:
        for state in self.symbols.values():
            state.is_realtime = is_realtime

    def apply_event(
        self,
        event: Mapping[str, Any],
        *,
        received_at: float,
        wall_time: Optional[datetime] = None,
    ) -> bool:
        symbol = str(event.get("eventSymbol") or "").strip().upper()
        state = self.symbols.get(symbol)
        if state is None or not state.apply(event, received_at=received_at, wall_time=wall_time):
            self.events_ignored += 1
            return False
        self.events_applied += 1
        return True

    def sample(self, now: float, max_age_seconds: float) -> None:
        for state in self.symbols.values():
            state.record_age_sample(now, max_age_seconds)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass
class StreamVerdict:
    passed: bool
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    no_data: bool = False


def evaluate_stream_result(
    state: StreamState,
    *,
    now: float,
    max_age_seconds: float,
    require_all_fresh: bool = False,
) -> StreamVerdict:
    """
    Core (tradable) symbols must have received Quote updates and be fresh at the
    end of the window; with require_all_fresh they must never exceed max age.
    Diagnostic-only symbols (VIX) only produce warnings.
    """
    verdict = StreamVerdict(passed=True)
    if state.events_applied == 0:
        verdict.passed = False
        verdict.no_data = True
        verdict.failures.append("no streaming events received for any symbol (subscription failed?)")
        return verdict

    for symbol, sym in state.symbols.items():
        if sym.diagnostic_only:
            if sym.total_updates == 0:
                verdict.warnings.append(f"{symbol}: no events received (unsupported or no data); skipped")
            elif not sym.has_bid_ask:
                verdict.warnings.append(
                    f"{symbol}: no bid/ask; allowed as volatility diagnostic only"
                )
            continue
        if sym.quote_updates == 0:
            verdict.failures.append(f"{symbol}: no Quote updates received")
            continue
        age = sym.quote_age_seconds(now)
        if sym.is_stale(now, max_age_seconds):
            verdict.failures.append(
                f"{symbol}: stale at end of window (quote_age={age:.3f}s > {max_age_seconds:g}s); "
                "do not trade (market may be closed)"
            )
        elif require_all_fresh and not sym.stayed_fresh(max_age_seconds):
            verdict.failures.append(
                f"{symbol}: exceeded max age during window "
                f"(max_quote_age={sym.age_max or 0:.3f}s > {max_age_seconds:g}s)"
            )
        elif not sym.stayed_fresh(max_age_seconds):
            verdict.warnings.append(
                f"{symbol}: briefly stale during window "
                f"(max_quote_age={sym.age_max or 0:.3f}s > {max_age_seconds:g}s)"
            )
    verdict.passed = not verdict.failures
    return verdict
