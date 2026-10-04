"""
Market session labelling and guard (Checkpoint 2.14).

Labels US equity session windows (weekend, pre-market, regular hours,
near-close, after-hours, holiday) so read-only data collection can avoid or
clearly mark poor market-data windows. No broker, order or execution access.
"""

from backend.market_session.models import (
    CLOSED_LABELS,
    HolidayCalendar,
    NoHolidayCalendar,
    SessionConfigError,
    SessionGuardConfig,
    SessionGuardDecision,
    SessionLabel,
    SessionStatus,
)
from backend.market_session.us_equity_session import (
    AVOID_NOTE,
    DEFAULT_NEAR_CLOSE_MINUTES,
    RECOMMENDED_SHADOW_WINDOWS,
    REGULAR_CLOSE,
    REGULAR_OPEN,
    MarketSessionGuard,
    RecommendedWindow,
    USEquitySession,
    active_recommended_window,
    enough_time_remaining,
    evaluate_session_guard,
    get_session_status,
    offset_clock,
    parse_session_now_override,
    to_eastern,
)

__all__ = [
    "AVOID_NOTE",
    "CLOSED_LABELS",
    "DEFAULT_NEAR_CLOSE_MINUTES",
    "RECOMMENDED_SHADOW_WINDOWS",
    "REGULAR_CLOSE",
    "REGULAR_OPEN",
    "HolidayCalendar",
    "MarketSessionGuard",
    "NoHolidayCalendar",
    "RecommendedWindow",
    "SessionConfigError",
    "SessionGuardConfig",
    "SessionGuardDecision",
    "SessionLabel",
    "SessionStatus",
    "USEquitySession",
    "active_recommended_window",
    "enough_time_remaining",
    "evaluate_session_guard",
    "get_session_status",
    "offset_clock",
    "parse_session_now_override",
    "to_eastern",
]
