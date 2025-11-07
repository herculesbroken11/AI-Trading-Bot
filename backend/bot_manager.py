import asyncio
import logging
import json
from datetime import datetime, time
from typing import Dict, Optional, Any

import pandas as pd

from .config import load_config
from .data_feed import AlphaVantageDataFeed
from .ai_decision import AIDecisionEngine
from .strategy import TradingStrategy
from .trade_exec import TradeExecutor
from .logger import TradeLogger
from .database import AsyncSessionLocal
from .models import AIAnalysisResponse

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
        self.trade_executor = trade_executor
        self.strategy = strategy
        self.config = load_config()

        self.task: Optional[asyncio.Task] = None
        self.running: bool = False
        self.active_trade_id: Optional[int] = None
        self.last_run: Optional[datetime] = None

    async def start(self):
        if self.running:
            return
        self.running = True
        self.strategy.start()
        self.task = asyncio.create_task(self._run_loop())
        logger.info("Trading bot started")

    async def stop(self):
        self.running = False
        self.strategy.stop()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("Trading bot stopped")

    async def _run_loop(self):
        interval = self.config.get("poll_interval_seconds", 60)
        while self.running:
            self.last_run = datetime.utcnow()
            try:
                await self._run_cycle()
            except Exception as exc:
                logger.exception("Error in bot cycle: %s", exc)
            await asyncio.sleep(interval)

    async def _run_cycle(self):
        async with AsyncSessionLocal() as session:
            if not self.active_trade_id:
                if self._is_entry_window():
                    await self._attempt_entry(session)
            else:
                await self._monitor_trade(session)

    async def _attempt_entry(self, session):
        symbols = self.config.get("symbols", ["TNA", "TZA"])
        data_map: Dict[str, pd.DataFrame] = {}
        summaries: Dict[str, Dict[str, Any]] = {}
        for symbol in symbols:
            df = await self.data_feed.fetch_intraday(symbol)
            data_map[symbol] = df
            summaries[symbol] = self.data_feed.summarize_intraday(df, lookback_minutes=60)

        trend_context = await self.build_trend_context(symbols)
        ai_response, raw_json = await self.ai_engine.analyze_market(summaries, data_map, trend_context)

        await TradeLogger.log_prediction(session, ai_response.recommended_symbol or "PAIR", ai_response.model_dump(), raw_json)
        await self.log_trend_summary(session, trend_context)

        entry_signal = self.strategy.evaluate_entry(ai_response, data_map)
        if not entry_signal:
            return

        # Execute trade via Tastytrade
        order = await self.trade_executor.place_order(
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            quantity=self.config.get("default_quantity", 1)
        )

        trade = await TradeLogger.log_trade(
            session=session,
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            entry_price=entry_signal.entry_price,
            quantity=self.config.get("default_quantity", 1),
            confidence=entry_signal.confidence,
            take_profit=ai_response.take_profit,
            early_exit_target=ai_response.early_exit_profit,
            stop_loss=ai_response.stop_loss,
            trailing_stop=self.config.get("trailing_stop_pct") if ai_response.use_trailing_stop else None,
            ai_reasoning=entry_signal.rationale,
            entry_time=datetime.utcnow()
        )

        self.active_trade_id = trade.trade_id
        logger.info("Entered trade %s on %s", trade.trade_id, trade.symbol)

    async def _monitor_trade(self, session):
        trade = await TradeLogger.get_trade(session, self.active_trade_id)
        if not trade:
            self.active_trade_id = None
            return

        df = await self.data_feed.fetch_intraday(trade.symbol)
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

        quote = await self.data_feed.fetch_quote(trade.symbol)
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

        if exit_decision.action == "exit":
            await self.trade_executor.close_position(trade.symbol)
            await TradeLogger.update_trade_exit(
                session,
                trade_id=trade.trade_id,
                exit_price=exit_decision.exit_price or current_price,
                exit_reason=exit_decision.reason or "exit_signal",
            )
            self.active_trade_id = None
            logger.info("Trade %s closed: %s", trade.trade_id, exit_decision.reason)
        elif exit_decision.action == "hold" and exit_decision.trailing_stop:
            await TradeLogger.update_trailing_stop(session, trade.trade_id, exit_decision.trailing_stop)

    def _is_entry_window(self) -> bool:
        entry_start = self._parse_time(self.config.get("entry_window_start", "09:30"))
        entry_end = self._parse_time(self.config.get("entry_window_end", "10:00"))
        now_et = datetime.now(self.strategy.timezone).time()
        return entry_start <= now_et <= entry_end

    async def build_trend_context(self, symbols):
        context: Dict[str, Any] = {}
        for symbol in symbols:
            daily = await self.data_feed.fetch_daily(symbol, outputsize="compact")
            last_120 = daily.tail(120)
            if last_120.empty:
                continue
            total_return = (last_120["close"].iloc[-1] / last_120["close"].iloc[0]) - 1
            volatility = last_120["rolling_volatility"].iloc[-1]
            context[symbol] = {
                "total_return_pct": round(float(total_return) * 100, 2),
                "volatility_annualized": round(float(volatility or 0), 3),
                "trend": "bull" if total_return > 0 else "bear",
            }
        context["seasonal_message"] = "Watch for holiday rally" if datetime.utcnow().month in [11, 12] else ""
        return context

    async def log_trend_summary(self, session, trend_context):
        for symbol, details in trend_context.items():
            if not isinstance(details, dict):
                continue
            await TradeLogger.log_trend_signal(
                session,
                symbol=symbol if symbol in self.config.get("symbols", []) else "PAIR",
                macro_trend=details.get("trend", "neutral"),
                seasonal_bias=trend_context.get("seasonal_message"),
                institutional_flow=None,
                bias=details.get("trend"),
                notes=json.dumps(details)
            )

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
