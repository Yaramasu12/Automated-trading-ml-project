from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

from trading_platform.domain.models import MarketBar


class HistoricalDataProvider:
    def load_csv(self, path: str | Path, symbol: str) -> list[MarketBar]:
        bars: list[MarketBar] = []
        with Path(path).open(newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                raw_timestamp = row.get("timestamp") or row.get("date") or row.get("Date")
                if not raw_timestamp:
                    continue
                timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                bars.append(
                    MarketBar(
                        timestamp=timestamp,
                        symbol=symbol,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume", 0))),
                    )
                )
        return bars


class SyntheticDataProvider:
    """Deterministic one-month data for local testing and strategy validation."""

    def __init__(self, seed: int = 7):
        self.seed = seed

    def generate_daily_bars(
        self,
        symbol: str,
        start: date,
        days: int = 30,
        base_price: float = 1000.0,
        drift: float = 0.0015,
        volatility: float = 0.012,
    ) -> list[MarketBar]:
        rng = random.Random(f"{self.seed}:{symbol}:{start.isoformat()}")
        bars: list[MarketBar] = []
        price = base_price
        current = start
        generated = 0
        while generated < days:
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue
            seasonal = math.sin(generated / 3.0) * volatility
            shock = rng.gauss(drift + seasonal, volatility)
            open_price = price
            close = max(1.0, price * (1 + shock))
            high = max(open_price, close) * (1 + abs(rng.gauss(0, volatility / 2)))
            low = min(open_price, close) * (1 - abs(rng.gauss(0, volatility / 2)))
            volume = int(500_000 + abs(rng.gauss(0, 250_000)))
            bars.append(
                MarketBar(
                    timestamp=datetime.combine(current, time(15, 30)),
                    symbol=symbol,
                    open=round(open_price, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(close, 2),
                    volume=volume,
                )
            )
            price = close
            current += timedelta(days=1)
            generated += 1
        return bars

    # Real NSE/BSE market prices as of May 2026 — used as synthetic bar anchors.
    # Update periodically; wrong anchors produce losing backtests.
    _BASE_PRICES: dict[str, float] = {
        "NIFTY": 23900, "BANKNIFTY": 54400, "FINNIFTY": 25600,
        "MIDCPNIFTY": 13900, "SENSEX": 76700, "BANKEX": 61300,
        "RELIANCE": 1452, "TCS": 2444, "INFY": 1175, "HDFCBANK": 770,
        "ICICIBANK": 1250, "SBIN": 1060, "WIPRO": 201, "KOTAKBANK": 372,
        "AXISBANK": 1263, "MARUTI": 13428, "SUNPHARMA": 1807,
        "TATAMOTORS": 341, "BAJFINANCE": 941, "HINDUNILVR": 2292,
        "LTIM": 5100, "LT": 3880, "HCLTECH": 1654, "ITC": 413,
        "BAJAJFINSV": 2105, "TITAN": 3501, "ONGC": 257, "NTPC": 337,
        "COALINDIA": 383, "POWERGRID": 296, "CIPLA": 1454,
        "DRREDDY": 1166, "DIVISLAB": 5456, "EICHERMOT": 5390,
        "APOLLOHOSP": 6801, "GRASIM": 2793, "HINDALCO": 649,
        "JSWSTEEL": 987, "TATACONSUM": 955, "BHARTIARTL": 1897,
        "ASIANPAINT": 2214, "ULTRACEMCO": 11680, "HEROMOTOCO": 4097,
        "BPCL": 289, "SHRIRAMFIN": 650, "M&M": 3069, "TRENT": 5545,
        "ADANIENT": 2212, "ADANIPORTS": 1343,
    }

    def generate_many(self, symbols: Iterable[str], start: date, days: int = 30) -> dict[str, list[MarketBar]]:
        result: dict[str, list[MarketBar]] = {}
        for symbol in symbols:
            base = self._BASE_PRICES.get(symbol, 1500.0)
            result[symbol] = self.generate_daily_bars(symbol, start, days, base_price=base)
        return result

