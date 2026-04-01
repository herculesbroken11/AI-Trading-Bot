import httpx
import os
from typing import Optional, Dict
import base64
from datetime import datetime
from urllib.parse import urlencode

from .database import SessionLocal, OAuthToken


class TastytradeAuth:
    def __init__(self):
        self.client_id = os.getenv("TASTYTRADE_CLIENT_ID")
        self.client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
        self.redirect_uri = os.getenv("TASTYTRADE_REDIRECT_URI", "https://localhost")
        self.env = os.getenv("TASTYTRADE_ENV", "sandbox")
        self.base_url = "https://api.cert.tastytrade.com" if self.env == "sandbox" else "https://api.tastytrade.com"
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._load_tokens_from_db()
        self._bootstrap_from_env_refresh_token()

    def _load_tokens_from_db(self) -> None:
        """Load most recent tokens from DB (if present)."""
        try:
            db = SessionLocal()
            token = (
                db.query(OAuthToken)
                .filter(OAuthToken.provider == "tastytrade")
                .order_by(OAuthToken.updated_at.desc())
                .first()
            )
            if token:
                self.access_token = token.access_token
                self.refresh_token = token.refresh_token
        except Exception:
            # If DB is not ready, we can still authenticate later via /auth/tastytrade
            return
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _persist_tokens_to_db(self, token_response: Dict) -> None:
        """Persist tokens to DB for stability across restarts."""
        db = SessionLocal()
        try:
            token = (
                db.query(OAuthToken)
                .filter(OAuthToken.provider == "tastytrade")
                .order_by(OAuthToken.updated_at.desc())
                .first()
            )
            if not token:
                token = OAuthToken(provider="tastytrade", access_token=self.access_token or "")
                db.add(token)

            token.access_token = self.access_token or token.access_token
            token.refresh_token = self.refresh_token or token.refresh_token
            token.token_type = token_response.get("token_type")
            token.scope = token_response.get("scope")
            token.expires_in = token_response.get("expires_in")
            token.updated_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()

    def _bootstrap_from_env_refresh_token(self) -> None:
        """
        Sandbox developer console can issue a refresh token via "Create Grant"
        without browser OAuth. If we have no access token yet, exchange env refresh
        for an access token.
        """
        if self.access_token:
            return
        env_refresh = os.getenv("TASTYTRADE_REFRESH_TOKEN", "").strip()
        if not env_refresh:
            return
        if not self.client_id or not self.client_secret:
            return
        self.refresh_token = env_refresh
        try:
            self.refresh_access_token()
        except Exception:
            self.access_token = None

    def get_auth_url(self) -> str:
        """Generate OAuth2 authorization URL"""
        auth_url = f"{self.base_url}/oauth/authorize"
        scope = os.getenv("TASTYTRADE_OAUTH_SCOPES", "read trade openid")
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": scope,
        }
        return f"{auth_url}?{urlencode(params)}"
    
    def exchange_code_for_token(self, code: str) -> Dict:
        """Exchange authorization code for access token"""
        auth_string = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        
        with httpx.Client() as client:
            response = client.post(
                f"{self.base_url}/oauth/token",
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri
                }
            )
            response.raise_for_status()
            data = response.json()
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token")
            self._persist_tokens_to_db(data)
            return data
    
    def refresh_access_token(self) -> Dict:
        """Refresh access token using refresh token"""
        if not self.refresh_token:
            raise ValueError("No refresh token available. Re-authenticate via OAuth.")
        auth_string = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        
        with httpx.Client() as client:
            response = client.post(
                f"{self.base_url}/oauth/token",
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token
                }
            )
            response.raise_for_status()
            data = response.json()
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            self._persist_tokens_to_db(data)
            return data
    
    def get_headers(self) -> Dict[str, str]:
        """Get headers with authorization token"""
        if not self.access_token:
            raise ValueError(
                "Not authenticated. Set TASTYTRADE_REFRESH_TOKEN + client credentials, "
                "or POST /auth/tastytrade with OAuth code."
            )
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

