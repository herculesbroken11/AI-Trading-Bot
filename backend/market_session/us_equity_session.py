"""
US equity session clock and guard (Checkpoint 2.14).

Regular session 09:30-16:00 America/New_York, Monday-Friday. Holidays and
early closes are delegated to a HolidayCalendar (default: none known), so an
exchange calendar can be plugged in later without changing callers.

This module only labels time windows and decides whether a read-only data
collection cycle should start. It has no broker, order or execution access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from backend.market_session.models import (
    HolidayCalendar,
    NoHolidayCalendar,
    SessionConfigError,
    SessionGuardConfig,
    SessionGuardDecision,
    SessionLabel,
    SessionStatus,
)

EASTERN_TZ_NAME = "America/New_York"
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
DEFAULT_NEAR_CLOSE_MINUTES = 20.0

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class RecommendedWindow:
    name: str
    start: time
    end: time
    purpose: str

    @property
    def label(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')} ET"

    def contains(self, eastern: datetime) -> bool:
        return eastern.weekday() < 5 and self.start <= eastern.time() < self.end

    def minutes(self) -> float:
        return (datetime.combine(date.min, self.end) - datetime.combine(date.min, self.start)).total_seconds() / 60.0


RECOMMENDED_SHADOW_WINDOWS: Tuple[RecommendedWindow, ...] = (
    RecommendedWindow("open_confirmation", time(9, 45), time(10, 30), "opening trend confirmation after the first 15 minutes"),
    RecommendedWindow("midday_sample", time(11, 0), time(14, 30), "steady midday sample; usually calmer, good freshness"),
    RecommendedWindow("power_hour_early", time(15, 0), time(15, 30), "power-hour movement while still well before the close"),
)
AVOID_NOTE = "avoid the last 20 minutes (15:40-16:00 ET) unless explicitly testing close behaviour (--allow-near-close)"


def _first_sunday_on_or_after(day: date) -> date:
    return day + timedelta(days=(6 - day.weekday()) % 7)


def _us_eastern_fallback(now_utc: datetime) -> datetime:
    """US Eastern via the post-2007 DST rule, used only if the system tz database is missing."""
    year = now_utc.year
    dst_start = datetime.combine(_first_sunday_on_or_after(date(year, 3, 8)), time(7, 0), tzinfo=timezone.utc)
    dst_end = datetime.combine(_first_sunday_on_or_after(date(year, 11, 1)), time(6, 0), tzinfo=timezone.utc)
    offset = timedelta(hours=-4) if dst_start <= now_utc < dst_end else timedelta(hours=-5)
    return now_utc.astimezone(timezone(offset, "EDT" if offset == timedelta(hours=-4) else "EST"))


def to_eastern(now: datetime) -> Tuple[datetime, str]:
    """Convert an aware datetime (naive is treated as UTC) to US Eastern. Returns (eastern, source)."""
    now_utc = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now_utc.astimezone(ZoneInfo(EASTERN_TZ_NAME)), "zoneinfo"
    except Exception:
        return _us_eastern_fallback(now_utc), "builtin_us_dst_rule"


def parse_session_now_override(raw: Optional[str]) -> Optional[datetime]:
    """ISO-8601 datetime WITH timezone (e.g. 2026-10-02T15:45:00-04:00 or ...Z). Tests / dry checks only."""
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SessionConfigError("--session-now-override must be an ISO datetime, e.g. 2026-10-02T15:45:00-04:00") from exc
    if value.tzinfo is None:
        raise SessionConfigError("--session-now-override must include a timezone offset (e.g. -04:00 or Z)")
    return value


class USEquitySession:
    def __init__(
        self,
        *,
        near_close_minutes: float = DEFAULT_NEAR_CLOSE_MINUTES,
        holiday_calendar: Optional[HolidayCalendar] = None,
    ) -> None:
        if not 0 <= float(near_close_minutes) <= 390:
            raise SessionConfigError("near-close minutes must be in [0, 390]")
        self.near_close_minutes = float(near_close_minutes)
        self.calendar: HolidayCalendar = holiday_calendar or NoHolidayCalendar()

    def status(self, now: Optional[datetime] = None) -> SessionStatus:
        now = now or datetime.now(timezone.utc)
        eastern, source = to_eastern(now)
        today = eastern.date()
        close_time = self.calendar.early_close(today) or REGULAR_CLOSE
        holiday = self.calendar.holiday_name(today)
        weekend = eastern.weekday() >= 5
        is_trading_day = not weekend and holiday is None

        open_dt = eastern.replace(hour=REGULAR_OPEN.hour, minute=REGULAR_OPEN.minute, second=0, microsecond=0)
        close_dt = eastern.replace(hour=close_time.hour, minute=close_time.minute, second=0, microsecond=0)
        minutes_to_open: Optional[float] = None
        minutes_to_close: Optional[float] = None
        regular = near_close = False

        if weekend:
            label = SessionLabel.WEEKEND
        elif holiday is not None:
            label = SessionLabel.CLOSED_UNKNOWN_HOLIDAY
        elif eastern < open_dt:
            label = SessionLabel.PRE_MARKET
            minutes_to_open = (open_dt - eastern).total_seconds() / 60.0
        elif eastern >= close_dt:
            label = SessionLabel.AFTER_HOURS
        else:
            regular = True
            minutes_to_close = (close_dt - eastern).total_seconds() / 60.0
            near_close = minutes_to_close <= self.near_close_minutes
            label = SessionLabel.NEAR_CLOSE if near_close else SessionLabel.REGULAR_HOURS

        return SessionStatus(
            label=label,
            now_utc=now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc),
            now_eastern=eastern,
            timezone_source=source,
            is_trading_day=is_trading_day,
            is_regular_hours=regular,
            is_near_close=near_close,
            minutes_to_open=minutes_to_open,
            minutes_to_close=minutes_to_close,
            near_close_minutes=self.near_close_minutes,
            session_open=REGULAR_OPEN,
            session_close=close_time,
            holiday_name=holiday,
        )


def get_session_status(
    now: Optional[datetime] = None,
    *,
    near_close_minutes: float = DEFAULT_NEAR_CLOSE_MINUTES,
    holiday_calendar: Optional[HolidayCalendar] = None,
) -> SessionStatus:
    return USEquitySession(near_close_minutes=near_close_minutes, holiday_calendar=holiday_calendar).status(now)


def enough_time_remaining(status: SessionStatus, required_seconds: float) -> bool:
    return status.enough_time_remaining(required_seconds)


def evaluate_session_guard(
    status: SessionStatus,
    config: SessionGuardConfig,
    *,
    required_seconds: float,
) -> SessionGuardDecision:
    """
    Enforce mode blocks weekends, holidays, pre-market, after-hours (unless
    allowed), near-close (unless allowed) and cycles that cannot finish before
    the close. Default mode never blocks: the same problems become warnings.
    """
    label = status.label
    code, detail = "ok", "regular session with enough time before close"

    if label == SessionLabel.WEEKEND:
        code, detail = "weekend", "US equity market is closed on weekends"
    elif label == SessionLabel.CLOSED_UNKNOWN_HOLIDAY:
        code, detail = "closed_unknown_holiday", f"market holiday ({status.holiday_name})"
    elif label == SessionLabel.PRE_MARKET:
        code, detail = "pre_market", f"regular session opens at 09:30 ET ({status.minutes_to_open:.1f} min)"
    elif label == SessionLabel.AFTER_HOURS:
        if config.allow_after_hours:
            code, detail = "after_hours_allowed", "after-hours allowed by --allow-after-hours (quotes are often stale)"
        else:
            code, detail = "after_hours", f"regular session closed at {status.session_close.strftime('%H:%M')} ET"
    elif label == SessionLabel.NEAR_CLOSE:
        if config.allow_near_close:
            code, detail = "near_close_allowed", f"near close allowed by --allow-near-close ({status.minutes_to_close:.1f} min left)"
        else:
            code, detail = (
                "near_close",
                f"{status.minutes_to_close:.1f} min to close (buffer {status.near_close_minutes:g} min)",
            )

    if code in ("ok", "near_close_allowed") and not status.enough_time_remaining(required_seconds):
        left = status.seconds_to_close or 0.0
        code, detail = (
            "not_enough_time_before_close",
            f"cycle needs {required_seconds:g}s (signal + follow-up + pause) but only {left:.0f}s remain",
        )

    passed = code in ("ok", "near_close_allowed", "after_hours_allowed")
    blocked = bool(config.enforce) and not passed
    reason = f"{code}: {detail}"
    warnings: List[str] = []
    if not passed and not config.enforce:
        reason += " (warn only; --enforce-market-session is off)"
        warnings.append(f"market session warning: {code}: {detail}")
    elif code in ("near_close_allowed", "after_hours_allowed"):
        warnings.append(f"market session warning: {detail}")
    return SessionGuardDecision(
        status=status,
        passed=passed,
        blocked=blocked,
        reason_code=code,
        reason=reason,
        enforce=bool(config.enforce),
        required_seconds=float(required_seconds),
        warnings=warnings,
    )


class MarketSessionGuard:
    """Evaluates the session at call time. Read-only; it can only say 'start' or 'do not start'."""

    def __init__(
        self,
        config: SessionGuardConfig,
        *,
        clock: Optional[Clock] = None,
        holiday_calendar: Optional[HolidayCalendar] = None,
    ) -> None:
        self.config = config.validate()
        self.session = USEquitySession(
            near_close_minutes=config.min_minutes_before_close, holiday_calendar=holiday_calendar
        )
        self._clock: Clock = clock or (lambda: datetime.now(timezone.utc))

    def status(self) -> SessionStatus:
        return self.session.status(self._clock())

    def check(self, required_seconds: float) -> SessionGuardDecision:
        return evaluate_session_guard(self.status(), self.config, required_seconds=required_seconds)


def offset_clock(start: datetime, monotonic: Callable[[], float]) -> Clock:
    """Clock that starts at `start` and advances with `monotonic` (used for --session-now-override)."""
    origin = monotonic()
    return lambda: start + timedelta(seconds=monotonic() - origin)


def active_recommended_window(now: Optional[datetime] = None) -> Optional[RecommendedWindow]:
    eastern, _ = to_eastern(now or datetime.now(timezone.utc))
    return next((w for w in RECOMMENDED_SHADOW_WINDOWS if w.contains(eastern)), None)
