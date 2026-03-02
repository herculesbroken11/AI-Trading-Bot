from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy.orm import Session
from sqlalchemy import select
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
from .capital_manager import calculate_position_size, should_allow_trade

config = load_config()
auth = TastytradeAuth()
data_feed = AlphaVantageDataFeed()
strategy = TradingStrategy(config)
trade_executor = TradeExecutor(auth)
ai_engine: Optional[AIDecisionEngine] = None
bot_manager: Optional[TradingBotManager] = None

app = Flask(__name__)
CORS(app)

# Initialize on startup
init_db()
ai_engine = AIDecisionEngine()
bot_manager = TradingBotManager(data_feed, ai_engine, trade_executor, strategy)

@app.route("/", methods=["GET"])
def root():
    return jsonify({"message": "AI ETF Trading Bot API"})

@app.route("/auth/tastytrade/url", methods=["GET"])
def get_auth_url():
    return jsonify({"auth_url": auth.get_auth_url()})

@app.route("/auth/tastytrade", methods=["POST"])
def authenticate():
    try:
        data = request.get_json() or {}
        code = data.get("code") or request.args.get("code")
        if not code:
            return jsonify({"error": "code parameter required"}), 400
        result = auth.exchange_code_for_token(code)
        return jsonify({"status": "authenticated", "access_token": result.get("access_token")})
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 400

@app.route("/data/fetch", methods=["GET"])
def fetch_data():
    try:
        symbols = request.args.get("symbols", "TNA,TZA")
        interval = request.args.get("interval", "1min")
        data_payload = {}
        for symbol in symbols.split(","):
            symbol = symbol.strip().upper()
            df = data_feed.fetch_intraday(symbol, interval=interval)
            summary = data_feed.summarize_intraday(df)
            data_payload[symbol] = {
                "summary": summary,
                "candles": _df_to_records(df.tail(180))
            }
        return jsonify({"data": data_payload})
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/ai/analyze", methods=["POST"])
def analyze_market():
    if not ai_engine or not bot_manager:
        return jsonify({"detail": "AI engine not initialised"}), 503

    try:
        data = request.get_json() or {}
        req = AIAnalysisRequest(**data)
        
        data_map: Dict[str, pd.DataFrame] = {}
        summaries: Dict[str, Dict] = {}
        for symbol in req.symbols:
            df = data_feed.fetch_intraday(symbol, interval=req.timeframe)
            data_map[symbol] = df
            summaries[symbol] = data_feed.summarize_intraday(df, lookback_minutes=req.lookback_minutes)
        trend_context = bot_manager.build_trend_context(req.symbols)
        analysis, raw_json = ai_engine.analyze_market(summaries, data_map, trend_context)
        
        db = next(get_db())
        try:
            TradeLogger.log_prediction(db, analysis.recommended_symbol or "PAIR", analysis.model_dump(), raw_json)
            bot_manager.log_trend_summary(db, trend_context)
        finally:
            db.close()
        
        return jsonify(analysis.model_dump())
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/strategy/entry", methods=["POST"])
def strategy_entry():
    if not ai_engine:
        return jsonify({"detail": "AI engine not initialised"}), 503

    try:
        data = request.get_json() or {}
        req = EntryStrategyRequest(**data)
        data_map = _build_data_map(req.symbol, req.intraday_data)
        entry = strategy.evaluate_entry(req.ai_analysis, data_map)
        if not entry:
            return jsonify({"detail": "No valid entry signal"}), 400
        return jsonify(entry.model_dump())
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/strategy/exit", methods=["POST"])
def strategy_exit():
    try:
        data = request.get_json() or {}
        req = ExitStrategyRequest(**data)
        df = _resolve_dataframe(req.symbol, req.intraday_data)
        request_data = req.model_dump(exclude={"ai_analysis", "intraday_data"})
        exit_signal = strategy.evaluate_exit(request_data, df, req.ai_analysis)
        return jsonify(exit_signal.model_dump())
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/trade/execute", methods=["POST"])
def execute_trade():
    if not ai_engine or not bot_manager:
        return jsonify({"detail": "AI engine not initialised"}), 503

    try:
        data = request.get_json() or {}
        req = TradeRequest(**data)
        
        symbols = config.get("symbols", ["TNA", "TZA"])
        data_map: Dict[str, pd.DataFrame] = {}
        summaries: Dict[str, Dict] = {}
        for symbol in symbols:
            df = data_feed.fetch_intraday(symbol)
            data_map[symbol] = df
            summaries[symbol] = data_feed.summarize_intraday(df)
        trend_context = bot_manager.build_trend_context(symbols)
        analysis, raw_json = ai_engine.analyze_market(summaries, data_map, trend_context)
        entry_signal = strategy.evaluate_entry(analysis, data_map)
        if not entry_signal:
            return jsonify({"detail": "Strategy conditions not met"}), 400

        try:
            account_info = trade_executor.get_account_info()
            buying_power = float(account_info.get("buying_power") or account_info.get("balance") or 0)
        except Exception:
            buying_power = 100000

        requested_qty = req.quantity or config.get("default_quantity", 1)
        quantity, size_reason = calculate_position_size(
            buying_power=buying_power,
            entry_price=entry_signal.entry_price,
            default_quantity=requested_qty,
            max_position_pct=config.get("max_position_pct_of_buying_power", 0.25),
            min_buying_power_required=config.get("min_buying_power_required", 5000),
        )
        if quantity <= 0:
            return jsonify({"detail": f"Cannot trade: {size_reason}"}), 400
        quantity = min(quantity, requested_qty)
        stop_loss = max(config.get("stop_loss_pct", 0.05), analysis.stop_loss or 0.05)

        order = trade_executor.place_order(
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            quantity=quantity
        )

        try:
            trade = TradeLogger.log_trade(
                session=db,
                symbol=entry_signal.symbol,
                side=entry_signal.side,
                entry_price=entry_signal.entry_price,
                quantity=quantity,
                confidence=entry_signal.confidence,
                take_profit=analysis.take_profit,
                early_exit_target=analysis.early_exit_profit,
                stop_loss=stop_loss,
                trailing_stop=config.get("trailing_stop_pct") if analysis.use_trailing_stop else None,
                ai_reasoning=entry_signal.rationale,
            )

            TradeLogger.log_prediction(db, analysis.recommended_symbol or entry_signal.symbol, analysis.model_dump(), raw_json)
            bot_manager.log_trend_summary(db, trend_context)
        finally:
            db.close()

        return jsonify({
            "status": "executed",
            "trade": TradeResponse.model_validate(trade).model_dump(),
            "analysis": analysis.model_dump(),
            "order": order
        })
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/trade/close/<int:trade_id>", methods=["POST"])
def close_trade(trade_id: int):
    try:
        data = request.get_json() or {}
        reason = data.get("reason") or request.args.get("reason")
        
        db = next(get_db())
        try:
            trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
            if not trade:
                return jsonify({"detail": "Trade not found"}), 404

            quote = data_feed.fetch_quote(trade.symbol)
            exit_price = quote.get("price", trade.entry_price)
            trade_executor.close_position(trade.symbol)
            closed_trade = TradeLogger.update_trade_exit(
                db,
                trade_id=trade_id,
                exit_price=exit_price,
                exit_reason=reason or "manual_close"
            )
        finally:
            db.close()
        
        return jsonify(TradeResponse.model_validate(closed_trade).model_dump())
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/logs", methods=["GET"])
def get_logs():
    try:
        limit = request.args.get("limit", 100, type=int)
        db = next(get_db())
        try:
            trades = TradeLogger.get_trades(db, limit)
        finally:
            db.close()
        return jsonify([TradeResponse.model_validate(trade).model_dump() for trade in trades])
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/account/balance", methods=["GET"])
def get_balance():
    try:
        info = trade_executor.get_account_info()
        return jsonify(AccountBalance(**info).model_dump())
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 500

