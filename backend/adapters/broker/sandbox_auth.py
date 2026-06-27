"""OAuth2 client for Tastytrade sandbox only (https://api.cert.tastyworks.com)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from backend.adapters.broker.oauth_diagnostics import (
    OAUTH_TOKEN_PATH,
    REFRESH_GRANT_TYPE,
    OAuthFailureDiagnostics,
    build_oauth_diagnostics,
    redact_oauth_body,
)
from backend.adapters.broker.sandbox_step_diagnostics import (
    StepFailureDiagnostics,
    build_step_failure,
)
from backend.config.settings import Settings
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, USER_AGENT, assert_sandbox_base_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0


class SandboxAuthError(RuntimeError):
    """Sandbox authentication failed."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Optional[OAuthFailureDiagnostics] = None,
        step_diagnostics: Optional[StepFailureDiagnostics] = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics
        self.step_diagnostics = step_diagnostics


class SandboxOAuthClient:
    """Sandbox-only OAuth — never constructs production URLs."""

    def __init__(self, settings: Settings) -> None:
        assert_sandbox_base_url(SANDBOX_BASE_URL)
        if settings.tastytrade_env.strip().lower() != "sandbox":
            raise SandboxAuthError("Sandbox OAuth requires TASTYTRADE_ENV=sandbox")
        self._settings = settings
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = settings.tastytrade_refresh_token.strip() or None
        self._oauth_attempted = False
        self._oauth_failed = False

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE_URL

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    @property
    def oauth_attempted(self) -> bool:
        return self._oauth_attempted

    def credential_flags(self) -> Dict[str, bool]:
        return {
            "client_id_configured": bool(self._settings.tastytrade_client_id.strip()),
            "client_secret_configured": bool(self._settings.tastytrade_client_secret.strip()),
            "refresh_token_configured": bool(self._refresh_token),
            "redirect_uri_configured": bool(self._settings.tastytrade_redirect_uri.strip()),
        }

    def ensure_authenticated(self) -> None:
        if self._access_token:
            return
        if self._oauth_failed:
            raise SandboxAuthError(
                "Sandbox OAuth already failed in this session. "
                "Fix credentials and restart — OAuth is not retried automatically."
            )
        if self._oauth_attempted:
            raise SandboxAuthError(
                "Sandbox OAuth already attempted without success. "
                "Fix credentials and restart."
            )
        if not self._refresh_token:
            raise SandboxAuthError(
                "Not authenticated. Set TASTYTRADE_REFRESH_TOKEN and client credentials in .env."
            )
        self._refresh_access_token()

    def get_headers(self) -> Dict[str, str]:
        self.ensure_authenticated()
        return {
            "Authorization": "Bearer [REDACTED]",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _auth_header_value(self) -> str:
        self.ensure_authenticated()
        return f"Bearer {self._access_token}"

    def request_headers(self) -> Dict[str, str]:
        """Headers for API calls (use this, not get_headers, for real requests)."""
        return {
            "Authorization": self._auth_header_value(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def refresh_access_token(self) -> None:
        """Refresh after API 401 (one retry allowed if prior auth succeeded)."""
        if self._oauth_failed:
            raise SandboxAuthError("OAuth refresh blocked after prior failure in this session.")
        self._access_token = None
        self._refresh_access_token()

    def _build_refresh_payload(self) -> Dict[str, str]:
        """
        Tastytrade refresh_token grant (matches official SDK).

        POST /oauth/token with JSON body: grant_type, client_secret, refresh_token.
        scope included when configured (required by JS SDK).
        redirect_uri is not sent for refresh_token grant.
        """
        client_secret = self._settings.tastytrade_client_secret.strip()
        refresh = self._refresh_token or ""
        payload: Dict[str, str] = {
            "grant_type": REFRESH_GRANT_TYPE,
            "client_secret": client_secret,
            "refresh_token": refresh,
        }
        scope = self._settings.tastytrade_oauth_scopes.strip()
        if scope:
            payload["scope"] = scope
        return payload

    def _refresh_access_token(self) -> None:
        client_secret = self._settings.tastytrade_client_secret.strip()
        refresh = self._refresh_token
        if not client_secret or not refresh:
            raise SandboxAuthError("Missing sandbox OAuth credentials in settings")

        self._oauth_attempted = True
        payload = self._build_refresh_payload()
        flags = self.credential_flags()

        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(
                    f"{SANDBOX_BASE_URL}{OAUTH_TOKEN_PATH}",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise SandboxAuthError(
                "Sandbox OAuth timed out. Possible network issue or IP block — retry later."
            ) from exc
        except httpx.RequestError as exc:
            raise SandboxAuthError(
                f"Sandbox OAuth request failed: {type(exc).__name__}. Check network connectivity."
            ) from exc

        if response.status_code >= 400:
            self._oauth_failed = True
            oauth_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
            step_diag = build_step_failure(
                step="oauth_token",
                status_code=response.status_code,
                endpoint_path=OAUTH_TOKEN_PATH,
                request_headers=oauth_headers,
                response_text=response.text,
            )
            diagnostics = build_oauth_diagnostics(
                status_code=response.status_code,
                response_text=response.text,
                grant_type=REFRESH_GRANT_TYPE,
                client_id_configured=flags["client_id_configured"],
                client_secret_configured=flags["client_secret_configured"],
                refresh_token_configured=flags["refresh_token_configured"],
                redirect_uri_configured=flags["redirect_uri_configured"],
            )
            logger.debug(
                "Sandbox OAuth failed\n%s",
                step_diag.format_safe(),
            )
            raise SandboxAuthError(
                f"Sandbox OAuth failed ({response.status_code}).",
                diagnostics=diagnostics,
                step_diagnostics=step_diag,
            )

        data = self._parse_token_response(response)
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token", refresh)
        if not self._access_token:
            raise SandboxAuthError("Token response missing access_token")

    @staticmethod
    def _parse_token_response(response: httpx.Response) -> Dict[str, Any]:
        try:
            return response.json()
        except Exception as exc:
            redacted = redact_oauth_body(response.text[:500])
            raise SandboxAuthError(
                f"Sandbox OAuth returned non-JSON response: {redacted}"
            ) from exc
