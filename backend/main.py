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
from .pipeline import describe_pipeline, pipeline_status as build_pipeline_status

config = load_config()
auth = TastytradeAuth()
data_feed = AlphaVantageDataFeed(config)
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


@app.route("/pipeline/describe", methods=["GET"])
def pipeline_describe():
    """How Alpha Vantage (data) and Tastytrade (execution) connect in this project."""
    return jsonify(describe_pipeline())


@app.route("/pipeline/status", methods=["GET"])
def pipeline_status_route():
    """Readiness: env vars + optional Tastytrade account probe."""
    return jsonify(
        build_pipeline_status(data_feed=data_feed, auth=auth, trade_executor=trade_executor)
    )


@app.route("/config/execution", methods=["GET"])
def execution_config():
    """Read-only execution / sizing parameters (config + computed morning entry band in ET)."""
    cfg = load_config()
    _, _, start_s, end_s = strategy.get_effective_entry_window_times()
    wait = cfg.get("morning_wait_minutes_after_open")
    open_s = cfg.get("market_open_time", "09:30")
    if wait is not None:
        desc = (
            f"{start_s}–{end_s} ET — entries after US open ({open_s}) + {wait} min wait, "
            f"through {end_s}."
        )
    else:
        desc = f"{start_s}–{end_s} ET — from entry_window_start / entry_window_end."

    return jsonify(
        {
            "timezone": cfg.get("timezone", "US/Eastern"),
            "market_open_time": open_s,
            "morning_wait_minutes_after_open": wait,
            "entry_window_end": cfg.get("entry_window_end", "10:15"),
            "entry_band_start": start_s,
            "entry_band_end": end_s,
            "entry_band_description": desc,
            "order_type": cfg.get("order_type", "Market"),
            "pullback_entry_enabled": bool(cfg.get("pullback_entry_enabled", True)),
            "pullback_min_retrace_pct": float(cfg.get("pullback_min_retrace_pct", 0.0015)),
            "pullback_lookback_bars": int(cfg.get("pullback_lookback_bars", 5)),
            "buying_power_reserve_pct": float(cfg.get("buying_power_reserve_pct", 0.0)),
            "max_position_pct_of_buying_power": float(
                cfg.get("max_position_pct_of_buying_power", 0.25)
            ),
            "default_quantity": int(cfg.get("default_quantity", 100)),
            "forced_exit_time": cfg.get("forced_exit_time", "15:30"),
            "min_confidence": float(cfg.get("min_confidence", 65)),
        }
    )


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
            reserve_pct=float(config.get("buying_power_reserve_pct", 0.0) or 0.0),
        )
        if quantity <= 0:
            return jsonify({"detail": f"Cannot trade: {size_reason}"}), 400
        quantity = min(quantity, requested_qty)
        stop_loss = max(config.get("stop_loss_pct", 0.05), analysis.stop_loss or 0.05)

        order_type = entry_signal.order_type or config.get("order_type", "Market")
        order = trade_executor.place_order(
            symbol=entry_signal.symbol,
            side=entry_signal.side,
            quantity=quantity,
            order_type=order_type,
        )

        db = next(get_db())
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
