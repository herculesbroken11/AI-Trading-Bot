"""Signal Engine v1 models (Checkpoint 2.11). Signal generation only — never orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from backend.market_data.stream_models import StreamState

REQUIRED_SIGNAL_SYMBOLS = ("TNA", "TZA", "IWM", "SPY", "QQQ")
VOLATILITY_SYMBOL = "VIX"


class SignalDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    SKIP = "skip"


SYMBOL_FOR_DIRECTION: Dict[SignalDirection, str] = {
    SignalDirection.BULLISH: "TNA",
    SignalDirection.BEARISH: "TZA",
}


class SignalReason(str, Enum):
    BULLISH_CONFIRMED = "bullish_confirmed"
    BEARISH_CONFIRMED = "bearish_confirmed"
    MISSING_REQUIRED_QUOTE = "missing_required_quote"
    STALE_MARKET_DATA = "stale_market_data"
    INVALID_QUOTE = "invalid_quote"
    WIDE_SPREAD = "wide_spread"
    INSUFFICIENT_REFERENCE_DATA = "insufficient_reference_data"
    HIGH_VOLATILITY = "high_volatility"
    BROAD_MARKET_DISAGREEMENT = "broad_market_disagreement"
    UNCLEAR_MARKET_DIRECTION = "unclear_market_direction"
    INSUFFICIENT_SIGNAL_STRENGTH = "insufficient_signal_strength"


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


@dataclass(frozen=True)
class SymbolQuote:
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    prev_close: Optional[float] = None
    volume: Optional[float] = None
    first_mid: Optional[float] = None
    quote_age_seconds: Optional[float] = None
    quote_updates: int = 0
    diagnostic_only: bool = False

    @property
    def has_quote(self) -> bool:
        return self.quote_updates > 0 or self.quote_age_seconds is not None

    @property
    def has_bid_ask(self) -> bool:
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    @property
    def mid(self) -> Optional[float]:
        return (self.bid + self.ask) / 2 if self.has_bid_ask else None  # type: ignore[operator]

    @property
    def price(self) -> Optional[float]:
        """Mid when a two-sided quote exists, otherwise last trade (VIX index value)."""
        return self.mid if self.mid is not None else self.last

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.ask - self.bid) / mid * 100.0  # type: ignore[operator]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "mid": _round(self.mid),
            "last": self.last,
            "day_open": self.day_open,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "prev_close": self.prev_close,
            "volume": self.volume,
            "first_mid": _round(self.first_mid),
            "spread_pct": _round(self.spread_pct),
            "quote_age_seconds": _round(self.quote_age_seconds, 3),
            "quote_updates": self.quote_updates,
            "diagnostic_only": self.diagnostic_only,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SymbolQuote":
        """Inverse of to_dict (derived fields mid/spread_pct are recomputed)."""

        def num(key: str) -> Optional[float]:
            value = data.get(key)
            return None if value is None else float(value)

        return cls(
            symbol=str(data["symbol"]).upper(),
            bid=num("bid"),
            ask=num("ask"),
            last=num("last"),
            day_open=num("day_open"),
            day_high=num("day_high"),
            day_low=num("day_low"),
            prev_close=num("prev_close"),
            volume=num("volume"),
            first_mid=num("first_mid"),
            quote_age_seconds=num("quote_age_seconds"),
            quote_updates=int(data.get("quote_updates") or 0),
            diagnostic_only=bool(data.get("diagnostic_only", False)),
        )


@dataclass
class MarketSnapshot:
    """Point-in-time view of the stream used for exactly one decision."""

    quotes: Dict[str, SymbolQuote]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "tastytrade_dxlink"

    def get(self, symbol: str) -> Optional[SymbolQuote]:
        return self.quotes.get(symbol.upper())

    @classmethod
    def from_stream_state(
        cls,
        state: StreamState,
        *,
        now: float,
        created_at: Optional[datetime] = None,
    ) -> "MarketSnapshot":
        quotes: Dict[str, SymbolQuote] = {}
        for symbol, sym in state.symbols.items():
            quotes[symbol] = SymbolQuote(
                symbol=symbol,
                bid=sym.bid,
                ask=sym.ask,
                last=sym.last_price,
                day_open=sym.day_open,
                day_high=sym.day_high,
                day_low=sym.day_low,
                prev_close=sym.prev_close,
                volume=sym.day_volume,
                first_mid=sym.first_mid,
                quote_age_seconds=sym.quote_age_seconds(now),
                quote_updates=sym.quote_updates if not sym.diagnostic_only else sym.total_updates,
                diagnostic_only=sym.diagnostic_only,
            )
        return cls(quotes=quotes, created_at=created_at or datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "quotes": {symbol: quote.to_dict() for symbol, quote in self.quotes.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MarketSnapshot":
        """Rebuild a stored snapshot (e.g. shadow_signal_log.raw_snapshot_json) for offline replay."""
        created_raw = data.get("created_at")
        created_at = datetime.fromisoformat(created_raw) if created_raw else datetime.now(timezone.utc)
        quotes = {
            str(symbol).upper(): SymbolQuote.from_dict({**quote, "symbol": quote.get("symbol", symbol)})
            for symbol, quote in (data.get("quotes") or {}).items()
        }
        return cls(quotes=quotes, created_at=created_at, source=str(data.get("source") or "stored"))


@dataclass(frozen=True)
class QuoteFreshnessStatus:
    symbol: str
    required: bool
    present: bool
    age_seconds: Optional[float]
    max_age_seconds: float
    fresh: bool
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "required": self.required,
            "present": self.present,
            "age_seconds": _round(self.age_seconds, 3),
            "max_age_seconds": self.max_age_seconds,
            "fresh": self.fresh,
            "note": self.note,
        }


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    bullish: float
    bearish: float
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "bullish": self.bullish, "bearish": self.bearish, "detail": self.detail}


@dataclass
class SignalScoreBreakdown:
    components: List[ScoreComponent] = field(default_factory=list)
    penalties: List[ScoreComponent] = field(default_factory=list)
    raw_bullish: float = 0.0
    raw_bearish: float = 0.0
    total_penalty: float = 0.0

    def add(self, name: str, bullish: float, bearish: float, detail: str) -> None:
        self.components.append(ScoreComponent(name, bullish, bearish, detail))
        self.raw_bullish += bullish
        self.raw_bearish += bearish

    def penalize(self, name: str, amount: float, detail: str) -> None:
        self.penalties.append(ScoreComponent(name, -amount, -amount, detail))
        self.total_penalty += amount

    @property
    def bullish(self) -> float:
        return max(0.0, min(100.0, self.raw_bullish - self.total_penalty))

    @property
    def bearish(self) -> float:
        return max(0.0, min(100.0, self.raw_bearish - self.total_penalty))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "components": [c.to_dict() for c in self.components],
            "penalties": [p.to_dict() for p in self.penalties],
            "raw_bullish": self.raw_bullish,
            "raw_bearish": self.raw_bearish,
            "total_penalty": self.total_penalty,
            "bullish_score": self.bullish,
            "bearish_score": self.bearish,
        }


@dataclass
class SignalDecision:
    """One TNA / TZA / SKIP decision. Diagnostic in this checkpoint — never an order."""

    decision: SignalDirection
    selected_symbol: Optional[str]
    confidence_score: float
    bullish_score: float
    bearish_score: float
    skip_reason: Optional[SignalReason]
    quote_freshness_by_symbol: Dict[str, QuoteFreshnessStatus]
    market_regime: str
    explanation: str
    created_at: datetime
    freshness_gate_passed: bool = False
    score_breakdown: Optional[SignalScoreBreakdown] = None
    warnings: List[str] = field(default_factory=list)
    thresholds: Mapping[str, float] = field(default_factory=dict)

    @property
    def is_trade_signal(self) -> bool:
        return self.decision in SYMBOL_FOR_DIRECTION and self.freshness_gate_passed

    @property
    def worker_signal(self) -> str:
        """Value accepted by SandboxBotWorker.run_cycle(signal=...)."""
        return self.decision.value if self.is_trade_signal else "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "selected_symbol": self.selected_symbol,
            "confidence_score": round(self.confidence_score, 2),
            "bullish_score": round(self.bullish_score, 2),
            "bearish_score": round(self.bearish_score, 2),
            "skip_reason": self.skip_reason.value if self.skip_reason else None,
            "quote_freshness_by_symbol": {
                symbol: status.to_dict() for symbol, status in self.quote_freshness_by_symbol.items()
            },
            "freshness_gate_passed": self.freshness_gate_passed,
            "market_regime": self.market_regime,
            "explanation": self.explanation,
            "created_at": self.created_at.isoformat(),
            "score_breakdown": self.score_breakdown.to_dict() if self.score_breakdown else None,
            "warnings": list(self.warnings),
            "thresholds": dict(self.thresholds),
        }
