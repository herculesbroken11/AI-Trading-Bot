"""Safe OAuth failure diagnostics for Tastytrade sandbox (no secrets in output)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

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
    failure_reason: str = "unknown_provider_error"

    def format_safe(self) -> str:
        lines = [
            f"status_code: {self.status_code}",
            f"endpoint: {self.endpoint_path}",
            f"grant_type: {self.grant_type}",
            f"failure_reason: {self.failure_reason}",
            f"error_code: {self.error_code or 'unknown'}",
            f"error_description: {self.error_description or 'none'}",
            f"client_id configured: {str(self.client_id_configured).lower()}",
            f"client_secret configured: {str(self.client_secret_configured).lower()}",
            f"refresh_token configured: {str(self.refresh_token_configured).lower()}",
            f"redirect_uri configured: {str(self.redirect_uri_configured).lower()}",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class OAuthFailureClassification:
    reason: str
    message: str
    next_step: str


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

    if not error_code and isinstance(redacted.get("error_code"), str):
        error_code = redacted["error_code"].strip() or None

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


def classify_oauth_failure(
    *,
    status_code: int,
    error_code: Optional[str],
    error_description: Optional[str],
) -> OAuthFailureClassification:
    """Map provider OAuth failure into a clear, secret-free reason."""
    code = (error_code or "").lower()
    desc = (error_description or "").lower()

    if status_code == 429:
        return OAuthFailureClassification(
            reason="rate_limited",
            message="Sandbox OAuth rate limited (429).",
            next_step=(
                "Next step: wait before retrying; do not regenerate credentials. "
                "Check cooldown with scripts/sandbox_cooldown_status.py."
            ),
        )
    if status_code in {502, 503, 504} or status_code >= 500:
        return OAuthFailureClassification(
            reason="provider_unavailable",
            message="Sandbox OAuth provider is temporarily unavailable.",
            next_step="Next step: retry later; do not regenerate credentials for 5xx errors.",
        )
    if status_code == 400 and "not a tastytrade customer" in desc:
        return OAuthFailureClassification(
            reason="wrong_sandbox_customer",
            message="Refresh token/grant does not map to a valid sandbox customer.",
            next_step=(
                "Next step: create a new grant on the sandbox OAuth app "
                "(developer.tastytrade.com/sandbox), update TASTYTRADE_REFRESH_TOKEN only."
            ),
        )
    if status_code == 400 and ("revoked" in desc or "expired" in desc):
        return OAuthFailureClassification(
            reason="invalid_refresh_token",
            message="Sandbox refresh token was revoked or expired.",
            next_step=(
                "Next step: Create Grant on sandbox OAuth app and update "
                "TASTYTRADE_REFRESH_TOKEN (do not regenerate secret unless lost)."
            ),
        )
    if status_code == 400 and code == "invalid_client":
        return OAuthFailureClassification(
            reason="secret_token_mismatch",
            message="Client secret does not match the sandbox OAuth app.",
            next_step=(
                "Next step: verify TASTYTRADE_CLIENT_SECRET is from the sandbox app "
                "(not production my.tastytrade.com)."
            ),
        )
    if status_code == 400 and code in {"invalid_grant", "invalid_request"}:
        return OAuthFailureClassification(
            reason="invalid_refresh_token",
            message="Sandbox refresh token is invalid or out of sync with client secret.",
            next_step=(
                "Next step: Create Grant after confirming sandbox client secret; "
                "update TASTYTRADE_REFRESH_TOKEN. Check for duplicate TASTYTRADE_ keys in .env."
            ),
        )
    if status_code == 400 and ("production" in desc or "wrong environment" in desc):
        return OAuthFailureClassification(
            reason="production_sandbox_mismatch",
            message="Credentials appear to target the wrong Tastytrade environment.",
            next_step=(
                "Next step: use sandbox Client ID/secret/grant only "
                "(developer.tastytrade.com/sandbox) with api.cert.tastyworks.com."
            ),
        )
    if status_code == 400:
        return OAuthFailureClassification(
            reason="wrong_app_or_grant_order",
            message="Sandbox OAuth request rejected (likely secret/grant mismatch).",
            next_step=(
                "Next step: confirm Client ID, secret, and grant are all from the same "
                "sandbox OAuth app; recreate grant after any secret regenerate."
            ),
        )
    if status_code == 401:
        return OAuthFailureClassification(
            reason="invalid_refresh_token",
            message="Sandbox OAuth unauthorized.",
            next_step=(
                "Next step: verify User-Agent and sandbox credentials; recreate grant if needed."
            ),
        )
    if status_code == 403:
        return OAuthFailureClassification(
            reason="wrong_app_or_grant_order",
            message="Sandbox OAuth forbidden — scopes or grant may be insufficient.",
            next_step="Next step: ensure grant scopes include read trade openid.",
        )
    return OAuthFailureClassification(
        reason="unknown_provider_error",
        message=f"Sandbox OAuth failed with status {status_code}.",
        next_step="Next step: run scripts/check_tastytrade_oauth.py and review diagnostics.",
    )


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
    classification = classify_oauth_failure(
        status_code=status_code,
        error_code=error_code,
        error_description=error_description,
    )
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
        failure_reason=classification.reason,
    )


def oauth_next_step_hint(diagnostics: OAuthFailureDiagnostics) -> str:
    """Human-readable next step — no secrets."""
    classification = classify_oauth_failure(
        status_code=diagnostics.status_code,
        error_code=diagnostics.error_code,
        error_description=diagnostics.error_description,
    )
    return classification.next_step
