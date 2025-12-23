from sqlalchemy.orm import Session
from sqlalchemy import select
from .database import Trade, AIPrediction, TrendSignal
from typing import List, Optional, Dict, Any
from datetime import datetime

class TradeLogger:
    @staticmethod
    def log_trade(
        session: Session,
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
        session.commit()
        session.refresh(trade)
        return trade

    @staticmethod
    def update_trade_exit(
        session: Session,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        partial_exit_pct: Optional[float] = None
    ) -> Trade:
        """Close a trade and calculate PnL"""
        trade = session.query(Trade).filter(Trade.trade_id == trade_id).first()

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

        session.commit()
        session.refresh(trade)
        return trade

    @staticmethod
    def get_trades(session: Session, limit: int = 100) -> List[Trade]:
        """Get recent trades"""
        return session.query(Trade).order_by(Trade.created_at.desc()).limit(limit).all()

    @staticmethod
    def get_open_trades(session: Session) -> List[Trade]:
        """Get all open trades"""
        return session.query(Trade).filter(Trade.status == "open").all()

    @staticmethod
    def log_prediction(
        session: Session,
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
        session.commit()
        session.refresh(prediction)
        return prediction

    @staticmethod
    def log_trend_signal(
        session: Session,
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
        session.commit()
        session.refresh(trend)
        return trend

    @staticmethod
    def update_trailing_stop(
        session: Session,
        trade_id: int,
        trailing_stop: float
    ) -> Trade:
        trade = session.query(Trade).filter(Trade.trade_id == trade_id).first()
        if not trade:
            raise ValueError(f"Trade {trade_id} not found")
        trade.trailing_stop = trailing_stop
        session.commit()
        session.refresh(trade)
        return trade

    @staticmethod
    def get_trade(session: Session, trade_id: int) -> Optional[Trade]:
        return session.query(Trade).filter(Trade.trade_id == trade_id).first()

