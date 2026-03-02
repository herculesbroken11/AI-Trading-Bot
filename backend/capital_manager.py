"""
Capital Management - Follow available funds first, avoid overtrading.

Client requirement: "The bot can use available fund effectively including
margin fund the way it doesn't over trade. Follow available fund first
and base on that, make a trading decision."
"""

from typing import Tuple, Optional


def calculate_position_size(
    buying_power: float,
    entry_price: float,
    default_quantity: int,
    max_position_pct: float = 0.25,
    min_buying_power_required: float = 5000.0,
) -> Tuple[int, str]:
    """
    Calculate safe position size based on available buying power.
    
    Returns:
        (quantity, reason) - quantity to trade, and reason for the decision
    """
    if buying_power < min_buying_power_required:
        return (0, f"insufficient_funds: buying_power ${buying_power:.0f} < min required ${min_buying_power_required:.0f}")
    
    if entry_price <= 0:
        return (0, "invalid_entry_price")
    
    # Max capital to allocate to this single trade
    max_trade_capital = buying_power * max_position_pct
    
    # Max shares we can afford with that capital
    max_shares_by_capital = int(max_trade_capital / entry_price)
    
    # Use the smaller of: default quantity, or what we can afford
    quantity = min(default_quantity, max_shares_by_capital)
    
    if quantity <= 0:
        return (0, f"position_too_small: max_trade_capital ${max_trade_capital:.0f} / price ${entry_price:.2f} = 0 shares")
    
    reason = "ok" if quantity == default_quantity else f"reduced_for_capital: using {quantity} shares (max allowed by {max_position_pct*100:.0f}% of buying power)"
    
    return (quantity, reason)


def should_allow_trade(
    buying_power: float,
    min_buying_power_required: float = 5000.0,
    has_open_position: bool = False,
    trades_today: int = 0,
    max_trades_per_day: int = 1,
) -> Tuple[bool, str]:
    """
    Check if we should allow a new trade based on capital and risk rules.
    
    Returns:
        (allow, reason)
    """
    if has_open_position:
        return (False, "already_has_open_position")
    
    if trades_today >= max_trades_per_day:
        return (False, f"max_trades_reached: {trades_today}/{max_trades_per_day} today")
    
    if buying_power < min_buying_power_required:
        return (False, f"insufficient_buying_power: ${buying_power:.0f} < ${min_buying_power_required:.0f} required")
    
    return (True, "ok")
