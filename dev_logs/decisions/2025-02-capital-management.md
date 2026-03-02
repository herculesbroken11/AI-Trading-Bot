# Decision: Capital Management & Strict Rules

**Date**: 2025-02
**Source**: Client chat - "The bot can use available fund effectively including margin fund the way it doesn't over trade. Follow available fund first and base on that, make a trading decision."

## What We Implemented

1. **Capital Manager** (`backend/capital_manager.py`)
   - `calculate_position_size()` - Limits position to max % of buying power (default 25%)
   - `should_allow_trade()` - Checks min buying power, max trades per day, no duplicate positions

2. **Config Additions** (`config.json`)
   - `stop_loss_pct`: 0.05 - Configurable, "far enough" so we don't trigger too often
   - `max_position_pct_of_buying_power`: 0.25 - Use max 25% of buying power per trade
   - `min_buying_power_required`: 5000 - Don't trade if account too small
   - `max_trades_per_day`: 1 - Avoid overtrading

3. **Integration**
   - Bot manager checks capital before every entry
   - Manual execute_trade also applies capital rules
   - Stop loss uses config value (can override AI if AI suggests tighter)

## Why

Client priority: "Stability first. The bot should protect the account before it tries to maximize returns. Blowing up the account is the one thing we absolutely cannot allow."

"With strict rules the bot can survive in any market conditions."