@app.route("/bot/start", methods=["POST"])
def start_bot():
    if not bot_manager:
        return jsonify({"detail": "Bot manager not initialised"}), 503
    bot_manager.start()
    return jsonify(BotStatus(
        running=True,
        active_trade_id=bot_manager.active_trade_id,
        last_run=bot_manager.last_run
    ).model_dump())

@app.route("/bot/stop", methods=["POST"])
def stop_bot():
    if not bot_manager:
        return jsonify({"detail": "Bot manager not initialised"}), 503
    bot_manager.stop()
    return jsonify(BotStatus(
        running=False,
        active_trade_id=bot_manager.active_trade_id,
        last_run=bot_manager.last_run
    ).model_dump())

@app.route("/bot/status", methods=["GET"])
def bot_status():
    if not bot_manager:
        return jsonify({"detail": "Bot manager not initialised"}), 503
    return jsonify(BotStatus(
        running=bot_manager.running,
        active_trade_id=bot_manager.active_trade_id,
        last_run=bot_manager.last_run
    ).model_dump())

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

def _build_data_map(symbol: str, intraday_payload: Optional[Dict]) -> Dict[str, pd.DataFrame]:
    other_symbol = "TZA" if symbol.upper() == "TNA" else "TNA"
    data_map = {
        symbol.upper(): _resolve_dataframe(symbol.upper(), intraday_payload)
    }
    data_map[other_symbol] = data_feed.fetch_intraday(other_symbol)
    return data_map

def _resolve_dataframe(symbol: str, intraday_payload: Optional[Dict]) -> pd.DataFrame:
    if intraday_payload and intraday_payload.get("candles"):
        df = pd.DataFrame(intraday_payload["candles"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        for column in ["open", "high", "low", "close", "volume"]:
            if column in df.columns:
                df[column] = df[column].astype(float)
        return df
    df = data_feed.fetch_intraday(symbol)
    return df

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
