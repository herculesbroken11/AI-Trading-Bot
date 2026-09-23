"""Transient retry helpers for Tastytrade sandbox REST calls."""

from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

import httpx

T = TypeVar("T")

TRANSIENT_HTTP_STATUS = frozenset({502, 503, 504})
NO_RETRY_HTTP_STATUS = frozenset({400, 401, 403, 422})
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (0.25, 0.5, 1.0)


def is_transient_http_status(status_code: int) -> bool:
    return status_code in TRANSIENT_HTTP_STATUS


def should_retry_http_status(status_code: int) -> bool:
    if status_code in NO_RETRY_HTTP_STATUS:
        return False
    return is_transient_http_status(status_code)


def is_transient_transport_error(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


def retry_transient(
    operation: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    should_retry_result: Optional[Callable[[T], bool]] = None,
) -> T:
    """
    Retry an operation on transient transport failures (and optional result predicate).

    Does not interpret HTTP status itself unless should_retry_result is provided.
    """
    attempts = max(1, max_attempts)
    last_exc: Optional[BaseException] = None
    for index in range(attempts):
        try:
            result = operation()
            if should_retry_result is not None and should_retry_result(result) and index < attempts - 1:
                delay = backoff_seconds[min(index, len(backoff_seconds) - 1)]
                sleep(delay)
                continue
            return result
        except Exception as exc:
            last_exc = exc
            if not is_transient_transport_error(exc) or index >= attempts - 1:
                raise
            delay = backoff_seconds[min(index, len(backoff_seconds) - 1)]
            sleep(delay)
    assert last_exc is not None
    raise last_exc
