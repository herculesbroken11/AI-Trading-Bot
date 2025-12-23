"""
Tactics Monitor AI - Secondary AI brain that monitors market conditions
and can override/change tactics when necessary.

Purpose: Prevent the "waiting too long" problem by allowing AI to break
its own cycle when great profit is shown or market conditions change.
"""

from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from .models import AIAnalysisResponse


class TacticsMonitorAI:
    """
    Secondary AI that monitors trading tactics and can override pattern decisions.
    
    This AI focuses on:
    1. Profit opportunity detection (don't wait too long)
    2. Market regime changes
    3. Pattern breaks
    4. Same-day profit protection
    """
    
    def __init__(self):
        self.profit_opportunity_threshold = 0.03  # 3% - significant profit
        self.aggressive_exit_threshold = 0.015  # 1.5% - good enough to take
        self.risk_turnaround_threshold = 0.005  # 0.5% - protect any positive gain
    
    def evaluate_tactics_override(
        self,
        current_pnl_pct: float,
        entry_price: float,
        current_price: float,
        hourly_analysis: Dict[str, Any],
        pattern_ai_decision: str,  # "hold", "exit", etc.
        time_since_entry_hours: float,
        volume_indicators: Dict[str, Any]
    ) -> Tuple[str, Optional[str], Dict[str, Any]]:
        """
        Evaluate if tactics should be overridden.
        
        Returns:
            (action, reason, metadata)
            action: "override_exit", "override_hold", "allow_pattern"
            reason: Explanation for override
            metadata: Additional context
        """
        metadata = {}
        
        # Rule 1: Aggressive profit taking - don't wait too long when profit is significant
        if current_pnl_pct >= self.profit_opportunity_threshold:
            if pattern_ai_decision == "hold":
                metadata["override_reason"] = "significant_profit"
                metadata["profit_pct"] = current_pnl_pct
                return ("override_exit", "Take profit now - significant gain shown, don't wait for perfect pattern", metadata)
        
        # Rule 2: Same-day profit protection - protect any positive gain if risk increases
        if current_pnl_pct >= self.risk_turnaround_threshold:
            # Check if money is leaving market
            if hourly_analysis.get("should_exit_on_negative_flow", False):
                metadata["override_reason"] = "money_leaving_market"
                metadata["profit_pct"] = current_pnl_pct
                return ("override_exit", "Protect profit - money leaving market, exit above entry price", metadata)
            
            # Check if hourly volume weakening significantly
            if hourly_analysis.get("volume_trend") == "weakening" and hourly_analysis.get("money_flow_changing") == "weakening":
                if current_pnl_pct >= self.aggressive_exit_threshold:
                    metadata["override_reason"] = "volume_weakening"
                    metadata["profit_pct"] = current_pnl_pct
                    return ("override_exit", "Take profit - volume weakening, protect gains", metadata)
        
        # Rule 3: Pattern break detection - market regime changed
        if hourly_analysis.get("money_flow_trend") == "leaving" and current_pnl_pct > 0:
            if pattern_ai_decision == "hold":
                metadata["override_reason"] = "regime_change"
                metadata["profit_pct"] = current_pnl_pct
                return ("override_exit", "Regime change detected - money leaving, exit with profit", metadata)
        
        # Rule 4: Time-based protection - if trade has been open for hours and is profitable
        # but not reaching target, protect the gain
        if time_since_entry_hours >= 3.0 and current_pnl_pct >= self.aggressive_exit_threshold:
            if pattern_ai_decision == "hold" and current_pnl_pct < 0.05:  # Below 5% target
                # Check if momentum is weakening
                momentum_weakening = volume_indicators.get("momentum_change", 0)
                if abs(momentum_weakening) < 0.001:  # Very low momentum
                    metadata["override_reason"] = "time_based_protection"
                    metadata["profit_pct"] = current_pnl_pct
                    metadata["hours_open"] = time_since_entry_hours
                    return ("override_exit", "Time-based protection - trade open for hours, take profit above entry", metadata)
        
        # Rule 5: Flat market detection - if market is flat and we have profit, take it
        volatility = volume_indicators.get("volatility_change", 0)
        if abs(volatility) < 0.05 and current_pnl_pct >= self.aggressive_exit_threshold:
            metadata["override_reason"] = "flat_market"
            metadata["profit_pct"] = current_pnl_pct
            return ("override_exit", "Flat market - take profit rather than wait for volatility", metadata)
        
        # No override needed - allow pattern AI decision
        return ("allow_pattern", None, {})
    
    def should_skip_trading_today(
        self,
        market_data: Dict[str, Any],
        trend_context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Determine if we should skip trading today due to market conditions.
        
        Returns:
            (should_skip, reason)
        """
        # Check for flat/low volatility day
        volatility = market_data.get("volatility", 0)
        if volatility < 0.01:  # Very low volatility
            return (True, "Low volatility day - market too flat for profitable trading")
        
        # Check for mixed signals
        confidence = market_data.get("confidence", 0)
        if confidence < 60:
            return (True, f"Low AI confidence ({confidence:.1f}%) - mixed signals, skip trading")
        
        return (False, "")


