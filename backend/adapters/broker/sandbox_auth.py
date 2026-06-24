"""OAuth2 client for Tastytrade sandbox only (https://api.cert.tastyworks.com)."""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional

import httpx

from backend.config.settings import Settings
from backend.config.tastytrade_urls import SANDBOX_BASE_URL, USER_AGENT, assert_sandbox_base_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0


class SandboxAuthError(RuntimeError):
    """Sandbox authentication failed."""


class SandboxOAuthClient:
    """Sandbox-only OAuth — never constructs production URLs."""

    def __init__(self, settings: Settings) -> None:
        assert_sandbox_base_url(SANDBOX_BASE_URL)
        if settings.tastytrade_env.strip().lower() != "sandbox":
            raise SandboxAuthError("Sandbox OAuth requires TASTYTRADE_ENV=sandbox")
        self._settings = settings
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = settings.tastytrade_refresh_token.strip() or None

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE_URL

    @property
    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    def ensure_authenticated(self) -> None:
        if self._access_token:
            return
        refresh = self._refresh_token
        if not refresh:
            raise SandboxAuthError(
                "Not authenticated. Set TASTYTRADE_REFRESH_TOKEN and client credentials in .env."
            )
        self._refresh_access_token()

    def get_headers(self) -> Dict[str, str]:
        self.ensure_authenticated()
        return {
            "Authorization": "Bearer [REDACTED]",  # never log real token — use _auth_header internally
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
            "User-Agent": USER_AGENT,
        }

    def refresh_access_token(self) -> None:
        self._refresh_access_token()

    def _refresh_access_token(self) -> None:
        client_id = self._settings.tastytrade_client_id
        client_secret = self._settings.tastytrade_client_secret
        refresh = self._refresh_token
        if not client_id or not client_secret or not refresh:
            raise SandboxAuthError("Missing sandbox OAuth credentials in settings")

        auth_string = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{SANDBOX_BASE_URL}/oauth/token",
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                },
                data={"grant_type": "refresh_token", "refresh_token": refresh},
            )
            if response.status_code in (401, 403):
                raise SandboxAuthError(
                    "Sandbox OAuth failed (401/403). Check credentials and User-Agent."
                )
            if response.status_code == 400:
                raise SandboxAuthError(
                    "Sandbox OAuth failed (400). Refresh token may be expired (sandbox "
                    "resets every 24h) or client credentials may not match the sandbox app."
                )
            response.raise_for_status()
            data = response.json()
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token", refresh)
            if not self._access_token:
                raise SandboxAuthError("Token response missing access_token")
