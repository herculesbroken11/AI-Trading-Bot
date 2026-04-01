from .auth_tastytrade import TastytradeAuth
from typing import Dict, Optional
import httpx

class TradeExecutor:
    def __init__(self, auth: Optional[TastytradeAuth] = None):
        self.auth = auth or TastytradeAuth()

    def _request_with_refresh(self, method: str, url: str, *, json: Optional[Dict] = None) -> httpx.Response:
        """
        Make a request, and if auth failed, refresh token and retry once.
        """
        with httpx.Client() as client:
            resp = client.request(method, url, headers=self.auth.get_headers(), json=json)
            if resp.status_code in (401, 403):
                # Attempt refresh + retry once
                self.auth.refresh_access_token()
                resp = client.request(method, url, headers=self.auth.get_headers(), json=json)
            resp.raise_for_status()
            return resp
    
    def get_account_info(self) -> Dict:
        """Get account balance and positions"""
        if not self.auth.access_token:
            raise ValueError("Not authenticated")

        response = self._request_with_refresh("GET", f"{self.auth.base_url}/accounts")
        accounts = response.json()

        if accounts.get("data", {}).get("items"):
            account = accounts["data"]["items"][0]
            account_number = account["account-number"]

            balance_response = self._request_with_refresh(
                "GET",
                f"{self.auth.base_url}/accounts/{account_number}/balances",
            )
            balance_data = balance_response.json()

            return {
                "account_number": account_number,
                "balance": balance_data.get("data", {}).get("cash-balance", 0),
                "buying_power": balance_data.get("data", {}).get("day-trading-buying-power", 0),
                "open_positions": 0,  # TODO: Fetch from positions endpoint
                "daily_pnl": balance_data.get("data", {}).get("day-pnl", 0)
            }
        return {}
    
    def place_order(self, symbol: str, side: str, quantity: int = 1, order_type: str = "Market") -> Dict:
        """Place an order via Tastytrade"""
        if not self.auth.access_token:
            raise ValueError("Not authenticated")
        
        account_info = self.get_account_info()
        account_number = account_info.get("account_number")
        
        if not account_number:
            raise ValueError("No account found")
        
        order_data = {
            "time-in-force": "Day",
            "order-type": order_type,
            "price": None,
            "price-effect": "Debit" if side == "buy" else "Credit",
            "legs": [{
                "instrument-type": "Equity",
                "symbol": symbol,
                "quantity": quantity,
                "action": "Buy to Open" if side == "buy" else "Sell to Open"
            }]
        }
        
        response = self._request_with_refresh(
            "POST",
            f"{self.auth.base_url}/accounts/{account_number}/orders",
            json=order_data,
        )
        return response.json()
    
    def close_position(self, symbol: str, quantity: Optional[int] = None) -> Dict:
        """Close an open position"""
        if not self.auth.access_token:
            raise ValueError("Not authenticated")
        
        account_info = self.get_account_info()
        account_number = account_info.get("account_number")
        
        # Get positions first
        positions_response = self._request_with_refresh(
            "GET",
            f"{self.auth.base_url}/accounts/{account_number}/positions",
        )
        positions = positions_response.json()

        position = None
        for pos in positions.get("data", {}).get("items", []):
            if pos.get("symbol") == symbol:
                position = pos
                break

        if not position:
            raise ValueError(f"No open position found for {symbol}")

        qty = quantity or position.get("quantity", 0)
        close_side = "Sell to Close" if position.get("quantity", 0) > 0 else "Buy to Close"

        order_data = {
            "time-in-force": "Day",
            "order-type": "Market",
            "legs": [{
                "instrument-type": "Equity",
                "symbol": symbol,
                "quantity": qty,
                "action": close_side
            }]
        }

        response = self._request_with_refresh(
            "POST",
            f"{self.auth.base_url}/accounts/{account_number}/orders",
            json=order_data,
        )
        return response.json()

