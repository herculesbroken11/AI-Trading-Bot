import httpx
import os
import time
import threading
from collections import deque
from typing import Dict, Optional, Tuple, Any

import pandas as pd

from .config import load_config


class AlphaVantageDataFeed:
    """
    Market data only (Alpha Vantage). Used for signals, indicators, and AI context.

    Execution is always Tastytrade — see ``backend.pipeline.describe_pipeline``.
    Responses are cached and rate-limited to respect paid-tier limits (e.g. 150 req/min).
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._cfg = config or load_config()
        self.api_key = os.getenv("ALPHAVANTAGE_API_KEY")
        self.base_url = "https://www.alphavantage.co/query"

        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._request_times: deque = deque()

        self._ttl_intraday = float(self._cfg.get("alphavantage_cache_ttl_intraday_seconds", 45))
        self._ttl_daily = float(self._cfg.get("alphavantage_cache_ttl_daily_seconds", 300))
        self._ttl_quote = float(self._cfg.get("alphavantage_cache_ttl_quote_seconds", 20))
        self._max_rpm = int(self._cfg.get("alphavantage_max_requests_per_minute", 120))

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise ValueError(
                "ALPHAVANTAGE_API_KEY is not set. Add it to .env for market data (signals / AI)."
            )

    def _cache_key(self, kind: str, *parts: str) -> str:
        return f"{kind}:" + ":".join(str(p) for p in parts)

    def _cache_get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            ts, val = entry
            if time.time() - ts > ttl:
                del self._cache[key]
                return None
            return val

    def _cache_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (time.time(), value)

    def _rate_limit_acquire(self) -> None:
        """Block briefly if we would exceed max requests per rolling minute."""
        window = 60.0
        with self._lock:
            now = time.time()
            while self._request_times and self._request_times[0] < now - window:
                self._request_times.popleft()
            if len(self._request_times) >= self._max_rpm:
                wait = window - (now - self._request_times[0]) + 0.05
            else:
                wait = 0.0
        if wait > 0:
            time.sleep(wait)
            with self._lock:
                now = time.time()
                while self._request_times and self._request_times[0] < now - window:
                    self._request_times.popleft()
                self._request_times.append(time.time())
        else:
            with self._lock:
                self._request_times.append(time.time())

    def fetch_intraday(self, symbol: str, interval: str = "1min", outputsize: str = "compact") -> pd.DataFrame:
        """Fetch intraday data for a symbol and enrich with indicators"""
        self._ensure_api_key()
        key = self._cache_key("intraday", symbol.upper(), interval, outputsize)
        cached = self._cache_get(key, self._ttl_intraday)
        if cached is not None:
            return cached.copy()

        self._rate_limit_acquire()
        with httpx.Client(timeout=30.0) as client:
            params = {
                "function": "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "interval": interval,
                "apikey": self.api_key,
                "outputsize": outputsize,
            }
            response = client.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()

            if "Error Message" in data:
                raise ValueError(f"Alpha Vantage Error: {data['Error Message']}")

            time_series_key = f"Time Series ({interval})"
            if time_series_key not in data:
                note = data.get("Note") or data.get("Information") or ""
                raise ValueError(
                    f"No data found for {symbol}. {note or 'Rate limit or invalid response?'}"
                )

            df = pd.DataFrame.from_dict(
                data[time_series_key],
                orient="index",
                dtype=float,
            )
            df.columns = ["open", "high", "low", "close", "volume"]
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            self._augment_intraday(df)

        self._cache_set(key, df)
        return df.copy()

    def fetch_daily(self, symbol: str, outputsize: str = "compact") -> pd.DataFrame:
        """Fetch daily adjusted data for long-term trend analysis"""
        self._ensure_api_key()
        key = self._cache_key("daily", symbol.upper(), outputsize)
        cached = self._cache_get(key, self._ttl_daily)
        if cached is not None:
            return cached.copy()

        self._rate_limit_acquire()
        with httpx.Client(timeout=30.0) as client:
            params = {
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": symbol,
                "apikey": self.api_key,
                "outputsize": outputsize,
            }
            response = client.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()

            if "Error Message" in data:
                raise ValueError(f"Alpha Vantage Error: {data['Error Message']}")

            time_series_key = "Time Series (Daily)"
            if time_series_key not in data:
                note = data.get("Note") or data.get("Information") or ""
                raise ValueError(
                    f"No daily data found for {symbol}. {note or 'Rate limit or invalid response?'}"
                )

            df = pd.DataFrame.from_dict(
                data[time_series_key],
                orient="index",
                dtype=float,
            )
            df.columns = [
                "open",
                "high",
                "low",
                "close",
                "adjusted_close",
                "volume",
                "dividend_amount",
                "split_coefficient",
            ]
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            df["return"] = df["close"].pct_change()
            df["rolling_volatility"] = df["return"].rolling(20).std() * (252 ** 0.5)
            df["rolling_volume"] = df["volume"].rolling(20).mean()

        self._cache_set(key, df)
        return df.copy()

    def fetch_quote(self, symbol: str) -> Dict:
        """Fetch current quote (Alpha Vantage GLOBAL_QUOTE — signal / monitoring price)."""
        self._ensure_api_key()
        key = self._cache_key("quote", symbol.upper())
        cached = self._cache_get(key, self._ttl_quote)
        if cached is not None:
            return dict(cached)

        self._rate_limit_acquire()
        with httpx.Client(timeout=30.0) as client:
            params = {
                "function": "GLOBAL_QUOTE",
                "symbol": symbol,
                "apikey": self.api_key,
            }
            response = client.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()

            if "Global Quote" in data:
                quote = data["Global Quote"]
                out = {
                    "symbol": quote.get("01. symbol"),
                    "price": float(quote.get("05. price", 0)),
                    "change": float(quote.get("09. change", 0)),
                    "change_percent": quote.get("10. change percent", "0%"),
                    "volume": float(quote.get("06. volume", 0)),
                }
            else:
                out = {}

        self._cache_set(key, out)
        return dict(out)

    def summarize_intraday(self, df: pd.DataFrame, lookback_minutes: int = 60) -> Dict[str, float]:
        """Produce a compact summary for the AI prompt"""
        recent = df.tail(lookback_minutes)
        volume_avg = recent["volume"].mean()
        volume_last = recent["volume"].iloc[-1]
        volume_trend = (
            "strengthening"
            if volume_last > volume_avg * 1.1
            else "weakening"
            if volume_last < volume_avg * 0.9
            else "steady"
        )
        momentum = recent["close"].pct_change().tail(10).mean()
        volatility = recent["close"].pct_change().std() * (len(recent) ** 0.5)
        money_flow = recent["money_flow_volume"].sum()

        return {
            "current_price": float(recent["close"].iloc[-1]),
            "price_change_pct": float((recent["close"].iloc[-1] / recent["close"].iloc[0]) - 1),
            "volume_trend": volume_trend,
            "volume_avg": float(volume_avg),
            "money_flow": float(money_flow),
            "momentum": float(momentum),
            "volatility": float(volatility),
        }

    def _augment_intraday(self, df: pd.DataFrame) -> None:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        spread = (df["high"] - df["low"]).replace(0, 1e-6)
        money_flow_multiplier = (2 * df["close"] - df["high"] - df["low"]) / spread
        money_flow_multiplier = money_flow_multiplier.replace([pd.NA, pd.NaT], 0).fillna(0)
        money_flow_multiplier = money_flow_multiplier.clip(lower=-1, upper=1)
        df["money_flow_volume"] = money_flow_multiplier * df["volume"].fillna(0)
        df["volume_sma_10"] = df["volume"].rolling(10).mean()
        df["price_sma_5"] = df["close"].rolling(5).mean()
        df["price_sma_20"] = df["close"].rolling(20).mean()
        df["volatility_10"] = df["close"].pct_change().rolling(10).std()
        df["momentum_15"] = df["close"].pct_change(15)
        df["typical_price"] = typical_price

        if not df.empty:
            df["hour"] = df.index.hour
            try:
                df["hourly_volume"] = df.groupby("hour")["volume"].transform("sum")
                df["hourly_money_flow"] = df.groupby("hour")["money_flow_volume"].transform("sum")
            except Exception:
                df["hourly_volume"] = df["volume"]
                df["hourly_money_flow"] = df["money_flow_volume"]

            df["money_flow_direction"] = df["money_flow_volume"].apply(
                lambda x: 1 if x > 0 else -1 if x < 0 else 0
            )

        df.bfill(inplace=True)
        df.ffill(inplace=True)

    @staticmethod
    def analyze_hourly_trends(df: pd.DataFrame) -> Dict[str, Any]:
        """Analyze hourly volume and money flow trends for exit decisions (no HTTP)."""
        recent = df.tail(60)

        current_hour = recent.index[-1].hour
        current_hour_data = recent[recent["hour"] == current_hour]

        prev_hour = current_hour - 1 if current_hour > 0 else 23
        prev_hour_data = recent[recent["hour"] == prev_hour]

        current_money_flow = (
            current_hour_data["money_flow_volume"].sum() if not current_hour_data.empty else 0
        )
        prev_money_flow = prev_hour_data["money_flow_volume"].sum() if not prev_hour_data.empty else 0

        current_volume = current_hour_data["volume"].sum() if not current_hour_data.empty else 0
        prev_volume = prev_hour_data["volume"].sum() if not prev_hour_data.empty else 0

        money_flow_trend = (
            "entering" if current_money_flow > 0 else "leaving" if current_money_flow < 0 else "neutral"
        )
        money_flow_changing = (
            "weakening"
            if abs(current_money_flow) < abs(prev_money_flow) * 0.7
            else "strengthening"
            if abs(current_money_flow) > abs(prev_money_flow) * 1.3
            else "stable"
        )

        volume_trend = (
            "strengthening"
            if current_volume > prev_volume * 1.2
            else "weakening"
            if current_volume < prev_volume * 0.8
            else "stable"
        )

        last_15_min = recent.tail(15)
        money_flow_momentum = last_15_min["money_flow_volume"].mean()

        return {
            "current_hour": int(current_hour),
            "money_flow_entering": float(current_money_flow) if current_money_flow > 0 else 0.0,
            "money_flow_leaving": float(abs(current_money_flow)) if current_money_flow < 0 else 0.0,
            "money_flow_trend": money_flow_trend,
            "money_flow_changing": money_flow_changing,
            "volume_trend": volume_trend,
            "volume_change_pct": float((current_volume / prev_volume - 1) * 100) if prev_volume > 0 else 0.0,
            "money_flow_momentum": float(money_flow_momentum),
            "should_exit_on_negative_flow": current_money_flow < 0 and money_flow_changing == "weakening",
        }
