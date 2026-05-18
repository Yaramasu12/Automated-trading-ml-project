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
        drift: float = 0.0,
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

    # Real NSE/BSE/MCX market prices as of May 2026 — used as synthetic bar anchors.
    # IMPORTANT: Keep in sync with DecisionPipeline._base_price() in decision/pipeline.py.
    # Wrong anchors produce synthetic bars at stale price levels, causing instant stop-losses.
    _BASE_PRICES: dict[str, float] = {
        # Indices
        "NIFTY": 23900, "BANKNIFTY": 54400, "FINNIFTY": 25600,
        "MIDCPNIFTY": 13900, "SENSEX": 76700, "BANKEX": 61300,
        # Large-cap equities (real prices as of May 2026)
        "RELIANCE": 1452, "TCS": 2444, "INFY": 1175, "HDFCBANK": 770,
        "ICICIBANK": 1250, "SBIN": 1060, "WIPRO": 201, "KOTAKBANK": 372,
        "AXISBANK": 1263, "MARUTI": 13428, "SUNPHARMA": 1807,
        "TATAMOTORS": 341, "BAJFINANCE": 941, "HINDUNILVR": 2292,
        "LTIM": 4240, "LT": 4036, "HCLTECH": 1196, "ITC": 310,
        "BAJAJFINSV": 1756, "TITAN": 4373, "ONGC": 288, "NTPC": 397,
        "COALINDIA": 474, "POWERGRID": 318, "CIPLA": 1318,
        "DRREDDY": 1279, "DIVISLAB": 6601, "EICHERMOT": 7261,
        "APOLLOHOSP": 7710, "GRASIM": 2854, "HINDALCO": 1042,
        "JSWSTEEL": 1258, "TATACONSUM": 1150, "BHARTIARTL": 1822,
        "ASIANPAINT": 2432, "ULTRACEMCO": 11500, "HEROMOTOCO": 5030,
        "BPCL": 297, "SHRIRAMFIN": 955, "M&M": 3077, "TRENT": 4106,
        "ADANIENT": 2471, "ADANIPORTS": 1729,
        # MCX commodities (per lot unit in INR)
        "GOLD": 73000, "GOLDM": 73000,
        "SILVER": 88000, "SILVERMIC": 88000,
        "CRUDEOIL": 6200, "CRUDEOILM": 6200,
        "NATURALGAS": 210,
        "COPPER": 780, "ZINC": 230, "NICKEL": 1450,
    }

    def generate_many(self, symbols: Iterable[str], start: date, days: int = 30) -> dict[str, list[MarketBar]]:
        result: dict[str, list[MarketBar]] = {}
        for symbol in symbols:
            base = self._BASE_PRICES.get(symbol, 1500.0)
            result[symbol] = self.generate_daily_bars(symbol, start, days, base_price=base)
        return result

