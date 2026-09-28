"""Centralized request throttling and 429 cooldown for Tastytrade sandbox calls.

State (last request time per endpoint group, 429 cooldown deadline) is persisted to
a small JSON file so separate script runs share throttling. No secrets are stored.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATE_PATH = _REPO_ROOT / ".sandbox_rate_state.json"
STATE_PATH_ENV = "TASTYTRADE_SANDBOX_RATE_STATE_PATH"
COOLDOWN_ENV = "TASTYTRADE_SANDBOX_429_COOLDOWN_SECONDS"
INTERVAL_ENV_PREFIX = "TASTYTRADE_SANDBOX_MIN_INTERVAL_"

ENDPOINT_GROUPS = (
    "oauth",
    "customer",
    "accounts",
    "balances",
    "positions",
    "live_orders",
    "orders",
    "cancel",
)

DEFAULT_MIN_INTERVALS: Dict[str, float] = {
    "oauth": 10.0,
    "customer": 10.0,
    "accounts": 10.0,
    "balances": 10.0,
    "positions": 10.0,
    "live_orders": 10.0,
    "orders": 15.0,
    "cancel": 15.0,
}

DEFAULT_429_COOLDOWN_SECONDS = 60.0
MIN_ENV_INTERVAL_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 3600.0

RATE_LIMITED_REASON = "rate_limited"
RATE_LIMITED_NEXT_STEP = "wait before retrying"


@dataclass(frozen=True)
class RateLimitInfo:
    """Safe, secret-free description of a 429 / active cooldown."""

    cooldown_seconds: float
    endpoint_group: str
    step: str
    retry_after_header_present: bool = False
    cooldown_already_active: bool = False

    @property
    def failure_reason(self) -> str:
        return RATE_LIMITED_REASON

    @property
    def next_step(self) -> str:
        return RATE_LIMITED_NEXT_STEP

    def format_safe(self) -> str:
        lines = [
            f"failure_reason: {self.failure_reason}",
            f"step: {self.step}",
            f"endpoint_group: {self.endpoint_group}",
            f"cooldown_seconds: {int(round(self.cooldown_seconds))}",
            f"retry_after_header_present: {str(self.retry_after_header_present).lower()}",
            f"cooldown_already_active: {str(self.cooldown_already_active).lower()}",
            f"next_step: {self.next_step}",
        ]
        return "\n".join(lines)


class RateLimitCooldownActive(Exception):
    """Raised before a request when a prior 429 cooldown has not expired (no API call made)."""

    def __init__(self, info: RateLimitInfo) -> None:
        super().__init__(
            f"Sandbox API cooldown active for {int(round(info.cooldown_seconds))}s "
            f"(group {info.endpoint_group}); request not sent."
        )
        self.info = info


def parse_retry_after(
    value: Optional[str],
    *,
    now: Optional[float] = None,
    default: float = DEFAULT_429_COOLDOWN_SECONDS,
) -> tuple[float, bool]:
    """Return (cooldown_seconds, header_present). Supports delta-seconds and HTTP-date."""
    if value is None or not str(value).strip():
        return default, False
    text = str(value).strip()
    seconds: Optional[float] = None
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            reference = now if now is not None else time.time()
            seconds = parsed.timestamp() - reference
        except (TypeError, ValueError, IndexError):
            return default, False
    if seconds is None:
        return default, False
    return max(1.0, min(seconds, MAX_RETRY_AFTER_SECONDS)), True


def _env_float(env: Mapping[str, str], key: str) -> Optional[float]:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s value (not a number)", key)
        return None


def intervals_from_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, float]:
    """Apply TASTYTRADE_SANDBOX_MIN_INTERVAL_<GROUP> overrides (floored at 1s)."""
    source = env if env is not None else os.environ
    intervals = dict(DEFAULT_MIN_INTERVALS)
    for group in ENDPOINT_GROUPS:
        override = _env_float(source, f"{INTERVAL_ENV_PREFIX}{group.upper()}")
        if override is not None:
            intervals[group] = max(MIN_ENV_INTERVAL_SECONDS, override)
    return intervals


def cooldown_from_env(env: Optional[Mapping[str, str]] = None) -> float:
    source = env if env is not None else os.environ
    override = _env_float(source, COOLDOWN_ENV)
    if override is None:
        return DEFAULT_429_COOLDOWN_SECONDS
    return max(MIN_ENV_INTERVAL_SECONDS, override)


def state_path_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    source = env if env is not None else os.environ
    raw = source.get(STATE_PATH_ENV)
    if raw is None:
        return DEFAULT_STATE_PATH
    text = raw.strip()
    if not text or text.lower() in {"none", "off", "disabled"}:
        return None
    return Path(text)


@dataclass
class SandboxRateLimiter:
    """
    Per-endpoint-group minimum spacing plus a global 429 cooldown.

    acquire() sleeps at most once for the remaining interval (never busy-loops) and
    raises RateLimitCooldownActive instead of sleeping through a 429 cooldown.
    """

    min_intervals: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MIN_INTERVALS))
    default_cooldown_seconds: float = DEFAULT_429_COOLDOWN_SECONDS
    state_path: Optional[Path] = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    _last_request: Dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("sandbox rate state unreadable; starting fresh")
            return
        if not isinstance(data, dict):
            return
        last = data.get("last_request")
        if isinstance(last, dict):
            for group, stamp in last.items():
                if group in ENDPOINT_GROUPS and isinstance(stamp, (int, float)):
                    self._last_request[group] = max(self._last_request.get(group, 0.0), float(stamp))
        cooldown = data.get("cooldown_until")
        if isinstance(cooldown, (int, float)):
            self._cooldown_until = max(self._cooldown_until, float(cooldown))

    def _save_state(self) -> None:
        if not self.state_path:
            return
        payload = {
            "last_request": dict(self._last_request),
            "cooldown_until": self._cooldown_until,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            logger.debug("sandbox rate state could not be written")

    def interval_for(self, group: str) -> float:
        return float(self.min_intervals.get(group, max(self.min_intervals.values(), default=10.0)))

    def cooldown_remaining(self) -> float:
        with self._lock:
            self._load_state()
            return max(0.0, self._cooldown_until - self.clock())

    def wait_seconds_for(self, group: str) -> float:
        with self._lock:
            last = self._last_request.get(group)
            if last is None:
                return 0.0
            return max(0.0, last + self.interval_for(group) - self.clock())

    def acquire(self, group: str, *, step: str = "") -> float:
        """Wait out the group's minimum interval, then record the request. Returns seconds slept."""
        with self._lock:
            self._load_state()
            now = self.clock()
            remaining_cooldown = self._cooldown_until - now
            if remaining_cooldown > 0:
                raise RateLimitCooldownActive(
                    RateLimitInfo(
                        cooldown_seconds=remaining_cooldown,
                        endpoint_group=group,
                        step=step or group,
                        cooldown_already_active=True,
                    )
                )
            last = self._last_request.get(group)
            wait = 0.0 if last is None else max(0.0, last + self.interval_for(group) - now)

        if wait > 0:
            logger.info("sandbox throttle: waiting %.1fs before %s request", wait, group)
            self.sleep(wait)

        with self._lock:
            self._last_request[group] = self.clock()
            self._save_state()
        return wait

    def record_rate_limited(
        self,
        group: str,
        *,
        step: str,
        retry_after: Optional[str] = None,
    ) -> RateLimitInfo:
        """Record a 429 response and start a cooldown (no retry is performed here)."""
        now = self.clock()
        cooldown, header_present = parse_retry_after(
            retry_after,
            now=now,
            default=self.default_cooldown_seconds,
        )
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, now + cooldown)
            self._last_request[group] = now
            self._save_state()
        return RateLimitInfo(
            cooldown_seconds=cooldown,
            endpoint_group=group,
            step=step,
            retry_after_header_present=header_present,
        )

    def pause(self, seconds: float) -> None:
        """Deliberate single delay (e.g. before post-cancel verification)."""
        if seconds > 0:
            self.sleep(seconds)

    def clear_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0
            self._save_state()


