import httpx
import os
from typing import Optional, Dict
import base64

class TastytradeAuth:
    def __init__(self):
        self.client_id = os.getenv("TASTYTRADE_CLIENT_ID")
        self.client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
        self.redirect_uri = os.getenv("TASTYTRADE_REDIRECT_URI", "https://localhost")
        self.env = os.getenv("TASTYTRADE_ENV", "sandbox")
        self.base_url = "https://api.cert.tastytrade.com" if self.env == "sandbox" else "https://api.tastytrade.com"
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        
    def get_auth_url(self) -> str:
        """Generate OAuth2 authorization URL"""
        auth_url = f"{self.base_url}/oauth/authorize"
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "trading"
        }
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{auth_url}?{query_string}"
    
    async def exchange_code_for_token(self, code: str) -> Dict:
        """Exchange authorization code for access token"""
        auth_string = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
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
            return data
    
    async def refresh_access_token(self) -> Dict:
        """Refresh access token using refresh token"""
        auth_string = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
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
            return data
    
    def get_headers(self) -> Dict[str, str]:
        """Get headers with authorization token"""
        if not self.access_token:
            raise ValueError("Not authenticated. Call exchange_code_for_token first.")
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

