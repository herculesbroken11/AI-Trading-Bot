from __future__ import annotations

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
import pandas as pd

from .database import init_db, get_db, Trade
from .models import (
    TradeRequest,
    TradeResponse,
    AIAnalysisRequest,
    AIAnalysisResponse,
    EntryStrategyRequest,
    EntryStrategyResponse,
    ExitStrategyRequest,
    ExitStrategyResponse,
    AccountBalance,
    BotStatus,
)
from .auth_tastytrade import TastytradeAuth
from .data_feed import AlphaVantageDataFeed
from .ai_decision import AIDecisionEngine
from .strategy import TradingStrategy
from .trade_exec import TradeExecutor
from .logger import TradeLogger
from .config import load_config
from .bot_manager import TradingBotManager
from .database import AsyncSessionLocal

config = load_config()
auth = TastytradeAuth()
data_feed = AlphaVantageDataFeed()
strategy = TradingStrategy(config)
trade_executor = TradeExecutor(auth)
ai_engine: Optional[AIDecisionEngine] = None
bot_manager: Optional[TradingBotManager] = None

@asynccontextmanager
def lifespan(app: FastAPI):
    global ai_engine, bot_manager
    await init_db()
    ai_engine = AIDecisionEngine()
    bot_manager = TradingBotManager(data_feed, ai_engine, trade_executor, strategy)
    yield
    if bot_manager:
        await bot_manager.stop()