_default_limiter: Optional[SandboxRateLimiter] = None
_default_lock = threading.Lock()


def build_rate_limiter_from_env(env: Optional[Mapping[str, str]] = None) -> SandboxRateLimiter:
    return SandboxRateLimiter(
        min_intervals=intervals_from_env(env),
        default_cooldown_seconds=cooldown_from_env(env),
        state_path=state_path_from_env(env),
    )


def get_sandbox_rate_limiter() -> SandboxRateLimiter:
    """Process-wide limiter shared by the OAuth client and adapter."""
    global _default_limiter
    with _default_lock:
        if _default_limiter is None:
            _default_limiter = build_rate_limiter_from_env()
        return _default_limiter


def set_sandbox_rate_limiter(limiter: Optional[SandboxRateLimiter]) -> None:
    """Replace (or reset with None) the process-wide limiter — used by tests."""
    global _default_limiter
    with _default_lock:
        _default_limiter = limiter


def rate_limit_info_from(exc: BaseException) -> Optional[RateLimitInfo]:
    """Return RateLimitInfo if the exception represents a 429 / active cooldown."""
    info = getattr(exc, "rate_limit", None)
    if isinstance(info, RateLimitInfo):
        return info
    if isinstance(exc, RateLimitCooldownActive):
        return exc.info
    return None
