"""
DXLink -> Signal Engine glue (read-only). Never imports execution code.

collect_signal_from_dxlink() streams for a window and makes exactly one decision
at the end. build_pre_submit_check() re-collects a short fresh window right
before a (confirmed) sandbox submit so the freshness gate applies at the exact
submit moment, not just when the cycle started.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from backend.market_data.config import MarketDataConfig
from backend.market_data.dxlink_stream import (
    DXLinkQuoteTokenProvider,
    DXLinkStreamClient,
    StreamRunSummary,
)
from backend.market_data.tastytrade_market_data import MarketDataError
from backend.signals.models import MarketSnapshot, SignalDecision, SignalDirection
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine

DEFAULT_SIGNAL_SYMBOLS: Tuple[str, ...] = ("TNA", "TZA", "IWM", "SPY", "QQQ", "VIX")
DEFAULT_REVALIDATION_SECONDS = 3.0

StreamFactory = Callable[[DXLinkQuoteTokenProvider, List[str]], DXLinkStreamClient]


@dataclass
class DXLinkSignalResult:
    decision: SignalDecision
    snapshot: MarketSnapshot
    stream_summary: StreamRunSummary
    token_summary: Dict[str, object] = field(default_factory=dict)


def _default_stream_factory(provider: DXLinkQuoteTokenProvider, symbols: List[str]) -> DXLinkStreamClient:
    return DXLinkStreamClient(provider, symbols=symbols)


def collect_snapshot_from_dxlink(
    config: MarketDataConfig,
    *,
    symbols: Sequence[str] = DEFAULT_SIGNAL_SYMBOLS,
    duration_seconds: float = 30.0,
    max_age_seconds: float = 1.0,
    provider: Optional[DXLinkQuoteTokenProvider] = None,
    stream_factory: Optional[StreamFactory] = None,
) -> Tuple[MarketSnapshot, StreamRunSummary, Dict[str, object]]:
    """Stream read-only quotes and return the snapshot at the end of the window. Raises MarketDataError."""
    provider = provider or DXLinkQuoteTokenProvider(config)
    token = provider.get_token()
    client = (stream_factory or _default_stream_factory)(provider, list(symbols))
    try:
        client.connect()
        summary = client.stream(duration_seconds, max_age_seconds=max_age_seconds)
        snapshot = MarketSnapshot.from_stream_state(
            client.state,
            now=client.now(),
            created_at=datetime.now(timezone.utc),
        )
    finally:
        client.disconnect()
    return snapshot, summary, token.safe_summary()


def collect_signal_from_dxlink(
    config: MarketDataConfig,
    *,
    engine: TnaTzaSignalEngine,
    symbols: Sequence[str] = DEFAULT_SIGNAL_SYMBOLS,
    duration_seconds: float = 30.0,
    provider: Optional[DXLinkQuoteTokenProvider] = None,
    stream_factory: Optional[StreamFactory] = None,
) -> DXLinkSignalResult:
    """Stream read-only quotes, then build one snapshot and one decision. Raises MarketDataError."""
    snapshot, summary, token_summary = collect_snapshot_from_dxlink(
        config,
        symbols=symbols,
        duration_seconds=duration_seconds,
        max_age_seconds=engine.config.max_quote_age_seconds,
        provider=provider,
        stream_factory=stream_factory,
    )
    return DXLinkSignalResult(
        decision=engine.decide(snapshot),
        snapshot=snapshot,
        stream_summary=summary,
        token_summary=token_summary,
    )


def build_pre_submit_check(
    config: MarketDataConfig,
    *,
    engine: TnaTzaSignalEngine,
    expected: SignalDirection,
    provider: DXLinkQuoteTokenProvider,
    symbols: Sequence[str] = DEFAULT_SIGNAL_SYMBOLS,
    revalidation_seconds: float = DEFAULT_REVALIDATION_SECONDS,
    stream_factory: Optional[StreamFactory] = None,
) -> Callable[[], Tuple[bool, str]]:
    """
    Return a callable for SandboxBotWorker.run_cycle(pre_submit_check=...).

    It approves only if a fresh re-collected decision still passes the
    freshness gate and still points the same direction. Any error -> deny.
    """
    if expected not in (SignalDirection.BULLISH, SignalDirection.BEARISH):
        raise ValueError("pre-submit check requires a bullish or bearish expectation")

    def check() -> Tuple[bool, str]:
        try:
            result = collect_signal_from_dxlink(
                config,
                engine=engine,
                symbols=symbols,
                duration_seconds=revalidation_seconds,
                provider=provider,
                stream_factory=stream_factory,
            )
        except MarketDataError as exc:
            return False, f"market_data_revalidation_failed: {exc.reason}"
        decision = result.decision
        if not decision.freshness_gate_passed:
            reason = decision.skip_reason.value if decision.skip_reason else "stale_market_data"
            return False, f"{reason} at submit time"
        if decision.decision is not expected:
            reason = decision.skip_reason.value if decision.skip_reason else decision.decision.value
            return False, f"signal changed at submit time ({expected.value} -> {reason})"
        return True, f"revalidated {expected.value} (confidence {decision.confidence_score:g})"

    return check
