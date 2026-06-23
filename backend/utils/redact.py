"""Redact sensitive fields from dict/JSON payloads before persistence."""

from __future__ import annotations

import json
from typing import Any, Dict, FrozenSet, Union

SENSITIVE_KEYS: FrozenSet[str] = frozenset(
    {
        "password",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "secret",
        "token",
        "database_url",
    }
)


def redact_value(key: str, value: Any) -> Any:
    key_lower = key.lower()
    if any(part in key_lower for part in SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [redact_value(key, item) if isinstance(item, dict) else item for item in value]
    return value


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: redact_value(key, value) for key, value in data.items()}


def redact_json(data: Union[Dict[str, Any], None]) -> str:
    if not data:
        return "{}"
    return json.dumps(redact_dict(data))
