#!/usr/bin/env python3
"""
Checkpoint 2.10 — Tastytrade DXLink streaming market-data check (read-only).

Production is used ONLY for the quote token (REST) and the DXLink quote
stream. Nothing here places, cancels or reads orders; execution stays sandbox.
Secrets and the quote token are never printed (fingerprints only).

Exit codes:
  0 stream connected and core symbols fresh
  1 auth / connection / subscription failure
  2 configuration invalid
  3 rate limited
  4 stream connected but core symbols missing or stale (> max age)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.adapters.broker.env_file_diagnostics import find_duplicate_tastytrade_env_keys
from backend.adapters.broker.sandbox_cooldown import RATE_LIMITED_EXIT_CODE, print_cooldown_advice
from backend.config.settings import ConfigurationError, Settings, load_settings, reset_settings_cache
from backend.config.tastytrade_urls import SANDBOX_BASE_URL
from backend.market_data.config import (
    MarketDataConfig,
    MarketDataConfigError,
    validate_market_data_settings,
)
from backend.market_data.dxlink_stream import (
    DXLinkQuoteTokenProvider,
    DXLinkStreamClient,
    StreamRunSummary,
)
from backend.market_data.stream_models import (
    DEFAULT_STREAM_SYMBOLS,
    DIAGNOSTIC_ONLY_SYMBOLS,
    StreamState,
    evaluate_stream_result,
)
from backend.market_data.tastytrade_market_data import MarketDataError

CHECK_COMMAND = "py -3.11 scripts/check_tastytrade_dxlink_stream.py"
CONFIG_EXIT_CODE = 2
STALE_EXIT_CODE = 4
MAX_DURATION_SECONDS = 300.0
PASSED = "dxlink_stream_check: passed"
FAILED = "dxlink_stream_check: failed"

TokenProviderFactory = Callable[[MarketDataConfig], DXLinkQuoteTokenProvider]
StreamFactory = Callable[[DXLinkQuoteTokenProvider, List[str]], DXLinkStreamClient]


def _fail(step: str, message: str, *, next_step: str = "", exit_code: int = 1) -> int:
    print(f"error_step: {step}")
    print(f"error: {message}")
    if next_step:
        print(f"next_step: {next_step}")
    print(FAILED)
    return exit_code


def _market_data_failure(exc: MarketDataError) -> int:
    print("--- dxlink failure ---")
    print(exc.format_safe())
    if exc.reason == "rate_limited":
        if exc.rate_limit is not None:
            print_cooldown_advice(
                exc.rate_limit,
                next_command=CHECK_COMMAND,
                stream=sys.stdout,
                label="market-data",
                status_command=None,
            )
        return _fail(exc.step, "rate limited", exit_code=RATE_LIMITED_EXIT_CODE)
    return _fail(exc.step, exc.reason, next_step=exc.next_step)


def _print_execution_env(settings: Settings) -> None:
    print("--- execution environment (must stay sandbox) ---")
    print(f"TRADING_MODE: {settings.trading_mode}")
    print(f"TASTYTRADE_ENV: {settings.tastytrade_env}")
    print(f"LIVE_TRADING_ENABLED: {str(settings.live_trading_enabled).lower()}")
    print(f"order_execution_base_url: {SANDBOX_BASE_URL}")
    print("production_order_execution: blocked")


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


_STATE_FIELDS = (
    "bid",
    "ask",
    "mid",
    "last_trade_price",
    "day_open",
    "day_high",
    "day_low",
    "previous_close",
    "volume",
    "event_types_seen",
    "last_event_type",
    "last_event_received_at",
    "quote_age_seconds",
    "max_quote_age_seconds",
    "avg_quote_age_seconds",
    "fresh_sample_pct",
    "is_realtime",
    "stale",
    "stayed_fresh",
)


def _print_state(state: StreamState, *, now: float, max_age: float) -> None:
    print("--- update counts ---")
    for symbol, sym in state.symbols.items():
        print(
            f"{symbol}: quote={sym.quote_updates} trade={sym.trade_updates} "
            f"summary={sym.summary_updates}"
        )
    print("--- latest stream state ---")
    for symbol, sym in state.symbols.items():
        data = sym.to_safe_dict(now, max_age)
        label = " (volatility diagnostic only)" if sym.diagnostic_only else ""
        print(f"[{symbol}]{label}")
        for key in _STATE_FIELDS:
            value = data[key]
            if isinstance(value, list):
                value = ",".join(value) or "none"
            print(f"  {key}: {_fmt(value)}")
        print(
            f"  usable_for_trading (diagnostic only): {_fmt(data['usable_for_trading'])} "
            f"({data['usable_reason']})"
        )


def _print_summary(summary: StreamRunSummary) -> None:
    print("--- stream summary ---")
    print(f"connected: {_fmt(summary.connected)}")
    print(f"authorized: {_fmt(summary.authorized)}")
    print(f"channel_opened: {_fmt(summary.channel_opened)}")
    print(f"subscribed: {_fmt(summary.subscribed)}")
    print(f"data_format: {summary.data_format or 'n/a'}")
    print(f"duration_seconds: {summary.duration_seconds:.2f}")
    print(f"messages_received: {summary.messages_received}")
    print(f"feed_data_messages: {summary.feed_data_messages}")
    print(f"keepalives_sent: {summary.keepalives_sent}")
    print(f"reconnects: {summary.reconnects}")
    for warning in summary.warnings:
        print(f"warning: {warning}")


def _default_stream_factory(provider: DXLinkQuoteTokenProvider, symbols: List[str]) -> DXLinkStreamClient:
    return DXLinkStreamClient(provider, symbols=symbols)


def run_stream_check(
    settings: Settings,
    *,
    symbols: Sequence[str] = DEFAULT_STREAM_SYMBOLS,
    duration_seconds: float = 15.0,
    max_age_seconds: Optional[float] = None,
    require_all_fresh: bool = False,
    token_provider_factory: Optional[TokenProviderFactory] = None,
    stream_factory: Optional[StreamFactory] = None,
) -> int:
    _print_execution_env(settings)
    try:
        config = validate_market_data_settings(settings)
    except MarketDataConfigError as exc:
        print("--- market data config invalid ---")
        for problem in exc.problems:
            print(f"problem: {problem}")
        return _fail(
            "config",
            "market-data configuration is unsafe or incomplete (see problems above)",
            next_step="fix .env market-data settings; see .env.example (MARKET DATA section)",
            exit_code=CONFIG_EXIT_CODE,
        )

    max_age = float(max_age_seconds if max_age_seconds is not None else settings.stream_max_quote_age_seconds)
    if max_age <= 0:
        return _fail("config", "max quote age must be > 0", exit_code=CONFIG_EXIT_CODE)
    if not 0 < duration_seconds <= MAX_DURATION_SECONDS:
        return _fail(
            "config",
            f"duration must be between 0 and {MAX_DURATION_SECONDS:g} seconds",
            exit_code=CONFIG_EXIT_CODE,
        )
    symbol_list = [s.strip().upper() for s in symbols if s and s.strip()]
    if not symbol_list:
        return _fail("config", "no symbols given", exit_code=CONFIG_EXIT_CODE)
    core = [s for s in symbol_list if s not in DIAGNOSTIC_ONLY_SYMBOLS]

    print("--- market data config (production, read-only) ---")
    for key, value in config.safe_summary().items():
        print(f"{key}: {value}")
    print("--- stream settings ---")
    print(f"symbols: {','.join(symbol_list)}")
    print(f"core_symbols: {','.join(core) or 'none'}")
    print(f"duration_seconds: {duration_seconds:g}")
    print(f"max_quote_age_seconds: {max_age:g}")
    print(f"require_all_fresh: {_fmt(require_all_fresh)}")

    provider = (token_provider_factory or (lambda cfg: DXLinkQuoteTokenProvider(cfg)))(config)
    try:
        token = provider.get_token()
    except MarketDataError as exc:
        return _market_data_failure(exc)
    print("--- quote token ---")
    for key, value in token.safe_summary().items():
        print(f"{key}: {_fmt(value)}")

    client = (stream_factory or _default_stream_factory)(provider, symbol_list)
    try:
        client.connect()
        print("dxlink_handshake: ok (SETUP -> AUTH -> CHANNEL -> FEED_SETUP -> FEED_SUBSCRIPTION)")
        summary = client.stream(duration_seconds, max_age_seconds=max_age)
    except MarketDataError as exc:
        _print_summary(client.summary)
        return _market_data_failure(exc)
    finally:
        client.disconnect()

    now = client.now()
    _print_summary(summary)
    _print_state(client.state, now=now, max_age=max_age)

    verdict = evaluate_stream_result(
        client.state,
        now=now,
        max_age_seconds=max_age,
        require_all_fresh=require_all_fresh,
    )
    print("--- verdict ---")
    for warning in verdict.warnings:
        print(f"warning: {warning}")
    for failure in verdict.failures:
        print(f"failure: {failure}")
    print("note: diagnostics only; nothing trades on streamed quotes in this checkpoint.")
    if verdict.no_data:
        return _fail(
            "subscription",
            "no streaming data received",
            next_step="verify streaming entitlement and symbols; retry during market hours",
        )
    if not verdict.passed:
        return _fail(
            "freshness",
            "core symbols missing or stale",
            next_step="stale data must not be traded; outside market hours this is expected",
            exit_code=STALE_EXIT_CODE,
        )
    print(PASSED)
    return 0


def _parse_symbols(raw: Optional[str]) -> List[str]:
    if raw is None:
        return list(DEFAULT_STREAM_SYMBOLS)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--symbols",
        help=f"comma-separated symbols (default: {','.join(DEFAULT_STREAM_SYMBOLS)})",
    )
    parser.add_argument("--duration-seconds", type=float, default=15.0)
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=None,
        help="stale threshold (default: STREAM_MAX_QUOTE_AGE_SECONDS or 1.0)",
    )
    parser.add_argument("--no-vix", action="store_true", help="skip the VIX diagnostic symbol")
    parser.add_argument(
        "--require-all-fresh",
        action="store_true",
        help="fail if any core symbol exceeds max age at any point during the window",
    )
    args = parser.parse_args(argv)

    env_path = _REPO_ROOT / ".env"
    print("--- tastytrade DXLink streaming check (read-only) ---")
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
            "load_settings",
            str(exc),
            next_step="fix .env (MARKET_DATA_READ_ONLY must be true; live trading stays off)",
            exit_code=CONFIG_EXIT_CODE,
        )

    symbols = _parse_symbols(args.symbols)
    if args.no_vix:
        symbols = [s for s in symbols if s != "VIX"]
    return run_stream_check(
        settings,
        symbols=symbols,
        duration_seconds=args.duration_seconds,
        max_age_seconds=args.max_age_seconds,
        require_all_fresh=args.require_all_fresh,
    )


if __name__ == "__main__":
    sys.exit(main())
