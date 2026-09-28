#!/usr/bin/env python3
"""
Checkpoint 2.9 — Tastytrade PRODUCTION market-data check (read-only quotes).

Production is used ONLY for quote snapshots. This script never places,
cancels or reads orders, and verifies order execution is still sandbox-only.
Secrets are never printed (fingerprints only).

Exit codes: 0 passed, 1 failed, 2 configuration invalid, 3 rate limited.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
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
from backend.market_data.models import (
    MarketDataResult,
    evaluate_quote_freshness,
    is_quote_usable_for_trading,
)
from backend.market_data.tastytrade_market_data import (
    DEFAULT_EQUITY_SYMBOLS,
    DEFAULT_INDEX_SYMBOLS,
    MarketDataError,
    TastytradeMarketDataClient,
)

CHECK_COMMAND = "py -3.11 scripts/check_tastytrade_market_data.py"
CONFIG_EXIT_CODE = 2
PASSED = "market_data_check: passed"
FAILED = "market_data_check: failed"

ClientFactory = Callable[[MarketDataConfig], TastytradeMarketDataClient]


def _fail(step: str, message: str, *, next_step: str = "", exit_code: int = 1) -> int:
    print(f"error_step: {step}")
    print(f"error: {message}")
    if next_step:
        print(f"next_step: {next_step}")
    print(FAILED)
    return exit_code


def _print_execution_env(settings: Settings) -> None:
    print("--- execution environment (must stay sandbox) ---")
    print(f"TRADING_MODE: {settings.trading_mode}")
    print(f"TASTYTRADE_ENV: {settings.tastytrade_env}")
    print(f"LIVE_TRADING_ENABLED: {str(settings.live_trading_enabled).lower()}")
    print(f"order_execution_base_url: {SANDBOX_BASE_URL}")
    print("production_order_execution: blocked")


def _print_config(config: MarketDataConfig) -> None:
    print("--- market data config (production, read-only) ---")
    for key, value in config.safe_summary().items():
        print(f"{key}: {value}")


def _fmt(value: object) -> str:
    return "n/a" if value is None else str(value)


def _print_result(
    result: MarketDataResult,
    *,
    max_age_seconds: float,
    now: datetime,
) -> List[str]:
    """Print normalized quotes; return the list of stale-quote warnings."""
    warnings: List[str] = []
    print("--- quotes ---")
    for symbol in result.requested:
        quote = result.quotes.get(symbol)
        if quote is None:
            continue
        data = quote.to_safe_dict(now)
        freshness = evaluate_quote_freshness(quote, now=now, max_age_seconds=max_age_seconds)
        usable, usable_reason = is_quote_usable_for_trading(quote, freshness)
        print(f"[{symbol}]")
        for field in (
            "instrument_type",
            "bid",
            "ask",
            "mid",
            "mark",
            "last",
            "open",
            "high",
            "low",
            "previous_close",
            "volume",
            "updated_at",
            "quote_age_seconds",
            "is_trading_halted",
            "source",
            "is_realtime",
        ):
            print(f"  {field}: {_fmt(data[field])}")
        print(f"  stale: {str(freshness.is_stale).lower()}")
        print(f"  usable_for_trading (diagnostic only): {str(usable).lower()} ({usable_reason})")
        if freshness.warning:
            warnings.append(freshness.warning)
    for symbol, message in result.unsupported.items():
        print(f"[{symbol}] unsupported: {message}")
    for symbol in result.missing:
        print(f"[{symbol}] missing: not returned by Tastytrade production REST")
    return warnings


def run_check(
    settings: Settings,
    *,
    equities: Sequence[str] = DEFAULT_EQUITY_SYMBOLS,
    indices: Sequence[str] = DEFAULT_INDEX_SYMBOLS,
    require_fresh: bool = False,
    max_age_seconds: Optional[float] = None,
    client_factory: Optional[ClientFactory] = None,
    now: Optional[datetime] = None,
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
    _print_config(config)

    max_age = float(max_age_seconds) if max_age_seconds else config.max_quote_age_seconds
    factory = client_factory or (lambda cfg: TastytradeMarketDataClient(cfg))
    client = factory(config)

    try:
        result = client.get_quotes(equities, indices=indices)
    except MarketDataError as exc:
        print("--- market data request failed ---")
        print(exc.format_safe())
        if exc.reason == "rate_limited" and exc.rate_limit is not None:
            print_cooldown_advice(
                exc.rate_limit,
                next_command=CHECK_COMMAND,
                stream=sys.stdout,
                label="market-data",
                status_command=None,
            )
            return _fail(exc.step, "rate limited", exit_code=RATE_LIMITED_EXIT_CODE)
        if exc.reason == "rate_limited":
            return _fail(exc.step, "rate limited", exit_code=RATE_LIMITED_EXIT_CODE)
        return _fail(exc.step, exc.reason, next_step=exc.next_step)

    granted = getattr(client.auth, "granted_scope", None)
    if granted:
        print(f"granted_scope: {granted}")

    current = now or datetime.now(timezone.utc)
    warnings = _print_result(result, max_age_seconds=max_age, now=current)

    print("--- summary ---")
    print(f"requested: {','.join(result.requested)}")
    print(f"received: {','.join(sorted(result.quotes)) or 'none'}")
    print(f"missing: {','.join(result.missing) or 'none'}")
    print(f"unsupported: {','.join(result.unsupported) or 'none'}")
    print(f"max_quote_age_seconds: {max_age:g}")
    for warning in warnings:
        print(f"warning: {warning}")
    if warnings:
        print("note: stale quotes are diagnostic only; nothing trades on them in Phase 2.")

    if result.missing:
        return _fail(
            "quotes",
            f"required symbols missing: {','.join(result.missing)}",
            next_step="confirm the production account is funded and has market-data access",
        )
    if require_fresh and warnings:
        return _fail("freshness", "stale quotes with --require-fresh")

    print(PASSED)
    return 0


def _parse_symbols(raw: Optional[str], default: Sequence[str]) -> List[str]:
    if raw is None:
        return list(default)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--symbols",
        help=f"comma-separated equity symbols (default: {','.join(DEFAULT_EQUITY_SYMBOLS)})",
    )
    parser.add_argument("--no-vix", action="store_true", help="skip the optional VIX index quote")
    parser.add_argument(
        "--require-fresh",
        action="store_true",
        help="fail if any quote is stale (default: warn only)",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=None,
        help="stale threshold override (default: MARKET_DATA_MAX_QUOTE_AGE_SECONDS or 60)",
    )
    args = parser.parse_args(argv)

    env_path = _REPO_ROOT / ".env"
    print("--- tastytrade production market-data check (read-only) ---")
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

    equities = _parse_symbols(args.symbols, DEFAULT_EQUITY_SYMBOLS)
    if not equities:
        return _fail("args", "no symbols given", exit_code=CONFIG_EXIT_CODE)
    indices: Sequence[str] = () if args.no_vix else DEFAULT_INDEX_SYMBOLS
    return run_check(
        settings,
        equities=equities,
        indices=indices,
        require_fresh=args.require_fresh,
        max_age_seconds=args.max_age_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
