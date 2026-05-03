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

    def generate_many(self, symbols: Iterable[str], start: date, days: int = 30) -> dict[str, list[MarketBar]]:
        result: dict[str, list[MarketBar]] = {}
        for index, symbol in enumerate(symbols):
            base = 1000.0 + index * 750.0
            if symbol == "NIFTY":
                base = 22500
            elif symbol == "BANKNIFTY":
                base = 48500
            elif symbol == "FINNIFTY":
                base = 21500
            elif symbol == "MIDCPNIFTY":
                base = 11800
            result[symbol] = self.generate_daily_bars(symbol, start, days, base_price=base)
        return result

