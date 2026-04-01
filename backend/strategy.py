from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Dict, Optional, Tuple, Any

import pandas as pd
import pytz

from .models import (
    AIAnalysisResponse,
    EntryStrategyResponse,
    ExitStrategyResponse,
)

class TradingStrategy:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.is_active = False
        self.timezone = pytz.timezone(config.get("timezone", "US/Eastern"))

    # ------------------------------------------------------------------
    # Entry Logic
    # ------------------------------------------------------------------
    def evaluate_entry(
        self,
        ai_analysis: AIAnalysisResponse,
        data_map: Dict[str, pd.DataFrame]
    ) -> Optional[EntryStrategyResponse]:
        if not self.is_active:
            return None

        if ai_analysis.skip_trade or ai_analysis.confidence < self.config.get("min_confidence", 65):
            return None

        symbol = ai_analysis.recommended_symbol or ("TNA" if ai_analysis.direction == "bullish" else "TZA")
        side = "buy" if symbol == "TNA" else "sell"
        df = data_map.get(symbol)
        if df is None or df.empty:
            return None

        now_et = datetime.now(self.timezone)
        start_time, end_time, entry_start, entry_end = self.get_effective_entry_window_times()

        if not self._is_within_time_window(now_et.time(), start_time, end_time):
            return None

        entry_window_df = df.between_time(entry_start, entry_end)
        if entry_window_df.empty:
            return None

        pullback_enabled = bool(self.config.get("pullback_entry_enabled", True))
        min_retrace = float(self.config.get("pullback_min_retrace_pct", 0.0015))
        min_bars = max(2, int(self.config.get("pullback_lookback_bars", 5)))

        last_row = entry_window_df.iloc[-1]
        last_close = float(last_row["close"])

        # Pullback / small counter-swing: long waits for dip from session high; short (bearish ETF) waits for bounce off session low
        pullback_met = True
        session_high = float(entry_window_df["high"].max())
        session_low = float(entry_window_df["low"].min())
        retrace_or_bounce_pct = 0.0

        if pullback_enabled:
            if len(entry_window_df) < min_bars:
                return None
            if side == "buy":
                if session_high <= 0:
                    return None
                retrace_or_bounce_pct = (session_high - last_close) / session_high
                pullback_met = retrace_or_bounce_pct >= min_retrace
            else:
                if session_low <= 0:
                    return None
                retrace_or_bounce_pct = (last_close - session_low) / session_low
                pullback_met = retrace_or_bounce_pct >= min_retrace

        if not pullback_met:
            return None

        # Market-style execution: size and log using last print; buffer kept for optional limit semantics elsewhere
        buffer_pct = self.config.get("entry_price_buffer", 0.001)
        entry_price = last_close * (1 + buffer_pct if side == "buy" else 1 - buffer_pct)

        vol_mean = entry_window_df["volume"].mean() or 1.0
        volume_surge = last_row["volume"] > (last_row.get("volume_sma_10") or vol_mean) * 1.15
        money_flow_strength = last_row.get("money_flow_volume", 0)
        volatility_burst = abs(last_row.get("volatility_10", 0)) > entry_window_df["volatility_10"].mean()

        rationale = ai_analysis.notes or "AI-directed entry: morning window + pullback from session extreme"

        order_type = str(self.config.get("order_type", "Market"))

        return EntryStrategyResponse(
            symbol=symbol,
            side=side,
            entry_price=float(entry_price),
            entry_window_start=self._today_at(start_time),
            entry_window_end=self._today_at(end_time),
            confidence=ai_analysis.confidence,
            rationale=rationale,
            indicators={
                "volume_surge": volume_surge,
                "money_flow": float(money_flow_strength or 0),
                "volatility_burst": bool(volatility_burst),
                "ai_direction": ai_analysis.direction,
                "ai_confidence": ai_analysis.confidence,
                "pullback_met": pullback_met,
                "pullback_retrace_or_bounce_pct": float(retrace_or_bounce_pct),
                "session_high": session_high,
                "session_low": session_low,
                "entry_window": f"{entry_start}-{entry_end}",
            },
            order_type=order_type,
        )

    # ------------------------------------------------------------------
    # Exit Logic
    # ------------------------------------------------------------------
    def evaluate_exit(
        self,
        request_data: Dict[str, Any],
        df: pd.DataFrame,
        ai_analysis: AIAnalysisResponse
    ) -> ExitStrategyResponse:
        current_price = request_data["current_price"]
        side = request_data["side"]
        entry_price = request_data["entry_price"]
        pnl_pct = self._calculate_return(side, entry_price, current_price)

        indicators = self._build_exit_indicators(df)
        now_et = datetime.now(self.timezone)

        # Forced exit rule
        forced_exit_time = self._parse_time(self.config.get("forced_exit_time", "15:30"))
        if now_et.time() >= forced_exit_time:
            return ExitStrategyResponse(
                action="exit",
                exit_price=current_price,
                reason="forced_close",
                trailing_stop=None,
                indicators=indicators,
            )

        # Stop-loss enforcement
        stop_loss = request_data.get("stop_loss", ai_analysis.stop_loss)
        if pnl_pct <= -abs(stop_loss):
            return ExitStrategyResponse(
                action="exit",
                exit_price=current_price,
                reason="stop_loss_trigger",
                trailing_stop=None,
                indicators={**indicators, "pnl_pct": pnl_pct},
            )

        take_profit = request_data.get("take_profit", ai_analysis.take_profit)
        early_exit_target = request_data.get("early_exit_target", ai_analysis.early_exit_profit)
        trailing_stop = request_data.get("trailing_stop", None)

        # Full profit target hit
        if pnl_pct >= take_profit:
            strong_volume = indicators.get("volume_ratio", 0) > 1.25
            if ai_analysis.use_trailing_stop and strong_volume:
                new_trailing = max(trailing_stop or 0, self.config.get("trailing_stop_pct", 0.02))
                return ExitStrategyResponse(
                    action="hold",
                    exit_price=None,
                    reason="trailing_stop_active",
                    trailing_stop=new_trailing,
                    indicators={**indicators, "pnl_pct": pnl_pct},
                )
            else:
                return ExitStrategyResponse(
                    action="exit",
                    exit_price=current_price,
                    reason="take_profit_hit",
                    trailing_stop=None,
                    indicators={**indicators, "pnl_pct": pnl_pct},
                )

        # Enhanced Partial profit / early exit logic with hourly volume and money flow
        if 0 < pnl_pct < take_profit:
            hourly_analysis = indicators.get("hourly_analysis", {})
            weakening_volume = indicators.get("volume_ratio", 1) < 0.9
            momentum_loss = indicators.get("momentum_change", 0) < 0 if side == "buy" else indicators.get("momentum_change", 0) > 0
            volatility_drop = indicators.get("volatility_change", 0) < -0.15
            
            # Enhanced exit conditions using hourly analysis
            money_leaving = hourly_analysis.get("money_flow_trend") == "leaving"
            money_flow_weakening = hourly_analysis.get("money_flow_changing") == "weakening"
            volume_weakening_hourly = hourly_analysis.get("volume_trend") == "weakening"
            
            # Calculate time since entry for time-based protection
            entry_time = request_data.get("entry_time")
            if entry_time:
                if isinstance(entry_time, str):
                    from dateutil import parser
                    entry_time = parser.parse(entry_time)
                time_since_entry = (now_et - entry_time).total_seconds() / 3600.0  # hours
            else:
                time_since_entry = 0.0
            
            # Enhanced exit conditions: use hourly volume/money flow analysis
            should_exit = False
            exit_reason = "early_profit_lock"
            
            # Priority 1: Money leaving market (strongest signal)
            if money_leaving and money_flow_weakening and pnl_pct >= 0.005:  # Any positive gain
                should_exit = True
                exit_reason = "money_leaving_market_protection"
            
            # Priority 2: Volume weakening + money flow weakening
            elif volume_weakening_hourly and money_flow_weakening and pnl_pct >= early_exit_target:
                should_exit = True
                exit_reason = "volume_money_flow_weakening"
            
            # Priority 3: Time-based protection (trade open for hours, protect gains)
            elif time_since_entry >= 3.0 and pnl_pct >= 0.015:  # 3+ hours, 1.5%+ profit
                if weakening_volume or momentum_loss or volatility_drop:
                    should_exit = True
                    exit_reason = "time_based_profit_protection"
            
            # Priority 4: Original conditions
            elif pnl_pct >= early_exit_target and (weakening_volume or momentum_loss or volatility_drop):
                should_exit = True
                exit_reason = "early_profit_lock"
            
            if should_exit:
                return ExitStrategyResponse(
                    action="exit",
                    exit_price=current_price,
                    reason=exit_reason,
                    trailing_stop=None,
                    indicators={**indicators, "pnl_pct": pnl_pct, "time_since_entry_hours": time_since_entry},
                )

        return ExitStrategyResponse(
            action="hold",
            exit_price=None,
            reason="hold_signal",
            trailing_stop=trailing_stop,
            indicators={**indicators, "pnl_pct": pnl_pct},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def start(self):
        self.is_active = True

    def stop(self):
        self.is_active = False

    def get_effective_entry_window_times(self) -> Tuple[time, time, str, str]:
        """
        Morning band: market_open + morning_wait_minutes_after_open .. entry_window_end (ET).
        If morning_wait_minutes_after_open is omitted, uses entry_window_start / entry_window_end strings.
        """
        cfg = self.config
        end_s = cfg.get("entry_window_end", "10:15")
        end_time = self._parse_time(end_s)
        wait = cfg.get("morning_wait_minutes_after_open")
        if wait is not None:
            open_s = cfg.get("market_open_time", "09:30")
            open_time = self._parse_time(open_s)
            today = datetime.now(self.timezone).date()
            open_dt = self.timezone.localize(datetime.combine(today, open_time))
            start_dt = open_dt + timedelta(minutes=int(wait))
            start_time = start_dt.time()
        else:
            start_s = cfg.get("entry_window_start", "09:42")
            start_time = self._parse_time(start_s)
        start_str = start_time.strftime("%H:%M")
        end_str = end_time.strftime("%H:%M")
        return start_time, end_time, start_str, end_str

    def _is_within_time_window(self, current: time, start: time, end: time) -> bool:
        return start <= current <= end

    def _today_at(self, t: time) -> datetime:
        today = datetime.now(self.timezone)
        return self.timezone.localize(datetime.combine(today.date(), t))

    def _calculate_return(self, side: str, entry_price: float, current_price: float) -> float:
        if entry_price == 0:
            return 0.0
        if side == "buy":
            return (current_price - entry_price) / entry_price
        return (entry_price - current_price) / entry_price

    def _parse_time(self, time_str: str) -> time:
        return datetime.strptime(time_str, "%H:%M").time()

    def _build_exit_indicators(self, df: pd.DataFrame) -> Dict[str, Any]:
        recent = df.tail(30)
        latest = recent.iloc[-1]
        volume_avg = recent["volume"].mean()
        volume_ratio = latest["volume"] / volume_avg if volume_avg else 1.0
        volatility_series = recent["volatility_10"].fillna(0)
        volatility_change = (volatility_series.iloc[-1] - volatility_series.mean()) / (volatility_series.mean() or 1)
        momentum_change = recent["close"].pct_change().tail(5).mean()
        money_flow = recent["money_flow_volume"].tail(10).sum()
        
        try:
            from .data_feed import AlphaVantageDataFeed

            hourly_analysis = AlphaVantageDataFeed.analyze_hourly_trends(df)
        except Exception:
            hourly_analysis = {}

        return {
            "volume_ratio": float(volume_ratio),
            "volatility_change": float(volatility_change),
            "momentum_change": float(momentum_change or 0),
            "money_flow": float(money_flow or 0),
            "last_price": float(latest["close"]),
            "hourly_analysis": hourly_analysis,
        }

