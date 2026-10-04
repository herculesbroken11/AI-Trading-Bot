"""Market session models (Checkpoint 2.14). Session-quality gating only — no orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


class SessionLabel(str, Enum):
    WEEKEND = "weekend"
    PRE_MARKET = "pre_market"
    REGULAR_HOURS = "regular_hours"
    NEAR_CLOSE = "near_close"
    AFTER_HOURS = "after_hours"
    CLOSED_UNKNOWN_HOLIDAY = "closed_unknown_holiday"


# Labels where the regular US equity session is not open.
CLOSED_LABELS = frozenset(
    {SessionLabel.WEEKEND, SessionLabel.PRE_MARKET, SessionLabel.AFTER_HOURS, SessionLabel.CLOSED_UNKNOWN_HOLIDAY}
)


class SessionConfigError(ValueError):
    """Invalid session guard configuration or override."""


class HolidayCalendar(Protocol):
    """Pluggable exchange calendar. Not implemented yet; the default knows no holidays."""

    def holiday_name(self, day: date) -> Optional[str]: ...

    def early_close(self, day: date) -> Optional[time]: ...


class NoHolidayCalendar:
    def holiday_name(self, day: date) -> Optional[str]:
        return None

    def early_close(self, day: date) -> Optional[time]:
        return None


@dataclass(frozen=True)
class SessionStatus:
    label: SessionLabel
    now_utc: datetime
    now_eastern: datetime
    timezone_source: str
    is_trading_day: bool
    is_regular_hours: bool  # inside 09:30-16:00 ET (true for regular_hours and near_close)
    is_near_close: bool
    minutes_to_open: Optional[float]  # only before today's open on a trading day
    minutes_to_close: Optional[float]  # only during the regular session
    near_close_minutes: float
    session_open: time
    session_close: time
    holiday_name: Optional[str] = None

    @property
    def seconds_to_close(self) -> Optional[float]:
        return None if self.minutes_to_close is None else self.minutes_to_close * 60.0

    def enough_time_remaining(self, required_seconds: float) -> bool:
        """True if required_seconds fit before the regular close (False outside the regular session)."""
        if not self.is_regular_hours or self.minutes_to_close is None:
            return False
        return self.minutes_to_close * 60.0 >= max(0.0, float(required_seconds))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_label": self.label.value,
            "now_utc": self.now_utc.isoformat(),
            "now_eastern": self.now_eastern.isoformat(),
            "timezone_source": self.timezone_source,
            "is_trading_day": self.is_trading_day,
            "session_is_regular_hours": self.is_regular_hours,
            "session_is_near_close": self.is_near_close,
            "minutes_to_open": None if self.minutes_to_open is None else round(self.minutes_to_open, 2),
            "session_minutes_to_close": None if self.minutes_to_close is None else round(self.minutes_to_close, 2),
            "near_close_minutes": self.near_close_minutes,
            "session_open_et": self.session_open.strftime("%H:%M"),
            "session_close_et": self.session_close.strftime("%H:%M"),
            "holiday_name": self.holiday_name,
        }


@dataclass(frozen=True)
class SessionGuardConfig:
    enforce: bool = False
    allow_near_close: bool = False
    allow_after_hours: bool = False
    min_minutes_before_close: float = 20.0

    def validate(self) -> "SessionGuardConfig":
        if not 0 <= float(self.min_minutes_before_close) <= 390:
            raise SessionConfigError("--min-minutes-before-close must be in [0, 390]")
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enforce_market_session": self.enforce,
            "allow_near_close": self.allow_near_close,
            "allow_after_hours": self.allow_after_hours,
            "min_minutes_before_close": self.min_minutes_before_close,
        }


@dataclass(frozen=True)
class SessionGuardDecision:
    """
    passed:  the session is suitable for collection (or explicitly allowed by a flag).
    blocked: enforce mode is on and passed is False — the caller must not start the cycle.
    In default (warn-only) mode blocked is always False and problems become warnings.
    """

    status: SessionStatus
    passed: bool
    blocked: bool
    reason_code: str
    reason: str
    enforce: bool
    required_seconds: float
    warnings: List[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        if self.blocked:
            return "blocked"
        return "passed" if self.passed else "warn"

    def record_fields(self) -> Dict[str, Any]:
        """Values persisted on shadow_signal_log rows."""
        return {
            "session_label": self.status.label.value,
            "session_is_regular_hours": self.status.is_regular_hours,
            "session_is_near_close": self.status.is_near_close,
            "session_minutes_to_close": (
                None if self.status.minutes_to_close is None else round(self.status.minutes_to_close, 2)
            ),
            "session_guard_passed": self.passed,
            "session_guard_reason": self.reason,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.status.to_dict(),
            "session_guard_mode": self.mode,
            "session_guard_passed": self.passed,
            "session_guard_blocked": self.blocked,
            "session_guard_reason_code": self.reason_code,
            "session_guard_reason": self.reason,
            "enforce_market_session": self.enforce,
            "required_seconds": self.required_seconds,
            "warnings": list(self.warnings),
        }
