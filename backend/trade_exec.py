from .auth_tastytrade import TastytradeAuth
from typing import Dict, Optional
import httpx

class TradeExecutor:
    def __init__(self, auth: Optional[TastytradeAuth] = None):
        self.auth = auth or TastytradeAuth()
    
    async def get_account_info(self) -> Dict:
        """Get account balance and positions"""
        if not self.auth.access_token:
            raise ValueError("Not authenticated")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.auth.base_url}/accounts",
                headers=self.auth.get_headers()
            )
            response.raise_for_status()
            accounts = response.json()
            
            if accounts.get("data", {}).get("items"):
                account = accounts["data"]["items"][0]
                account_number = account["account-number"]
                
                # Get account balance
                balance_response = await client.get(
                    f"{self.auth.base_url}/accounts/{account_number}/balances",
                    headers=self.auth.get_headers()
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
    
    async def place_order(self, symbol: str, side: str, quantity: int = 1, order_type: str = "Market") -> Dict:
        """Place an order via Tastytrade"""
        if not self.auth.access_token:
            raise ValueError("Not authenticated")
        
        account_info = await self.get_account_info()
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
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.auth.base_url}/accounts/{account_number}/orders",
                headers=self.auth.get_headers(),
                json=order_data
            )
            response.raise_for_status()
            return response.json()
    
    async def close_position(self, symbol: str, quantity: Optional[int] = None) -> Dict:
        """Close an open position"""
        if not self.auth.access_token:
            raise ValueError("Not authenticated")
        
        account_info = await self.get_account_info()
        account_number = account_info.get("account_number")
        
        # Get positions first
        async with httpx.AsyncClient() as client:
            positions_response = await client.get(
                f"{self.auth.base_url}/accounts/{account_number}/positions",
                headers=self.auth.get_headers()
            )
            positions = positions_response.json()
            
            # Find position for symbol
            position = None
            for pos in positions.get("data", {}).get("items", []):
                if pos.get("symbol") == symbol:
                    position = pos
                    break
            
            if not position:
                raise ValueError(f"No open position found for {symbol}")
            
            qty = quantity or position.get("quantity", 0)
            side = "Sell to Close" if position.get("quantity", 0) > 0 else "Buy to Close"
            
            order_data = {
                "time-in-force": "Day",
                "order-type": "Market",
                "legs": [{
                    "instrument-type": "Equity",
                    "symbol": symbol,
                    "quantity": qty,
                    "action": side
                }]
            }
            
            response = await client.post(
                f"{self.auth.base_url}/accounts/{account_number}/orders",
                headers=self.auth.get_headers(),
                json=order_data
            )
            response.raise_for_status()
            return response.json()

