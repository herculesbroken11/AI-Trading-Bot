from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .database import Trade, AIPrediction, TrendSignal
from typing import List, Optional, Dict, Any
from datetime import datetime

class TradeLogger:
    @staticmethod
    async def log_trade(
        session: AsyncSession,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: int,
        confidence: float,
        take_profit: float,
        early_exit_target: float,
        stop_loss: float,
        trailing_stop: Optional[float] = None,
        ai_reasoning: Optional[str] = None,
        entry_time: Optional[datetime] = None
    ) -> Trade:
        """Log a new trade"""
        trade = Trade(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            confidence=confidence,
            take_profit=take_profit,
            early_exit_target=early_exit_target,
            stop_loss=stop_loss,
            trailing_stop=trailing_stop,
            ai_reasoning=ai_reasoning,
            status="open",
            entry_time=entry_time or datetime.utcnow()
        )
        session.add(trade)
        await session.commit()
        await session.refresh(trade)
        return trade

    @staticmethod
    async def update_trade_exit(
        session: AsyncSession,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        partial_exit_pct: Optional[float] = None
    ) -> Trade:
        """Close a trade and calculate PnL"""
        result = await session.execute(select(Trade).where(Trade.trade_id == trade_id))
        trade = result.scalar_one_or_none()

        if not trade:
            raise ValueError(f"Trade {trade_id} not found")

        trade.exit_price = exit_price
        trade.status = "closed"
        trade.closed_at = datetime.utcnow()
        trade.exit_reason = exit_reason
        trade.partial_exit_pct = partial_exit_pct

        # Calculate PnL
        if trade.side == "buy":
            trade.pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            trade.pnl = (trade.entry_price - exit_price) * trade.quantity

        await session.commit()
        await session.refresh(trade)
        return trade

    @staticmethod
    async def get_trades(session: AsyncSession, limit: int = 100) -> List[Trade]:
        """Get recent trades"""
        result = await session.execute(
            select(Trade).order_by(Trade.created_at.desc()).limit(limit)
        )
        return result.scalars().all()

    @staticmethod
    async def get_open_trades(session: AsyncSession) -> List[Trade]:
        """Get all open trades"""
        result = await session.execute(
            select(Trade).where(Trade.status == "open")
        )
        return result.scalars().all()

    @staticmethod
    async def log_prediction(
        session: AsyncSession,
        symbol: str,
        analysis: Dict[str, Any],
        raw_response: str
    ) -> AIPrediction:
        prediction = AIPrediction(
            symbol=symbol,
            direction=analysis.get("direction"),
            confidence=analysis.get("confidence"),
            entry_price=analysis.get("entry_price"),
            take_profit=analysis.get("take_profit"),
            early_exit_profit=analysis.get("early_exit_profit"),
            stop_loss=analysis.get("stop_loss"),
            use_trailing_stop=analysis.get("use_trailing_stop", False),
            skip_trade=analysis.get("skip_trade", False),
            analysis_json=raw_response
        )
        session.add(prediction)
        await session.commit()
        await session.refresh(prediction)
        return prediction

    @staticmethod
    async def log_trend_signal(
        session: AsyncSession,
        symbol: str,
        macro_trend: str,
        seasonal_bias: Optional[str],
        institutional_flow: Optional[str],
        bias: Optional[str],
        notes: Optional[str]
    ) -> TrendSignal:
        trend = TrendSignal(
            symbol=symbol,
            macro_trend=macro_trend,
            seasonal_bias=seasonal_bias,
            institutional_flow=institutional_flow,
            bias=bias,
            notes=notes
        )
        session.add(trend)
        await session.commit()
        await session.refresh(trend)
        return trend

    @staticmethod
    async def update_trailing_stop(
        session: AsyncSession,
        trade_id: int,
        trailing_stop: float
    ) -> Trade:
        result = await session.execute(select(Trade).where(Trade.trade_id == trade_id))
        trade = result.scalar_one_or_none()
        if not trade:
            raise ValueError(f"Trade {trade_id} not found")
        trade.trailing_stop = trailing_stop
        await session.commit()
        await session.refresh(trade)
        return trade

    @staticmethod
    async def get_trade(session: AsyncSession, trade_id: int) -> Optional[Trade]:
        result = await session.execute(select(Trade).where(Trade.trade_id == trade_id))
        return result.scalar_one_or_none()

