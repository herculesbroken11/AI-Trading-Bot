"""
Shadow mode runner (Checkpoint 2.12): bounded, manual, observation only.

Runs a fixed number of cycles (hard cap MAX_CYCLES; no daemon, no infinite
loop). Each cycle: stream read-only DXLink data -> one Signal Engine decision
-> log it -> optionally stream a follow-up window and record market movement.
Checkpoint 2.14: an optional market session guard labels every cycle and, in
enforce mode, stops the run before a cycle that would start in a poor window.
This module has no broker, order or execution dependency and cannot submit orders.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from backend.config.settings import ConfigurationError, Settings
from backend.config.tastytrade_urls import (
    PRODUCTION_BASE_URL,
    BrokerUrlBlockedError,
    assert_market_data_request,
    resolve_broker_base_url,
)
from backend.market_data.config import MarketDataConfig, validate_market_data_settings
from backend.market_data.dxlink_stream import DXLinkQuoteTokenProvider
from backend.market_data.tastytrade_market_data import MarketDataError
from backend.market_session import MarketSessionGuard, SessionGuardDecision, get_session_status
from backend.shadow_mode.logger import ShadowSignalLogger
from backend.shadow_mode.models import (
    FOLLOWUP_MAX_QUOTE_AGE_SECONDS,
    ShadowCycleError,
    ShadowCycleRecord,
    ShadowRunSummary,
    compute_followup_outcome,
)
from backend.signals.dxlink_signal_source import (
    DEFAULT_SIGNAL_SYMBOLS,
    StreamFactory,
    collect_signal_from_dxlink,
    collect_snapshot_from_dxlink,
)
from backend.signals.models import REQUIRED_SIGNAL_SYMBOLS
from backend.signals.tna_tza_signal_engine import TnaTzaSignalEngine

MAX_CYCLES = 100
MAX_SIGNAL_DURATION_SECONDS = 300.0
MAX_FOLLOWUP_SECONDS = 900.0
MAX_PAUSE_SECONDS = 3600.0

# Stream hiccups: log the cycle error and move on. Anything else (auth, rate
# limit, read-only/URL guards, unknown) is fatal and stops the run (fail closed).
TRANSIENT_STREAM_REASONS = frozenset(
    {
        "network_error",
        "timeout",
        "connection_closed",
        "connection_lost",
        "connection_failed",
        "subscription_closed",
        "protocol_error",
        "provider_unavailable",
        "request_failed",
        "not_connected",
        "invalid_response",
    }
)

EventHandler = Callable[[str, Dict[str, Any]], None]


def is_fatal_market_data_error(exc: MarketDataError) -> bool:
    return exc.reason not in TRANSIENT_STREAM_REASONS


def production_execution_block_checks() -> Dict[str, bool]:
    """Prove (no network) that production order routes fail closed."""
    checks: Dict[str, bool] = {}
    try:
        resolve_broker_base_url("production")
        checks["production_broker_url_blocked"] = False
    except BrokerUrlBlockedError:
        checks["production_broker_url_blocked"] = True
    try:
        assert_market_data_request("POST", f"{PRODUCTION_BASE_URL}/accounts/BLOCKED/orders")
        checks["production_order_request_blocked"] = False
    except BrokerUrlBlockedError:
        checks["production_order_request_blocked"] = True
    return checks


def validate_shadow_environment(settings: Settings) -> MarketDataConfig:
    """Execution sandbox-only, production orders blocked, market data read-only. Raises ConfigurationError."""
    checks = production_execution_block_checks()
    if not all(checks.values()):
        raise ConfigurationError(f"production order execution is not blocked: {checks}")
    if settings.market_data_read_only is not True:
        raise ConfigurationError("MARKET_DATA_READ_ONLY must be true for shadow mode")
    return validate_market_data_settings(settings)


def is_regular_market_hours(now: Optional[datetime] = None) -> Optional[bool]:
    """US equities regular session (Mon-Fri 09:30-16:00 ET; holidays not known yet)."""
    return get_session_status(now).is_regular_hours


def new_run_id(now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"shadow-{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class ShadowRunConfig:
    cycles: int = 5
    signal_duration_seconds: float = 30.0
    pause_seconds: float = 10.0
    followup_seconds: float = 60.0
    symbols: Tuple[str, ...] = DEFAULT_SIGNAL_SYMBOLS
    followup_max_age_seconds: float = FOLLOWUP_MAX_QUOTE_AGE_SECONDS

    def validate(self) -> "ShadowRunConfig":
        problems = []
        if not isinstance(self.cycles, int) or not 1 <= self.cycles <= MAX_CYCLES:
            problems.append(f"cycles must be an integer in [1, {MAX_CYCLES}]")
        if not 0 < self.signal_duration_seconds <= MAX_SIGNAL_DURATION_SECONDS:
            problems.append(f"signal duration must be in (0, {MAX_SIGNAL_DURATION_SECONDS:g}] seconds")
        if not 0 <= self.followup_seconds <= MAX_FOLLOWUP_SECONDS:
            problems.append(f"follow-up must be in [0, {MAX_FOLLOWUP_SECONDS:g}] seconds (0 disables)")
        if not 0 <= self.pause_seconds <= MAX_PAUSE_SECONDS:
            problems.append(f"pause must be in [0, {MAX_PAUSE_SECONDS:g}] seconds")
        missing = [s for s in REQUIRED_SIGNAL_SYMBOLS if s not in self.symbols]
        if missing:
            problems.append(f"symbols must include {','.join(missing)}")
        if problems:
            raise ConfigurationError("Invalid shadow run config: " + "; ".join(problems))
        return self

    @property
    def seconds_per_cycle(self) -> float:
        """Time one cycle needs before the close: signal window + follow-up + pause."""
        return float(self.signal_duration_seconds + self.followup_seconds + self.pause_seconds)

    @property
    def estimated_run_seconds(self) -> float:
        return self.seconds_per_cycle * self.cycles - self.pause_seconds


class ShadowModeRunner:
    """Bounded observation loop. Holds no executor, router or broker adapter."""

    IS_SHADOW_ONLY = True

    def __init__(
        self,
        market_data_config: MarketDataConfig,
        engine: TnaTzaSignalEngine,
        signal_logger: ShadowSignalLogger,
        run_config: ShadowRunConfig,
        *,
        run_id: Optional[str] = None,
        provider: Optional[DXLinkQuoteTokenProvider] = None,
        stream_factory: Optional[StreamFactory] = None,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Optional[EventHandler] = None,
        session_guard: Optional[MarketSessionGuard] = None,
    ) -> None:
        self._config = market_data_config
        self._engine = engine
        self._logger = signal_logger
        self._run = run_config.validate()
        self.run_id = run_id or new_run_id()
        self._provider = provider
        self._stream_factory = stream_factory
        self._sleep = sleep
        self._on_event = on_event or (lambda _kind, _payload: None)
        self._session_guard = session_guard

    def _provider_or_default(self) -> DXLinkQuoteTokenProvider:
        if self._provider is None:
            self._provider = DXLinkQuoteTokenProvider(self._config)
        return self._provider

    def _cycle_error(self, summary: ShadowRunSummary, cycle: int, exc: MarketDataError) -> bool:
        fatal = is_fatal_market_data_error(exc)
        error = ShadowCycleError(cycle_number=cycle, step=exc.step, reason=exc.reason, fatal=fatal)
        summary.errors.append(error)
        self._on_event("cycle_error", {"error": error, "exception": exc})
        if fatal:
            summary.aborted = True
            summary.abort_reason = exc.reason
        return fatal

    def run(self) -> ShadowRunSummary:
        summary = ShadowRunSummary(run_id=self.run_id, cycles_requested=self._run.cycles)
        symbols: Sequence[str] = self._run.symbols
        for cycle in range(1, self._run.cycles + 1):
            self._on_event("cycle_start", {"cycle": cycle, "cycles": self._run.cycles})
            session = self._check_session(summary, cycle)
            if session is not None and session.blocked:
                break
            try:
                result = collect_signal_from_dxlink(
                    self._config,
                    engine=self._engine,
                    symbols=symbols,
                    duration_seconds=self._run.signal_duration_seconds,
                    provider=self._provider_or_default(),
                    stream_factory=self._stream_factory,
                )
            except MarketDataError as exc:
                if self._cycle_error(summary, cycle, exc):
                    break
                self._pause(cycle)
                continue

            record = ShadowCycleRecord.from_decision(
                run_id=self.run_id,
                cycle_number=cycle,
                decision=result.decision,
                snapshot=result.snapshot,
            )
            if session is not None:
                record.apply_session(session.record_fields())
            self._logger.log(record)
            summary.records.append(record)
            self._on_event("decision", {"record": record, "decision": result.decision})

            if self._run.followup_seconds > 0:
                if not self._followup(summary, cycle, record):
                    break
            self._pause(cycle)
        return summary

    def _check_session(self, summary: ShadowRunSummary, cycle: int) -> Optional[SessionGuardDecision]:
        if self._session_guard is None:
            return None
        decision = self._session_guard.check(self._run.seconds_per_cycle)
        self._on_event("session", {"cycle": cycle, "session": decision})
        if decision.blocked:
            summary.session_blocked = True
            summary.session_block_reason = decision.reason
            self._on_event("session_blocked", {"cycle": cycle, "session": decision})
        return decision

    def _followup(self, summary: ShadowRunSummary, cycle: int, record: ShadowCycleRecord) -> bool:
        """Returns False if a fatal error means the run must stop."""
        after = None
        failure_note = ""
        try:
            after, _summary, _token = collect_snapshot_from_dxlink(
                self._config,
                symbols=self._run.symbols,
                duration_seconds=self._run.followup_seconds,
                max_age_seconds=self._run.followup_max_age_seconds,
                provider=self._provider_or_default(),
                stream_factory=self._stream_factory,
            )
        except MarketDataError as exc:
            if self._cycle_error(summary, cycle, exc):
                return False
            failure_note = f"follow-up collection failed ({exc.reason})"
        outcome = compute_followup_outcome(
            decision=record.decision,
            selected_symbol=record.selected_symbol,
            mids_before=record.mids,
            after=after,
            followup_seconds=self._run.followup_seconds,
            max_after_age_seconds=self._run.followup_max_age_seconds,
        )
        if failure_note:
            outcome.outcome_note = f"{failure_note}; {outcome.outcome_note}"
        self._logger.record_followup(record, outcome)
        self._on_event("followup", {"record": record, "outcome": outcome})
        return True

    def _pause(self, cycle: int) -> None:
        if cycle < self._run.cycles and self._run.pause_seconds > 0:
            self._on_event("pause", {"seconds": self._run.pause_seconds})
            self._sleep(self._run.pause_seconds)
