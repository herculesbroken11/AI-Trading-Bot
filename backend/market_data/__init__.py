"""
Read-only market data (Checkpoint 2.9).

Tastytrade production = quotes only. Order execution stays on the sandbox
(backend.execution / backend.adapters.broker) and never imports this package's
credentials.
"""

from backend.market_data.config import (
    MarketDataConfig,
    MarketDataConfigError,
    validate_execution_still_sandbox,
    validate_market_data_settings,
)
from backend.market_data.models import (
    MarketDataResult,
    QuoteFreshness,
    QuoteSnapshot,
    evaluate_quote_freshness,
    is_quote_usable_for_trading,
    normalize_tastytrade_quote,
)

__all__ = [
    "MarketDataConfig",
    "MarketDataConfigError",
    "MarketDataResult",
    "QuoteFreshness",
    "QuoteSnapshot",
    "evaluate_quote_freshness",
    "is_quote_usable_for_trading",
    "normalize_tastytrade_quote",
    "validate_execution_still_sandbox",
    "validate_market_data_settings",
]
