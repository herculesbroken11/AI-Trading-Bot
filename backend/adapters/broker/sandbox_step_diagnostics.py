"""Safe step-level diagnostics for sandbox read smoke (no secrets in output)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from backend.utils.redact import redact_dict

_TOKEN_LIKE_RE = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")

FORBIDDEN_403_HINT = (
    "403 usually means the token is valid but not authorized for this resource, "
    "the sandbox user/email is unconfirmed, the OAuth grant is missing scopes, "
    "or the sandbox customer/account was not created under the same sandbox user."
)

READ_SMOKE_STEPS = frozenset(
    {
        "oauth_token",
        "get_customers_me",
        "get_accounts",
        "get_balance",
        "get_positions",
    }
)


@dataclass(frozen=True)
class StepFailureDiagnostics:
    step: str
    status_code: int
    endpoint_path: str
    authorization_present: bool
    user_agent_present: bool
    provider_message: Optional[str] = None

    def format_safe(self) -> str:
        lines = [
            f"step: {self.step}",
            f"status_code: {self.status_code}",
            f"endpoint: {self.endpoint_path}",
            f"Authorization header present: {str(self.authorization_present).lower()}",
            f"User-Agent header present: {str(self.user_agent_present).lower()}",
            f"provider_message: {self.provider_message or 'none'}",
            f"next_step: {step_next_step_hint(self)}",
        ]
        return "\n".join(lines)


def header_flags(headers: Mapping[str, str]) -> tuple[bool, bool]:
    """Return (authorization_present, user_agent_present) without exposing values."""
    auth_present = False
    ua_present = False
    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower == "authorization" and bool(str(value).strip()):
            auth_present = True
        if key_lower == "user-agent" and bool(str(value).strip()):
            ua_present = True
    return auth_present, ua_present


def parse_provider_message(response_text: str) -> Optional[str]:
    """Extract a redacted provider error message from an API response body."""
    if not response_text or not response_text.strip():
        return None

    try:
        data = json.loads(response_text)
    except Exception:
        redacted = _TOKEN_LIKE_RE.sub("[REDACTED]", response_text[:500].strip())
        return redacted or None

    if not isinstance(data, dict):
        return None

    redacted = redact_dict(data)
    for key in ("error_description", "message", "error"):
        value = redacted.get(key)
        if isinstance(value, str) and value.strip():
            return _TOKEN_LIKE_RE.sub("[REDACTED]", value.strip())
        if isinstance(value, dict):
            nested = value.get("message") or value.get("code")
            if isinstance(nested, str) and nested.strip():
                return _TOKEN_LIKE_RE.sub("[REDACTED]", nested.strip())

    errors = redacted.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            msg = first.get("message") or first.get("code")
            if isinstance(msg, str) and msg.strip():
                return _TOKEN_LIKE_RE.sub("[REDACTED]", msg.strip())
        elif isinstance(first, str):
            return _TOKEN_LIKE_RE.sub("[REDACTED]", first.strip())

    return None


def build_step_failure(
    *,
    step: str,
    status_code: int,
    endpoint_path: str,
    request_headers: Mapping[str, str],
    response_text: str = "",
) -> StepFailureDiagnostics:
    auth_present, ua_present = header_flags(request_headers)
    return StepFailureDiagnostics(
        step=step,
        status_code=status_code,
        endpoint_path=endpoint_path,
        authorization_present=auth_present,
        user_agent_present=ua_present,
        provider_message=parse_provider_message(response_text),
    )


def step_next_step_hint(diagnostics: StepFailureDiagnostics) -> str:
    status = diagnostics.status_code
    step = diagnostics.step
    msg = (diagnostics.provider_message or "").lower()

    if status == 403:
        return FORBIDDEN_403_HINT

    if status == 401:
        return (
            "Next step: verify sandbox OAuth credentials and User-Agent header "
            "(AI-Trading-Bot/0.1); regenerate refresh token if expired."
        )

    if status == 422:
        return "Next step: sandbox instrumentation may lag — retry later."

    if status == 429:
        return "Next step: rate limited — wait and retry."

    if status >= 500:
        return "Next step: sandbox API server error — retry later."

    if step == "oauth_token":
        if "revoked" in msg:
            return (
                "Next step: create a new OAuth Grant in developer.tastyworks.com sandbox "
                "and update TASTYTRADE_REFRESH_TOKEN."
            )
        return (
            "Next step: verify TASTYTRADE_CLIENT_SECRET, refresh token, and "
            "TASTYTRADE_OAUTH_SCOPES match the sandbox OAuth app."
        )

    if step == "get_customers_me":
        return (
            "Next step: confirm sandbox user email is verified; OAuth grant must belong "
            "to the same sandbox login used to create the customer profile."
        )

    if step == "get_accounts":
        return (
            "Next step: create a sandbox trading account under the same sandbox user "
            "that issued the OAuth grant; confirm read scope is granted."
        )

    if step in {"get_balance", "get_positions"}:
        return (
            "Next step: verify the account number belongs to the authenticated sandbox "
            "customer and the grant includes read/trade scopes."
        )

    return "Next step: run scripts/check_tastytrade_sandbox_env.py and review diagnostics."


def format_step_failure(diagnostics: StepFailureDiagnostics) -> str:
    """Alias for safe formatted output."""
    return diagnostics.format_safe()
