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
        entry_start = self.config.get("entry_window_start", "09:30")
        entry_end = self.config.get("entry_window_end", "10:00")
        start_time = datetime.strptime(entry_start, "%H:%M").time()
        end_time = datetime.strptime(entry_end, "%H:%M").time()

        if not self._is_within_time_window(now_et.time(), start_time, end_time):
            # Entry evaluation is only valid during the entry window
            return None

        entry_window_df = df.between_time(entry_start, entry_end)
        if entry_window_df.empty:
            return None

        # Locate swing low (for long) or swing high (for short) within the window
        if side == "buy":
            candidate_row = entry_window_df.nsmallest(1, "low").iloc[0]
            base_price = candidate_row["low"]
        else:
            candidate_row = entry_window_df.nlargest(1, "high").iloc[0]
            base_price = candidate_row["high"]

        buffer_pct = self.config.get("entry_price_buffer", 0.001)
        entry_price = base_price * (1 + buffer_pct if side == "buy" else 1 - buffer_pct)

        volume_surge = candidate_row["volume"] > (candidate_row.get("volume_sma_10") or entry_window_df["volume"].mean()) * 1.15
        money_flow_strength = candidate_row.get("money_flow_volume", 0)
        volatility_burst = abs(candidate_row.get("volatility_10", 0)) > entry_window_df["volatility_10"].mean()

        rationale = ai_analysis.notes or "AI-directed entry based on intraday swing analysis"

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
            },
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

        # Partial profit / early exit logic
        if 0 < pnl_pct < take_profit:
            weakening_volume = indicators.get("volume_ratio", 1) < 0.9
            momentum_loss = indicators.get("momentum_change", 0) < 0 if side == "buy" else indicators.get("momentum_change", 0) > 0
            volatility_drop = indicators.get("volatility_change", 0) < -0.15
            if pnl_pct >= early_exit_target and (weakening_volume or momentum_loss or volatility_drop):
                return ExitStrategyResponse(
                    action="exit",
                    exit_price=current_price,
                    reason="early_profit_lock",
                    trailing_stop=None,
                    indicators={**indicators, "pnl_pct": pnl_pct},
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

        return {
            "volume_ratio": float(volume_ratio),
            "volatility_change": float(volatility_change),
            "momentum_change": float(momentum_change or 0),
            "money_flow": float(money_flow or 0),
            "last_price": float(latest["close"]),
        }

