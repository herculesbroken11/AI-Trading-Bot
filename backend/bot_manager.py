import threading
import time
import logging
import json
from datetime import datetime, time as dt_time
from typing import Dict, Optional, Any

import pandas as pd

from .config import load_config
from .data_feed import AlphaVantageDataFeed
from .ai_decision import AIDecisionEngine
from .tactics_monitor import TacticsMonitorAI
from .strategy import TradingStrategy
from .trade_exec import TradeExecutor
from .logger import TradeLogger
from .database import SessionLocal
from .models import AIAnalysisResponse
from .capital_manager import calculate_position_size, should_allow_trade

logger = logging.getLogger(__name__)

class TradingBotManager:
    def __init__(
        self,
        data_feed: AlphaVantageDataFeed,
        ai_engine: AIDecisionEngine,
        trade_executor: TradeExecutor,
        strategy: TradingStrategy,
    ):
        self.data_feed = data_feed
        self.ai_engine = ai_engine
        self.tactics_monitor = TacticsMonitorAI()  # Dual AI system
        self.trade_executor = trade_executor
        self.strategy = strategy
        self.config = load_config()

        self.thread: Optional[threading.Thread] = None
        self.running: bool = False
        self.active_trade_id: Optional[int] = None
        self.last_run: Optional[datetime] = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.strategy.start()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Trading bot started")

    def stop(self):
        self.running = False
        self.strategy.stop()
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None
        logger.info("Trading bot stopped")

    def _run_loop(self):
        interval = self.config.get("poll_interval_seconds", 60)
        while self.running:
            self.last_run = datetime.utcnow()
            try:
                self._run_cycle()
            except Exception as exc:
                logger.exception("Error in bot cycle: %s", exc)
            time.sleep(interval)

    def _run_cycle(self):
        session = SessionLocal()
        try:
            if not self.active_trade_id:
                if self._is_entry_window():
                    self._attempt_entry(session)
            else:
                self._monitor_trade(session)
        finally:
            session.close()

    def _attempt_entry(self, session):
        symbols = self.config.get("symbols", ["TNA", "TZA"])
        data_map: Dict[str, pd.DataFrame] = {}
        summaries: Dict[str, Dict[str, Any]] = {}
        for symbol in symbols:
            df = self.data_feed.fetch_intraday(symbol)
            data_map[symbol] = df
            summaries[symbol] = self.data_feed.summarize_intraday(df, lookback_minutes=60)

        trend_context = self.build_trend_context(symbols)
        ai_response, raw_json = self.ai_engine.analyze_market(summaries, data_map, trend_context)

        # Check if Tactics Monitor recommends skipping today
        market_summary = summaries.get("TNA", {}) or summaries.get("TZA", {})
        should_skip, skip_reason = self.tactics_monitor.should_skip_trading_today(
            market_data={**market_summary, "confidence": ai_response.confidence},
            trend_context=trend_context
        )
        
        if should_skip:
            logger.info(f"Skipping trade today: {skip_reason}")
            return

        TradeLogger.log_prediction(session, ai_response.recommended_symbol or "PAIR", ai_response.model_dump(), raw_json)
        self.log_trend_summary(session, trend_context)

        entry_signal = self.strategy.evaluate_entry(ai_response, data_map)
        if not entry_signal:
            return

        # Capital management: follow available funds first, avoid overtrading
        try:
            account_info = self.trade_executor.get_account_info()
            buying_power = float(account_info.get("buying_power") or account_info.get("balance") or 0)
        except Exception as e:
            logger.warning("Could not get account info for capital check: %s. Using default quantity.", e)
            buying_power = 0  # Will use default if we can't check

        max_trades = self.config.get("max_trades_per_day", 1)
        trades_today = TradeLogger.count_trades_closed_today(session)
        allow, allow_reason = should_allow_trade(
            buying_power=buying_power if buying_power > 0 else 999999,  # Allow if we couldn't fetch
            min_buying_power_required=self.config.get("min_buying_power_required", 5000),
            has_open_position=bool(self.active_trade_id),
            trades_today=trades_today,
            max_trades_per_day=max_trades,
        )
        if not allow:
            logger.info("Skipping trade (capital rules): %s", allow_reason)
            return

        quantity, size_reason = calculate_position_size(
            buying_power=buying_power if buying_power > 0 else 100000,  # Fallback for paper mode
            entry_price=entry_signal.entry_price,
            default_quantity=self.config.get("default_quantity", 100),
            max_position_pct=self.config.get("max_position_pct_of_buying_power", 0.25),
            min_buying_power_required=self.config.get("min_buying_power_required", 5000),
            reserve_pct=float(self.config.get("buying_power_reserve_pct", 0.0) or 0.0),
        )
        if quantity <= 0:
            logger.info("Skipping trade (position size): %s", size_reason)
            return

        if size_reason != "ok":
            logger.info("Position size adjusted: %s", size_reason)

        # Use config stop_loss (far enough to not trigger too often)
        stop_loss = self.config.get("stop_loss_pct", 0.05)
        if ai_response.stop_loss and ai_response.stop_loss > stop_loss:
            stop_loss = ai_response.stop_loss  # Use AI value if it's wider

        # Execute trade via Tastytrade
        order_type = entry_signal.order_type or self.config.get("order_type", "Market")
        order = self.trade_executor.place_order(
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            quantity=quantity,
            order_type=order_type,
        )

        trade = TradeLogger.log_trade(
            session=session,
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            entry_price=entry_signal.entry_price,
            quantity=quantity,
            confidence=entry_signal.confidence,
            take_profit=ai_response.take_profit,
            early_exit_target=ai_response.early_exit_profit,
            stop_loss=stop_loss,
            trailing_stop=self.config.get("trailing_stop_pct") if ai_response.use_trailing_stop else None,
            ai_reasoning=entry_signal.rationale,
            entry_time=datetime.utcnow()
        )

        self.active_trade_id = trade.trade_id
        logger.info("Entered trade %s on %s", trade.trade_id, trade.symbol)

    def _monitor_trade(self, session):
        trade = TradeLogger.get_trade(session, self.active_trade_id)
        if not trade:
            self.active_trade_id = None
            return

        df = self.data_feed.fetch_intraday(trade.symbol)
        ai_stub = AIAnalysisResponse(
            direction="bullish" if trade.side == "buy" else "bearish",
            confidence=trade.confidence,
            entry_price=trade.entry_price,
            take_profit=trade.take_profit,
            early_exit_profit=trade.early_exit_target,
            stop_loss=trade.stop_loss,
            use_trailing_stop=bool(trade.trailing_stop),
            skip_trade=False
        )

        quote = self.data_feed.fetch_quote(trade.symbol)
        current_price = quote.get("price", trade.entry_price)

        exit_decision = self.strategy.evaluate_exit(
            request_data={
                "trade_id": trade.trade_id,
                "symbol": trade.symbol,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "take_profit": trade.take_profit,
                "early_exit_target": trade.early_exit_target,
                "stop_loss": trade.stop_loss,
                "trailing_stop": trade.trailing_stop,
                "entry_time": trade.entry_time,
                "current_price": current_price,
            },
            df=df,
            ai_analysis=ai_stub
        )

        # Dual AI system: Check if Tactics Monitor wants to override Pattern AI decision
        if exit_decision.action == "hold":
            hourly_analysis = exit_decision.indicators.get("hourly_analysis", {})
            time_since_entry = (datetime.utcnow() - trade.entry_time).total_seconds() / 3600.0 if trade.entry_time else 0.0
            pnl_pct = (current_price - trade.entry_price) / trade.entry_price if trade.side == "buy" else (trade.entry_price - current_price) / trade.entry_price
            
            override_action, override_reason, override_metadata = self.tactics_monitor.evaluate_tactics_override(
                current_pnl_pct=pnl_pct,
                entry_price=trade.entry_price,
                current_price=current_price,
                hourly_analysis=hourly_analysis,
                pattern_ai_decision=exit_decision.action,
                time_since_entry_hours=time_since_entry,
                volume_indicators=exit_decision.indicators
            )
            
            if override_action == "override_exit":
                # Tactics Monitor overrides Pattern AI - exit with profit protection
                logger.info("Tactics Monitor override: %s (Pattern AI wanted to hold)", override_reason)
                self.trade_executor.close_position(trade.symbol)
                TradeLogger.update_trade_exit(
                    session,
                    trade_id=trade.trade_id,
                    exit_price=current_price,
                    exit_reason=f"tactics_override: {override_reason}",
                )
                self.active_trade_id = None
                return

        if exit_decision.action == "exit":
            self.trade_executor.close_position(trade.symbol)
            TradeLogger.update_trade_exit(
                session,
                trade_id=trade.trade_id,
                exit_price=exit_decision.exit_price or current_price,
                exit_reason=exit_decision.reason or "exit_signal",
            )
            self.active_trade_id = None
            logger.info("Trade %s closed: %s", trade.trade_id, exit_decision.reason)
        elif exit_decision.action == "hold" and exit_decision.trailing_stop:
            TradeLogger.update_trailing_stop(session, trade.trade_id, exit_decision.trailing_stop)

    def _is_entry_window(self) -> bool:
        start_time, end_time, _, _ = self.strategy.get_effective_entry_window_times()
        now_et = datetime.now(self.strategy.timezone).time()
        return start_time <= now_et <= end_time

    def build_trend_context(self, symbols):
        """Enhanced trend context with 6-month analysis and seasonal patterns"""
        context: Dict[str, Any] = {}
        now = datetime.utcnow()
        month = now.month
        
        for symbol in symbols:
            daily = self.data_feed.fetch_daily(symbol, outputsize="full")  # Get more data
            last_120 = daily.tail(120)  # ~6 months of trading days
            if last_120.empty:
                continue
            
            # 6-month trend analysis
            total_return = (last_120["close"].iloc[-1] / last_120["close"].iloc[0]) - 1
            
            # Recent trend (last 30 days vs previous 30 days)
            if len(last_120) >= 60:
                recent_30 = last_120.tail(30)
                prev_30 = last_120.tail(60).head(30)
                recent_return = (recent_30["close"].iloc[-1] / recent_30["close"].iloc[0]) - 1
                prev_return = (prev_30["close"].iloc[-1] / prev_30["close"].iloc[0]) - 1
                trend_accelerating = recent_return > prev_return * 1.2
                trend_decelerating = recent_return < prev_return * 0.8
            else:
                recent_return = total_return
                trend_accelerating = False
                trend_decelerating = False
            
            volatility = last_120["rolling_volatility"].iloc[-1]
            
            context[symbol] = {
                "total_return_pct": round(float(total_return) * 100, 2),
                "recent_return_pct": round(float(recent_return) * 100, 2),
                "volatility_annualized": round(float(volatility or 0), 3),
                "trend": "bull" if total_return > 0 else "bear",
                "trend_accelerating": trend_accelerating,
                "trend_decelerating": trend_decelerating,
                "trend_strength": "strong" if abs(total_return) > 0.15 else "moderate" if abs(total_return) > 0.05 else "weak",
            }
        
        # Enhanced seasonal messages
        seasonal_messages = []
        if month in [11, 12]:
            seasonal_messages.append("Christmas rally period - bull market typically lasts rest of year")
        if month in [1, 2]:
            seasonal_messages.append("Jan-Feb institutional selling period - watch for trend reversal")
        if month == 12:
            seasonal_messages.append("Corporate tax timing may affect institutional selling")
        
        context["seasonal_message"] = " | ".join(seasonal_messages) if seasonal_messages else ""
        context["institutional_selling_period"] = month in [1, 2]
        context["christmas_rally_period"] = month in [11, 12]
        
        return context

    def log_trend_summary(self, session, trend_context):
        for symbol, details in trend_context.items():
            if not isinstance(details, dict):
                continue
            TradeLogger.log_trend_signal(
                session,
                symbol=symbol if symbol in self.config.get("symbols", []) else "PAIR",
                macro_trend=details.get("trend", "neutral"),
                seasonal_bias=trend_context.get("seasonal_message"),
                institutional_flow=None,
                bias=details.get("trend"),
                notes=json.dumps(details)
            )

    def _parse_time(self, value: str) -> dt_time:
        hour, minute = value.split(":")
        return dt_time(int(hour), int(minute))
