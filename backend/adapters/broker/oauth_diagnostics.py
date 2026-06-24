"""Safe OAuth failure diagnostics for Tastytrade sandbox (no secrets in output)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.utils.redact import redact_dict

OAUTH_TOKEN_PATH = "/oauth/token"
REFRESH_GRANT_TYPE = "refresh_token"

# Redact JWT-like and long opaque strings from free-text error bodies.
_TOKEN_LIKE_RE = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")


@dataclass(frozen=True)
class OAuthFailureDiagnostics:
    status_code: int
    endpoint_path: str
    grant_type: str
    error_code: Optional[str]
    error_description: Optional[str]
    client_id_configured: bool
    client_secret_configured: bool
    refresh_token_configured: bool
    redirect_uri_configured: bool

    def format_safe(self) -> str:
        lines = [
            f"status_code: {self.status_code}",
            f"endpoint: {self.endpoint_path}",
            f"grant_type: {self.grant_type}",
            f"error_code: {self.error_code or 'unknown'}",
            f"error_description: {self.error_description or 'none'}",
            f"client_id configured: {str(self.client_id_configured).lower()}",
            f"client_secret configured: {str(self.client_secret_configured).lower()}",
            f"refresh_token configured: {str(self.refresh_token_configured).lower()}",
            f"redirect_uri configured: {str(self.redirect_uri_configured).lower()}",
        ]
        return "\n".join(lines)


def redact_oauth_body(body: Any) -> Any:
    """Redact token-like fields from OAuth JSON before logging or display."""
    if isinstance(body, dict):
        return redact_dict(body)
    if isinstance(body, str):
        return _TOKEN_LIKE_RE.sub("[REDACTED]", body)
    return body


def parse_oauth_error_body(response_text: str) -> tuple[Optional[str], Optional[str]]:
    """Extract OAuth error fields from response body without returning secrets."""
    import json

    try:
        data = json.loads(response_text)
    except Exception:
        redacted = redact_oauth_body(response_text[:500] if response_text else "")
        return None, str(redacted) if redacted else None

    if not isinstance(data, dict):
        return None, None

    redacted = redact_dict(data)
    error_code = redacted.get("error")
    if isinstance(error_code, dict):
        description = error_code.get("message") or error_code.get("code")
        error_code = error_code.get("code") or error_code.get("message")
        if isinstance(description, str):
            description = _TOKEN_LIKE_RE.sub("[REDACTED]", description.strip()) or None
        return (
            str(error_code).strip() if isinstance(error_code, str) and error_code else None,
            description if isinstance(description, str) else None,
        )

    if isinstance(error_code, str):
        error_code = error_code.strip() or None
    else:
        error_code = None

    description = redacted.get("error_description") or redacted.get("message")
    if isinstance(description, str):
        description = _TOKEN_LIKE_RE.sub("[REDACTED]", description.strip()) or None
    else:
        description = None

    # Some Tastytrade responses use {"error": "Grant revoked"} without error_description.
    if not description and error_code and " " in error_code:
        description = error_code
        error_code = "invalid_grant"

    return error_code, description


def build_oauth_diagnostics(
    *,
    status_code: int,
    response_text: str,
    grant_type: str,
    client_id_configured: bool,
    client_secret_configured: bool,
    refresh_token_configured: bool,
    redirect_uri_configured: bool,
    endpoint_path: str = OAUTH_TOKEN_PATH,
) -> OAuthFailureDiagnostics:
    error_code, error_description = parse_oauth_error_body(response_text)
    return OAuthFailureDiagnostics(
        status_code=status_code,
        endpoint_path=endpoint_path,
        grant_type=grant_type,
        error_code=error_code,
        error_description=error_description,
        client_id_configured=client_id_configured,
        client_secret_configured=client_secret_configured,
        refresh_token_configured=refresh_token_configured,
        redirect_uri_configured=redirect_uri_configured,
    )


def oauth_next_step_hint(diagnostics: OAuthFailureDiagnostics) -> str:
    """Human-readable next step — no secrets."""
    code = (diagnostics.error_code or "").lower()
    status = diagnostics.status_code

    if status == 400 and code in {"invalid_grant", "invalid_request"}:
        return (
            "Next step: regenerate sandbox refresh token (OAuth Applications > Manage > "
            "Create Grant). Sandbox resets every 24h — old tokens may be invalid."
        )
    desc = (diagnostics.error_description or "").lower()
    if status == 400 and "revoked" in desc:
        return (
            "Next step: refresh token was revoked or sandbox reset. Create a new Personal "
            "OAuth Grant in developer.tastyworks.com sandbox and update TASTYTRADE_REFRESH_TOKEN."
        )
    if status == 400 and code == "invalid_client":
        return (
            "Next step: verify TASTYTRADE_CLIENT_SECRET matches the sandbox OAuth app "
            "at developer.tastyworks.com (not production credentials)."
        )
    if status == 400:
        return (
            "Next step: confirm client_secret and refresh_token are from the same sandbox "
            "OAuth app; check TASTYTRADE_OAUTH_SCOPES matches app scopes."
        )
    if status == 401:
        return (
            "Next step: verify User-Agent header (AI-Trading-Bot/0.1) and that credentials "
            "are for sandbox (TASTYTRADE_ENV=sandbox)."
        )
    if status == 403:
        return (
            "Next step: verify OAuth app scopes include read/trade; regenerate grant with "
            "required scopes in TASTYTRADE_OAUTH_SCOPES."
        )
    if status == 422:
        return "Next step: sandbox instrumentation may lag — retry later."
    return "Next step: run scripts/check_tastytrade_sandbox_env.py and review diagnostics."