app = FastAPI(title="AI ETF Trading Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "AI ETF Trading Bot API"}

@app.get("/auth/tastytrade/url")
async def get_auth_url():
    return {"auth_url": auth.get_auth_url()}

@app.post("/auth/tastytrade")
async def authenticate(code: str):
    try:
        result = await auth.exchange_code_for_token(code)
        return {"status": "authenticated", "access_token": result.get("access_token")}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/data/fetch")
async def fetch_data(symbols: str = Query(default="TNA,TZA"), interval: str = "1min"):
    try:
        data_payload = {}
        for symbol in symbols.split(","):
            symbol = symbol.strip().upper()
            df = await data_feed.fetch_intraday(symbol, interval=interval)
            summary = data_feed.summarize_intraday(df)
            data_payload[symbol] = {
                "summary": summary,
                "candles": _df_to_records(df.tail(180))
            }
        return {"data": data_payload}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/ai/analyze", response_model=AIAnalysisResponse)
async def analyze_market(request: AIAnalysisRequest):
    if not ai_engine or not bot_manager:
        raise HTTPException(status_code=503, detail="AI engine not initialised")

    try:
        data_map: Dict[str, pd.DataFrame] = {}
        summaries: Dict[str, Dict] = {}
        for symbol in request.symbols:
            df = await data_feed.fetch_intraday(symbol, interval=request.timeframe)
            data_map[symbol] = df
            summaries[symbol] = data_feed.summarize_intraday(df, lookback_minutes=request.lookback_minutes)
        trend_context = await bot_manager.build_trend_context(request.symbols)
        analysis, raw_json = await ai_engine.analyze_market(summaries, data_map, trend_context)
        async with AsyncSessionLocal() as session:
            await TradeLogger.log_prediction(session, analysis.recommended_symbol or "PAIR", analysis.model_dump(), raw_json)
            await bot_manager.log_trend_summary(session, trend_context)
        return analysis
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/strategy/entry", response_model=EntryStrategyResponse)
async def strategy_entry(request: EntryStrategyRequest):
    if not ai_engine:
        raise HTTPException(status_code=503, detail="AI engine not initialised")

    try:
        data_map = await _build_data_map(request.symbol, request.intraday_data)
        entry = strategy.evaluate_entry(request.ai_analysis, data_map)
        if not entry:
            raise HTTPException(status_code=400, detail="No valid entry signal")
        return entry
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/strategy/exit", response_model=ExitStrategyResponse)
async def strategy_exit(request: ExitStrategyRequest):
    try:
        df = await _resolve_dataframe(request.symbol, request.intraday_data)
        request_data = request.model_dump(exclude={"ai_analysis", "intraday_data"})
        exit_signal = strategy.evaluate_exit(request_data, df, request.ai_analysis)
        return exit_signal
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/trade/execute")
async def execute_trade(request: TradeRequest, db: AsyncSession = Depends(get_db)):
    if not ai_engine or not bot_manager:
        raise HTTPException(status_code=503, detail="AI engine not initialised")

    try:
        symbols = config.get("symbols", ["TNA", "TZA"])
        data_map: Dict[str, pd.DataFrame] = {}
        summaries: Dict[str, Dict] = {}
        for symbol in symbols:
            df = await data_feed.fetch_intraday(symbol)
            data_map[symbol] = df
            summaries[symbol] = data_feed.summarize_intraday(df)
        trend_context = await bot_manager.build_trend_context(symbols)
        analysis, raw_json = await ai_engine.analyze_market(summaries, data_map, trend_context)
        entry_signal = strategy.evaluate_entry(analysis, data_map)
        if not entry_signal:
            raise HTTPException(status_code=400, detail="Strategy conditions not met")

        order = await trade_executor.place_order(
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            quantity=request.quantity or config.get("default_quantity", 1)
        )

        trade = await TradeLogger.log_trade(
            session=db,
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            entry_price=entry_signal.entry_price,
            quantity=request.quantity or config.get("default_quantity", 1),
            confidence=entry_signal.confidence,
            take_profit=analysis.take_profit,
            early_exit_target=analysis.early_exit_profit,
            stop_loss=analysis.stop_loss,
            trailing_stop=config.get("trailing_stop_pct") if analysis.use_trailing_stop else None,
            ai_reasoning=entry_signal.rationale,
        )

        await TradeLogger.log_prediction(db, analysis.recommended_symbol or entry_signal.symbol, analysis.model_dump(), raw_json)
        await bot_manager.log_trend_summary(db, trend_context)

        return {
            "status": "executed",
            "trade": TradeResponse.model_validate(trade),
            "analysis": analysis,
            "order": order
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/trade/close/{trade_id}")
async def close_trade(trade_id: int, reason: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Trade).where(Trade.trade_id == trade_id))
        trade = result.scalar_one_or_none()
        if not trade:
            raise HTTPException(status_code=404, detail="Trade not found")

        quote = await data_feed.fetch_quote(trade.symbol)
        exit_price = quote.get("price", trade.entry_price)
        await trade_executor.close_position(trade.symbol)
        closed_trade = await TradeLogger.update_trade_exit(
            db,
            trade_id=trade_id,
            exit_price=exit_price,
            exit_reason=reason or "manual_close"
        )
        return TradeResponse.model_validate(closed_trade)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/logs", response_model=List[TradeResponse])
async def get_logs(limit: int = 100, db: AsyncSession = Depends(get_db)):
    trades = await TradeLogger.get_trades(db, limit)
    return [TradeResponse.model_validate(trade) for trade in trades]

@app.get("/account/balance", response_model=AccountBalance)
async def get_balance():
    try:
        info = await trade_executor.get_account_info()
        return AccountBalance(**info)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/bot/start", response_model=BotStatus)
async def start_bot():
    if not bot_manager:
        raise HTTPException(status_code=503, detail="Bot manager not initialised")
    await bot_manager.start()
    return BotStatus(running=True, active_trade_id=bot_manager.active_trade_id, last_run=bot_manager.last_run)

@app.post("/bot/stop", response_model=BotStatus)
async def stop_bot():
    if not bot_manager:
        raise HTTPException(status_code=503, detail="Bot manager not initialised")
    await bot_manager.stop()
    return BotStatus(running=False, active_trade_id=bot_manager.active_trade_id, last_run=bot_manager.last_run)

@app.get("/bot/status", response_model=BotStatus)
async def bot_status():
    if not bot_manager:
        raise HTTPException(status_code=503, detail="Bot manager not initialised")
    return BotStatus(running=bot_manager.running, active_trade_id=bot_manager.active_trade_id, last_run=bot_manager.last_run)

# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def _df_to_records(df: pd.DataFrame) -> List[Dict]:
    records = []
    for idx, row in df.iterrows():
        records.append({
            "timestamp": idx.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })
    return records

async def _build_data_map(symbol: str, intraday_payload: Optional[Dict]) -> Dict[str, pd.DataFrame]:
    other_symbol = "TZA" if symbol.upper() == "TNA" else "TNA"
    data_map = {
        symbol.upper(): await _resolve_dataframe(symbol.upper(), intraday_payload)
    }
    data_map[other_symbol] = await data_feed.fetch_intraday(other_symbol)
    return data_map

async def _resolve_dataframe(symbol: str, intraday_payload: Optional[Dict]) -> pd.DataFrame:
    if intraday_payload and intraday_payload.get("candles"):
        df = pd.DataFrame(intraday_payload["candles"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        for column in ["open", "high", "low", "close", "volume"]:
            if column in df.columns:
                df[column] = df[column].astype(float)
        return df
    df = await data_feed.fetch_intraday(symbol)
    return df

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

