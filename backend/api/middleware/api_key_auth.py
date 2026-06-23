"""API admin key verification for mutating routes (Phase 2 skeleton)."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Optional, Tuple

from flask import jsonify, request

from backend.config.settings import Settings

DEFAULT_INSECURE_KEY = "change-me-long-random-string"
API_KEY_HEADER = "X-API-Key"


def is_api_key_configured(settings: Settings) -> bool:
    key = (settings.api_admin_key or "").strip()
    return bool(key) and key != DEFAULT_INSECURE_KEY


def verify_api_key(settings: Settings, provided_key: Optional[str]) -> bool:
    if not is_api_key_configured(settings):
        return False
    expected = settings.api_admin_key.strip()
    if not provided_key:
        return False
    return provided_key.strip() == expected


def api_key_auth_response(
    settings: Settings,
    *,
    missing_config: bool = False,
) -> Tuple[Any, int]:
    if missing_config or not is_api_key_configured(settings):
        message = "API_ADMIN_KEY is not configured. Mutating routes are blocked."
    else:
        message = "Invalid or missing X-API-Key header."
    return (
        jsonify(
            {
                "success": False,
                "status": "unauthorized",
                "message": message,
            }
        ),
        401,
    )


def require_api_key(settings: Settings) -> Optional[Tuple[Any, int]]:
    """Return error response when API key check fails, else None."""
    if not is_api_key_configured(settings):
        return api_key_auth_response(settings, missing_config=True)
    provided = request.headers.get(API_KEY_HEADER)
    if not verify_api_key(settings, provided):
        return api_key_auth_response(settings)
    return None


def require_api_key_decorator(get_settings: Callable[[], Settings]) -> Callable:
    """Decorator factory for mutating Flask routes."""

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            settings = get_settings()
            auth_error = require_api_key(settings)
            if auth_error:
                return auth_error
            return view_func(*args, **kwargs)

        return wrapped

    return decorator
