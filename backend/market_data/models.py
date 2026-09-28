"""Normalized quote snapshots and freshness diagnostics (read-only market data)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional

TASTYTRADE_PRODUCTION_REST_SOURCE = "tastytrade_production_rest"
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60.0

# Tastytrade REST quotes use dasherized keys; camelCase appears in some docs samples.
_KEYS: Dict[str, tuple] = {
    "symbol": ("symbol",),
    "instrument_type": ("instrument-type", "instrumentType"),
    "bid": ("bid", "bid-price", "bidPrice"),
    "ask": ("ask", "ask-price", "askPrice"),
    "mid": ("mid", "mid-price", "midPrice"),
    "mark": ("mark", "mark-price", "markPrice"),
    "last": ("last", "last-price", "lastPrice", "last-mkt", "lastMkt"),
    "open": ("open", "day-open", "dayOpen", "open-price", "openPrice"),
    "high": ("day-high-price", "dayHighPrice", "day-high", "dayHigh", "high"),
    "low": ("day-low-price", "dayLowPrice", "day-low", "dayLow", "low"),
    "previous_close": (
        "prev-close",
        "prevClose",
        "prev-day-close",
        "prevDayClose",
        "previous-close",
        "previousClose",
    ),
    "close": ("close", "close-price", "closePrice"),
    "volume": ("volume", "day-volume", "dayVolume"),
    "updated_at": ("updated-at", "updatedAt", "quote-time", "quoteTime"),
    "is_trading_halted": ("is-trading-halted", "isTradingHalted"),
}


def _first(item: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return None


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if not text or text.lower() in {"nan", "null", "none"}:
            return None
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse ISO-8601 strings or epoch seconds/milliseconds into aware UTC datetimes."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_timestamp(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class QuoteSnapshot:
    """One normalized quote. Diagnostic only — never an order instruction."""

    symbol: str
    instrument_type: Optional[str] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    mid: Optional[Decimal] = None
    mark: Optional[Decimal] = None
    last: Optional[Decimal] = None
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None
    close: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    updated_at: Optional[datetime] = None
    is_trading_halted: Optional[bool] = None
    source: str = TASTYTRADE_PRODUCTION_REST_SOURCE
    is_realtime: Optional[bool] = None
    realtime_basis: str = ""

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.updated_at is None:
            return None
        current = now or _utc_now()
        return (current - self.updated_at).total_seconds()

    def to_safe_dict(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        def num(value: Optional[Decimal]) -> Optional[str]:
            return None if value is None else str(value)

        age = self.age_seconds(now)
        return {
            "symbol": self.symbol,
            "instrument_type": self.instrument_type,
            "bid": num(self.bid),
            "ask": num(self.ask),
            "mid": num(self.mid),
            "mark": num(self.mark),
            "last": num(self.last),
            "open": num(self.open),
            "high": num(self.high),
            "low": num(self.low),
            "previous_close": num(self.previous_close),
            "volume": num(self.volume),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "quote_age_seconds": None if age is None else round(age, 3),
            "is_trading_halted": self.is_trading_halted,
            "source": self.source,
            "is_realtime": self.is_realtime,
            "realtime_basis": self.realtime_basis,
        }


def normalize_tastytrade_quote(
    item: Mapping[str, Any],
    *,
    source: str = TASTYTRADE_PRODUCTION_REST_SOURCE,
) -> QuoteSnapshot:
    """Map a Tastytrade /market-data/by-type item to a QuoteSnapshot."""
    symbol = str(_first(item, _KEYS["symbol"]) or "").strip().upper()
    if not symbol:
        raise ValueError("quote item has no symbol")

    bid = parse_decimal(_first(item, _KEYS["bid"]))
    ask = parse_decimal(_first(item, _KEYS["ask"]))
    mid = parse_decimal(_first(item, _KEYS["mid"]))
    if mid is None and bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2

    halted_raw = _first(item, _KEYS["is_trading_halted"])
    halted: Optional[bool]
    if isinstance(halted_raw, bool):
        halted = halted_raw
    elif isinstance(halted_raw, str) and halted_raw.lower() in {"true", "false"}:
        halted = halted_raw.lower() == "true"
    else:
        halted = None

    instrument_type = _first(item, _KEYS["instrument_type"])
    is_realtime: Optional[bool] = None
    basis = ""
    if source == TASTYTRADE_PRODUCTION_REST_SOURCE:
        # Tastytrade serves no delayed quotes over REST (funded accounts only).
        is_realtime = True
        basis = "tastytrade REST serves real-time quotes only (no delayed quotes)"

    return QuoteSnapshot(
        symbol=symbol,
        instrument_type=str(instrument_type) if instrument_type is not None else None,
        bid=bid,
        ask=ask,
        mid=mid,
        mark=parse_decimal(_first(item, _KEYS["mark"])),
        last=parse_decimal(_first(item, _KEYS["last"])),
        open=parse_decimal(_first(item, _KEYS["open"])),
        high=parse_decimal(_first(item, _KEYS["high"])),
        low=parse_decimal(_first(item, _KEYS["low"])),
        previous_close=parse_decimal(_first(item, _KEYS["previous_close"])),
        close=parse_decimal(_first(item, _KEYS["close"])),
        volume=parse_decimal(_first(item, _KEYS["volume"])),
        updated_at=parse_timestamp(_first(item, _KEYS["updated_at"])),
        is_trading_halted=halted,
        source=source,
        is_realtime=is_realtime,
        realtime_basis=basis,
    )


@dataclass(frozen=True)
class QuoteFreshness:
    symbol: str
    has_timestamp: bool
    age_seconds: Optional[float]
    max_age_seconds: float
    is_stale: bool
    warning: Optional[str] = None


def evaluate_quote_freshness(
    quote: QuoteSnapshot,
    *,
    now: Optional[datetime] = None,
    max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> QuoteFreshness:
    """Diagnostic freshness check. Missing timestamps are treated as stale."""
    age = quote.age_seconds(now)
    if age is None:
        return QuoteFreshness(
            symbol=quote.symbol,
            has_timestamp=False,
            age_seconds=None,
            max_age_seconds=max_age_seconds,
            is_stale=True,
            warning=f"{quote.symbol}: quote has no updated_at timestamp; freshness unknown",
        )
    if age < -5:
        return QuoteFreshness(
            symbol=quote.symbol,
            has_timestamp=True,
            age_seconds=age,
            max_age_seconds=max_age_seconds,
            is_stale=False,
            warning=(
                f"{quote.symbol}: quote timestamp is {abs(age):.1f}s in the future; "
                "check local clock"
            ),
        )
    if age > max_age_seconds:
        return QuoteFreshness(
            symbol=quote.symbol,
            has_timestamp=True,
            age_seconds=age,
            max_age_seconds=max_age_seconds,
            is_stale=True,
            warning=(
                f"{quote.symbol}: quote is stale ({age:.1f}s old > {max_age_seconds:.0f}s); "
                "do not trade on stale data (market may be closed)"
            ),
        )
    return QuoteFreshness(
        symbol=quote.symbol,
        has_timestamp=True,
        age_seconds=age,
        max_age_seconds=max_age_seconds,
        is_stale=False,
    )


def is_quote_usable_for_trading(
    quote: QuoteSnapshot,
    freshness: QuoteFreshness,
) -> tuple:
    """
    Diagnostic gate: (usable, reason). Nothing in Phase 2 trades on this yet,
    but stale, halted or one-sided quotes must never be acted on.
    """
    if freshness.is_stale:
        return False, "stale_quote"
    if quote.is_trading_halted:
        return False, "trading_halted"
    if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
        return False, "missing_bid_ask"
    if quote.ask < quote.bid:
        return False, "crossed_market"
    return True, "ok"


@dataclass
class MarketDataResult:
    """Outcome of one quote fetch."""

    quotes: Dict[str, QuoteSnapshot] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    unsupported: Dict[str, str] = field(default_factory=dict)
    requested: List[str] = field(default_factory=list)
    fetched_at: Optional[datetime] = None
