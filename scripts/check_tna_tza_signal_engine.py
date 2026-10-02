#!/usr/bin/env python3
"""
Checkpoint 2.11 — TNA / TZA Signal Engine v1 diagnostic (read-only, no orders).

Streams read-only DXLink quotes for a window, then builds exactly one decision
snapshot at the end: TNA (bullish), TZA (bearish) or SKIP. Required core symbols
(TNA, TZA, IWM, SPY, QQQ) must be fresh (<= max age) at the decision moment;
otherwise the decision is SKIP. VIX is a volatility diagnostic only.

This script never imports the order executor, never submits orders and never
touches account endpoints. Secrets and the quote token are never printed.

Exit codes:
  0 signal engine ran (any decision, including SKIP)
  1 auth / connection / stream failure
  2 configuration invalid
  3 rate limited
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.env_file_diagnostics import find_duplicate_tastytrade_env_keys
from backend.adapters.broker.sandbox_cooldown import RATE_LIMITED_EXIT_CODE, print_cooldown_advice
from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import (
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    BrokerUrlBlockedError,
    assert_market_data_request,
    resolve_broker_base_url,
)
from backend.market_data.config import MarketDataConfig, MarketDataConfigError, validate_market_data_settings
from backend.market_data.dxlink_stream import DXLinkQuoteTokenProvider, DXLinkStreamClient
from backend.market_data.tastytrade_market_data import MarketDataError
from backend.signals.dxlink_signal_source import DEFAULT_SIGNAL_SYMBOLS, collect_signal_from_dxlink
from backend.signals.models import REQUIRED_SIGNAL_SYMBOLS, VOLATILITY_SYMBOL, SignalDecision
from backend.signals.tna_tza_signal_engine import SignalEngineConfig, TnaTzaSignalEngine

CHECK_COMMAND = "py -3.11 scripts/check_tna_tza_signal_engine.py"
CONFIG_EXIT_CODE = 2
MAX_DURATION_SECONDS = 300.0
COMPLETED = "signal_engine_check: completed"
FAILED = "signal_engine_check: failed"

TokenProviderFactory = Callable[[MarketDataConfig], DXLinkQuoteTokenProvider]
StreamFactory = Callable[[DXLinkQuoteTokenProvider, List[str]], DXLinkStreamClient]


class _Output:
    """Human lines go to stdout unless --json, in which case only one JSON document is printed."""

    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode
        self.document: Dict[str, Any] = {"signal_engine_check": "running"}

    def line(self, text: str = "") -> None:
        if not self.json_mode:
            print(text)

    def finish(self, status: str, exit_code: int) -> int:
        if self.json_mode:
            self.document["signal_engine_check"] = status
            self.document["exit_code"] = exit_code
            print(json.dumps(self.document, indent=2, sort_keys=True, default=str))
        else:
            print(COMPLETED if status == "completed" else FAILED)
        return exit_code


def verify_production_execution_blocked() -> Dict[str, bool]:
    """Prove (without any network call) that production order routes fail closed."""
    checks: Dict[str, bool] = {}
    try:
        resolve_broker_base_url("production")
        checks["production_broker_url_blocked"] = False
    except BrokerUrlBlockedError:
        checks["production_broker_url_blocked"] = True
    try:
        assert_market_data_request("POST", f"{PRODUCTION_BASE_URL}/accounts/BLOCKED/orders")
        checks["production_order_request_blocked"] = False
    except BrokerUrlBlockedError:
        checks["production_order_request_blocked"] = True
    return checks


def _fail(out: _Output, step: str, message: str, *, next_step: str = "", exit_code: int = 1) -> int:
    out.line(f"error_step: {step}")
    out.line(f"error: {message}")
    if next_step:
        out.line(f"next_step: {next_step}")
    out.document["error"] = {"step": step, "message": message, "next_step": next_step}
    return out.finish("failed", exit_code)


def _market_data_failure(out: _Output, exc: MarketDataError) -> int:
    out.line("--- market data failure ---")
    out.line(exc.format_safe())
    if exc.reason == "rate_limited":
        if exc.rate_limit is not None and not out.json_mode:
            print_cooldown_advice(
                exc.rate_limit,
                next_command=CHECK_COMMAND,
                stream=sys.stdout,
                label="market-data",
                status_command=None,
            )
        return _fail(out, exc.step, "rate limited", exit_code=RATE_LIMITED_EXIT_CODE)
    return _fail(out, exc.step, exc.reason, next_step=exc.next_step)


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _print_decision(out: _Output, decision: SignalDecision) -> None:
    out.line("--- quote freshness (at decision moment) ---")
    for symbol, status in decision.quote_freshness_by_symbol.items():
        age = "n/a" if status.age_seconds is None else f"{status.age_seconds:.3f}s"
        role = "required" if status.required else "diagnostic"
        verdict = "fresh" if status.fresh else (status.note or "not fresh")
        if not status.required:
            verdict = status.note
        out.line(f"{symbol}: age={age} max={status.max_age_seconds:g}s [{role}] {verdict}")
    out.line(f"freshness_gate_passed: {_fmt(decision.freshness_gate_passed)}")

    out.line("--- score breakdown ---")
    breakdown = decision.score_breakdown
    if breakdown is None:
        out.line("not computed (decision skipped before scoring)")
    else:
        for comp in breakdown.components:
            out.line(f"{comp.name}: bullish +{comp.bullish:g} bearish +{comp.bearish:g} ({comp.detail})")
        for pen in breakdown.penalties:
            out.line(f"penalty {pen.name}: {pen.bullish:g} ({pen.detail})")
        out.line(f"raw_bullish: {breakdown.raw_bullish:g}  raw_bearish: {breakdown.raw_bearish:g}")
    out.line(f"bullish_score: {decision.bullish_score:g}")
    out.line(f"bearish_score: {decision.bearish_score:g}")

    out.line("--- signal decision ---")
    label = decision.selected_symbol or "SKIP"
    out.line(f"decision: {label} ({decision.decision.value})")
    out.line(f"selected_symbol: {_fmt(decision.selected_symbol)}")
    out.line(f"confidence_score: {decision.confidence_score:g}")
    out.line(f"skip_reason: {decision.skip_reason.value if decision.skip_reason else 'n/a'}")
    out.line(f"market_regime: {decision.market_regime}")
    out.line(f"explanation: {decision.explanation}")
    for warning in decision.warnings:
        out.line(f"warning: {warning}")
    out.line(f"created_at: {decision.created_at.isoformat()}")
    out.line("note: signal only; no order was placed or routed.")


def run_signal_check(
    settings: Settings,
    *,
    symbols: Sequence[str] = DEFAULT_SIGNAL_SYMBOLS,
    duration_seconds: float = 30.0,
    max_age_seconds: Optional[float] = None,
    json_output: bool = False,
    token_provider_factory: Optional[TokenProviderFactory] = None,
    stream_factory: Optional[StreamFactory] = None,
) -> int:
    out = _Output(json_output)

    blocked = verify_production_execution_blocked()
    execution = {
        "trading_mode": settings.trading_mode,
        "tastytrade_env": settings.tastytrade_env,
        "live_trading_enabled": bool(settings.live_trading_enabled),
        "order_execution_base_url": SANDBOX_BASE_URL,
        "production_order_execution": "blocked" if all(blocked.values()) else "NOT_BLOCKED",
        "checks": blocked,
        "orders_submitted": 0,
    }
    out.document["execution"] = execution
    out.line("--- execution environment (must stay sandbox) ---")
    out.line(f"TRADING_MODE: {settings.trading_mode}")
    out.line(f"TASTYTRADE_ENV: {settings.tastytrade_env}")
    out.line(f"LIVE_TRADING_ENABLED: {_fmt(bool(settings.live_trading_enabled))}")
    out.line(f"order_execution_base_url: {SANDBOX_BASE_URL}")
    out.line(f"production_order_execution: {execution['production_order_execution']}")
    if not all(blocked.values()):
        return _fail(out, "safety", "production order execution is not blocked", exit_code=CONFIG_EXIT_CODE)

    try:
        config = validate_market_data_settings(settings)
    except MarketDataConfigError as exc:
        out.line("--- market data config invalid ---")
        for problem in exc.problems:
            out.line(f"problem: {problem}")
        out.document["config_problems"] = list(exc.problems)
        return _fail(
            out,
            "config",
            "market-data configuration is unsafe or incomplete (see problems)",
            next_step="fix .env market-data settings; see .env.example (MARKET DATA section)",
            exit_code=CONFIG_EXIT_CODE,
        )

    symbol_list = [s.strip().upper() for s in symbols if s and s.strip()]
    missing_core = [s for s in REQUIRED_SIGNAL_SYMBOLS if s not in symbol_list]
    if missing_core:
        return _fail(
            out,
            "config",
            f"required signal symbols missing from --symbols: {','.join(missing_core)}",
            exit_code=CONFIG_EXIT_CODE,
        )
    if not 0 < duration_seconds <= MAX_DURATION_SECONDS:
        return _fail(
            out,
            "config",
            f"duration must be between 0 and {MAX_DURATION_SECONDS:g} seconds",
            exit_code=CONFIG_EXIT_CODE,
        )
    try:
        max_age = max_age_seconds if max_age_seconds is not None else settings.stream_max_quote_age_seconds
        engine = TnaTzaSignalEngine(SignalEngineConfig.from_settings(settings, max_quote_age_seconds=max_age))
    except ConfigurationError as exc:
        return _fail(out, "config", str(exc), exit_code=CONFIG_EXIT_CODE)

    out.document["market_data_config"] = config.safe_summary()
    out.document["engine_config"] = engine.config.to_dict()
    out.document["stream_settings"] = {
        "symbols": symbol_list,
        "duration_seconds": duration_seconds,
        "max_quote_age_seconds": engine.config.max_quote_age_seconds,
        "vix_included": VOLATILITY_SYMBOL in symbol_list,
    }
    out.line("--- market data config (production, read-only) ---")
    for key, value in config.safe_summary().items():
        out.line(f"{key}: {value}")
    out.line("--- signal settings ---")
    out.line(f"symbols: {','.join(symbol_list)}")
    out.line(f"required_symbols: {','.join(REQUIRED_SIGNAL_SYMBOLS)}")
    out.line(f"duration_seconds: {duration_seconds:g}")
    out.line(f"max_quote_age_seconds: {engine.config.max_quote_age_seconds:g}")
    out.line(f"entry_score_threshold: {engine.config.entry_score_threshold:g}")
    out.line(f"opposing_score_max: {engine.config.opposing_score_max:g}")

    provider = (token_provider_factory or (lambda cfg: DXLinkQuoteTokenProvider(cfg)))(config)
    try:
        result = collect_signal_from_dxlink(
            config,
            engine=engine,
            symbols=symbol_list,
            duration_seconds=duration_seconds,
            provider=provider,
            stream_factory=stream_factory,
        )
    except MarketDataError as exc:
        return _market_data_failure(out, exc)

    summary = result.stream_summary
    status = {
        "connected": summary.connected,
        "authorized": summary.authorized,
        "subscribed": summary.subscribed,
        "data_format": summary.data_format,
        "duration_seconds": round(summary.duration_seconds, 2),
        "messages_received": summary.messages_received,
        "feed_data_messages": summary.feed_data_messages,
        "reconnects": summary.reconnects,
        "quote_updates": {s: q.quote_updates for s, q in result.snapshot.quotes.items()},
        "warnings": list(summary.warnings),
    }
    out.document["quote_token"] = result.token_summary
    out.document["market_data_status"] = status
    out.document["snapshot"] = result.snapshot.to_dict()
    out.document["decision"] = result.decision.to_dict()

    out.line("--- quote token ---")
    for key, value in result.token_summary.items():
        out.line(f"{key}: {_fmt(value)}")
    out.line("--- market data status ---")
    for key in ("connected", "authorized", "subscribed", "data_format", "duration_seconds",
                "messages_received", "feed_data_messages", "reconnects"):
        out.line(f"{key}: {_fmt(status[key])}")
    out.line("quote_updates: " + ", ".join(f"{s}={n}" for s, n in status["quote_updates"].items()))
    for warning in summary.warnings:
        out.line(f"warning: {warning}")
    if summary.feed_data_messages == 0:
        out.line("warning: no streaming data received (decision will be SKIP)")

    _print_decision(out, result.decision)
    return out.finish("completed", 0)


def _parse_symbols(raw: Optional[str]) -> List[str]:
    if raw is None:
        return list(DEFAULT_SIGNAL_SYMBOLS)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=None,
        help="freshness gate at decision time (default: STREAM_MAX_QUOTE_AGE_SECONDS or 1.0)",
    )
    parser.add_argument(
        "--symbols",
        help=f"comma-separated symbols (default: {','.join(DEFAULT_SIGNAL_SYMBOLS)})",
    )
    parser.add_argument("--no-vix", action="store_true", help="skip the VIX volatility diagnostic")
    parser.add_argument("--json", action="store_true", help="print one redacted JSON document only")
    args = parser.parse_args(argv)

    env_path = _REPO_ROOT / ".env"
    if not args.json:
        print("--- TNA/TZA signal engine check (read-only, no orders) ---")
        print(f"env_path: {env_path}")
        if env_path.is_file():
            duplicates = find_duplicate_tastytrade_env_keys(env_path)
            if duplicates.has_duplicates:
                print(duplicates.format_safe())
                print("warning: duplicate TASTYTRADE_ keys detected; last definition wins.")

    reset_settings_cache()
    try:
        settings = load_settings(env_path=env_path, override=True)
    except ConfigurationError as exc:
        return _fail(
            _Output(args.json),
            "load_settings",
            str(exc),
            next_step="fix .env (MARKET_DATA_READ_ONLY must be true; live trading stays off)",
            exit_code=CONFIG_EXIT_CODE,
        )

    symbols = _parse_symbols(args.symbols)
    if args.no_vix:
        symbols = [s for s in symbols if s != VOLATILITY_SYMBOL]
    return run_signal_check(
        settings,
        symbols=symbols,
        duration_seconds=args.duration_seconds,
        max_age_seconds=args.max_age_seconds,
        json_output=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())
