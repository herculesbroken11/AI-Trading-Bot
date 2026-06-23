"""Flask application factory with Phase 2 startup safety."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

from backend.api.middleware.api_key_auth import require_api_key
from backend.bot_manager import TradingBotManager
from backend.capital_manager import calculate_position_size
from backend.config import load_config
from backend.config.settings import Settings, get_settings, load_settings, reset_settings_cache
from backend.database import Trade, get_db, init_db
from backend.models import (
    AIAnalysisRequest,
    BotStatus,
    EntryStrategyRequest,
    ExitStrategyRequest,
    TradeRequest,
    TradeResponse,
)
from backend.pipeline import describe_pipeline, pipeline_status as build_pipeline_status
from backend.safety.runtime_gate import legacy_trading_block_response

logger = logging.getLogger(__name__)


def create_app(
    settings: Optional[Settings] = None,
    *,
    skip_db_init: bool = False,
    defer_heavy_services: bool = False,
) -> Flask:
    """Build Flask app with Phase 2 safety checks."""
    if settings is None:
        reset_settings_cache()
        settings = load_settings()
    settings.validate_startup()

    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    CORS(app)

    logger.info("Startup settings: %s", settings.safe_summary())

    if not skip_db_init:
        try:
            init_db()
        except Exception as exc:
            logger.error("Database initialization failed: %s", type(exc).__name__)

    config = load_config()
    auth = None
    data_feed = None
    strategy = None
    trade_executor = None
    ai_engine = None
    bot_manager = None

    if not defer_heavy_services:
        from backend.ai_decision import AIDecisionEngine
        from backend.auth_tastytrade import TastytradeAuth
        from backend.data_feed import AlphaVantageDataFeed
        from backend.strategy import TradingStrategy
        from backend.trade_exec import TradeExecutor

        auth = TastytradeAuth()
        data_feed = AlphaVantageDataFeed(config)
        strategy = TradingStrategy(config)
        trade_executor = TradeExecutor(auth)
        try:
            ai_engine = AIDecisionEngine()
        except Exception as exc:
            logger.warning("AI engine not initialized: %s", type(exc).__name__)
        bot_manager = TradingBotManager(data_feed, ai_engine, trade_executor, strategy)

    app.config["AUTH"] = auth
    app.config["DATA_FEED"] = data_feed
    app.config["STRATEGY"] = strategy
    app.config["TRADE_EXECUTOR"] = trade_executor
    app.config["AI_ENGINE"] = ai_engine
    app.config["BOT_MANAGER"] = bot_manager
    app.config["STRATEGY_CONFIG"] = config

    def _settings() -> Settings:
        return app.config["SETTINGS"]

    def _blocked_if_legacy_trading():
        return legacy_trading_block_response(_settings())

    @app.route("/", methods=["GET"])
    def root():
        return jsonify(
            {
                "message": "AI ETF Trading Bot API",
                "settings": _settings().safe_summary(),
                "endpoints": {
                    "execution_profile": "/config/execution",
                    "pipeline_describe": "/pipeline/describe",
                    "pipeline_status": "/pipeline/status",
                },
            }
        )

    @app.route("/pipeline/describe", methods=["GET"])
    def pipeline_describe():
        return jsonify(describe_pipeline())

    @app.route("/pipeline/status", methods=["GET"])
    def pipeline_status_route():
        return jsonify(
            build_pipeline_status(
                data_feed=app.config["DATA_FEED"],
                auth=app.config["AUTH"],
                trade_executor=app.config["TRADE_EXECUTOR"],
            )
        )

    @app.route("/config/execution", methods=["GET"])
    def execution_config():
        cfg = load_config()
        strat = app.config["STRATEGY"]
        if strat:
            _, _, start_s, end_s = strat.get_effective_entry_window_times()
        else:
            start_s, end_s = "09:42", "10:15"
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
        auth_svc = app.config["AUTH"]
        if not auth_svc:
            return jsonify({"detail": "Auth service not initialised"}), 503
        return jsonify({"auth_url": auth_svc.get_auth_url()})

    @app.route("/auth/tastytrade", methods=["POST"])
    def authenticate():
        auth_svc = app.config["AUTH"]
        if not auth_svc:
            return jsonify({"detail": "Auth service not initialised"}), 503
        try:
            data = request.get_json() or {}
            code = data.get("code") or request.args.get("code")
            if not code:
                return jsonify({"error": "code parameter required"}), 400
            auth_svc.exchange_code_for_token(code)
            return jsonify(
                {
                    "success": True,
                    "authenticated": True,
                    "message": "Tastytrade sandbox authentication stored successfully",
                    "access_token_present": True,
                }
            )
        except Exception as exc:
            return jsonify({"detail": str(exc)}), 400

    @app.route("/data/fetch", methods=["GET"])
    def fetch_data():
        data_feed = app.config["DATA_FEED"]
        if not data_feed:
            return jsonify({"detail": "Data feed not initialised"}), 503
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
                    "candles": _df_to_records(df.tail(180)),
                }
            return jsonify({"data": data_payload})
        except Exception as exc:
            return jsonify({"detail": str(exc)}), 500

    @app.route("/ai/analyze", methods=["POST"])
    def analyze_market():
        ai_engine = app.config["AI_ENGINE"]
        bot_manager = app.config["BOT_MANAGER"]
        data_feed = app.config["DATA_FEED"]
        if not ai_engine or not bot_manager or not data_feed:
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

            from backend.logger import TradeLogger

            db = next(get_db())
            try:
                TradeLogger.log_prediction(
                    db, analysis.recommended_symbol or "PAIR", analysis.model_dump(), raw_json
                )
                bot_manager.log_trend_summary(db, trend_context)
            finally:
                db.close()

            return jsonify(analysis.model_dump())
        except Exception as exc:
            return jsonify({"detail": str(exc)}), 500

    @app.route("/strategy/entry", methods=["POST"])
    def strategy_entry():
        strategy = app.config["STRATEGY"]
        data_feed = app.config["DATA_FEED"]
        if not strategy or not data_feed:
            return jsonify({"detail": "Strategy not initialised"}), 503

        try:
            data = request.get_json() or {}
            req = EntryStrategyRequest(**data)
            data_map = _build_data_map(data_feed, req.symbol, req.intraday_data)
            entry = strategy.evaluate_entry(req.ai_analysis, data_map)
            if not entry:
                return jsonify({"detail": "No valid entry signal"}), 400
            return jsonify(entry.model_dump())
        except Exception as exc:
            return jsonify({"detail": str(exc)}), 500

    @app.route("/strategy/exit", methods=["POST"])
    def strategy_exit():
        strategy = app.config["STRATEGY"]
        data_feed = app.config["DATA_FEED"]
        if not strategy or not data_feed:
            return jsonify({"detail": "Strategy not initialised"}), 503
        try:
            data = request.get_json() or {}
            req = ExitStrategyRequest(**data)
            df = _resolve_dataframe(data_feed, req.symbol, req.intraday_data)
            request_data = req.model_dump(exclude={"ai_analysis", "intraday_data"})
            exit_signal = strategy.evaluate_exit(request_data, df, req.ai_analysis)
            return jsonify(exit_signal.model_dump())
        except Exception as exc:
            return jsonify({"detail": str(exc)}), 500

    @app.route("/trade/execute", methods=["POST"])
    def execute_trade():
        blocked = _blocked_if_legacy_trading()
        if blocked:
            return blocked
        api_block = require_api_key(_settings())
        if api_block:
            return api_block
        return jsonify({"detail": "Legacy execute path disabled"}), 503

    @app.route("/trade/close/<int:trade_id>", methods=["POST"])
    def close_trade(trade_id: int):
        blocked = _blocked_if_legacy_trading()
        if blocked:
            return blocked
        api_block = require_api_key(_settings())
        if api_block:
            return api_block
        return jsonify({"detail": "Legacy close path disabled"}), 503

    @app.route("/logs", methods=["GET"])
    def get_logs():
        from backend.logger import TradeLogger

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
        trade_executor = app.config["TRADE_EXECUTOR"]
        if not trade_executor:
            return jsonify(
                {
                    "balance": None,
                    "buying_power": None,
                    "open_positions": 0,
                    "daily_pnl": 0.0,
                    "status": "unavailable",
                    "message": "Account data unavailable; trading disabled.",
                }
            ), 503
        try:
            info = trade_executor.get_account_info()
            return jsonify(
                {
                    "balance": info.get("balance"),
                    "buying_power": info.get("buying_power"),
                    "open_positions": info.get("open_positions", 0),
                    "daily_pnl": info.get("daily_pnl", 0.0),
                    "status": "ok",
                }
            )
        except Exception as exc:
            logger.warning("Account balance unavailable: %s", type(exc).__name__)
            return jsonify(
                {
                    "balance": None,
                    "buying_power": None,
                    "open_positions": 0,
                    "daily_pnl": 0.0,
                    "status": "unavailable",
                    "message": "Account data unavailable; trading disabled.",
                }
            ), 503

    @app.route("/bot/start", methods=["POST"])
    def start_bot():
        blocked = _blocked_if_legacy_trading()
        if blocked:
            return blocked
        api_block = require_api_key(_settings())
        if api_block:
            return api_block
        return jsonify({"detail": "Legacy bot start disabled"}), 503

    @app.route("/bot/stop", methods=["POST"])
    def stop_bot():
        blocked = _blocked_if_legacy_trading()
        if blocked:
            return blocked
        api_block = require_api_key(_settings())
        if api_block:
            return api_block
        return jsonify({"detail": "Legacy bot stop disabled"}), 503

    @app.route("/bot/status", methods=["GET"])
    def bot_status():
        bot_manager = app.config["BOT_MANAGER"]
        if not bot_manager:
            return jsonify({"detail": "Bot manager not initialised"}), 503
        return jsonify(
            BotStatus(
                running=bot_manager.running,
                active_trade_id=bot_manager.active_trade_id,
                last_run=bot_manager.last_run,
            ).model_dump()
        )

    return app


def _df_to_records(df: pd.DataFrame) -> List[Dict]:
    records = []
    for idx, row in df.iterrows():
        records.append(
            {
                "timestamp": idx.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return records


def _build_data_map(data_feed, symbol: str, intraday_payload: Optional[Dict]) -> Dict[str, pd.DataFrame]:
    other_symbol = "TZA" if symbol.upper() == "TNA" else "TNA"
    data_map = {symbol.upper(): _resolve_dataframe(data_feed, symbol.upper(), intraday_payload)}
    data_map[other_symbol] = data_feed.fetch_intraday(other_symbol)
    return data_map


def _resolve_dataframe(data_feed, symbol: str, intraday_payload: Optional[Dict]) -> pd.DataFrame:
    if intraday_payload and intraday_payload.get("candles"):
        df = pd.DataFrame(intraday_payload["candles"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        for column in ["open", "high", "low", "close", "volume"]:
            if column in df.columns:
                df[column] = df[column].astype(float)
        return df
    return data_feed.fetch_intraday(symbol)
