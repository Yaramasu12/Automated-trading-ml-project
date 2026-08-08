"""
Mock/Replay market-data adapter.

Generates deterministic synthetic ticks for development and CI.
Can replay historical synthetic data from a seed for reproducibility.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Protocol

import numpy as np

from ingestion.adapters.base import MarketDataAdapter, Tick, TradeTick, Bar, OrderBookL2


@dataclass
class MockConfig:
    """Configuration for the mock replay adapter."""
    seed: int = 42
    start_price: float = 100.0
    volatility: float = 0.02  # daily volatility
    drift: float = 0.0001  # daily drift
    tick_interval_ms: int = 100  # time between ticks in milliseconds
    mean_reversion: float = 0.0  # 0 = random walk, >0 = mean-reverting
    regime_switch_prob: float = 0.0  # probability of regime change
    regimes: Dict[str, Any] = field(default_factory=lambda: {
        "trending_up": {"drift": 0.001, "vol": 0.015},
        "trending_down": {"drift": -0.001, "vol": 0.015},
        "range_bound": {"drift": 0.0, "vol": 0.008},
        "high_vol": {"drift": 0.0, "vol": 0.03},
    })


class TickSource(Protocol):
    """Protocol for tick generators."""
    
    def next_tick(self) -> Tick: ...
    
    def next_bar(self) -> Bar: ...
    
    def has_more(self) -> bool: ...


class DeterministicMockSource:
    """
    Deterministic tick generator from a seed.
    
    Uses a seeded PRNG to generate realistic price movements with
    configurable regimes. Same seed always produces same output.
    """
    
    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.rng = np.random.default_rng(config.seed)
        self.price = config.start_price
        self.regime = "range_bound"
        self.regime_timer = 0
        self.tick_count = 0
        self._base_time = datetime(2024, 1, 15, 9, 30, 0, tzinfo=timezone.utc)
        
    def next_tick(self) -> Tick:
        """Generate the next tick deterministically."""
        self.tick_count += 1
        
        # Regime switching
        if self.regime_timer <= 0:
            if self.rng.random() < self.config.regime_switch_prob:
                regimes = list(self.config.regimes.keys())
                self.regime = self.rng.choice(regimes)
                self.regime_timer = int(self.rng.integers(50, 200))
            else:
                self.regime_timer = int(self.rng.integers(100, 500))
        
        regime_params = self.config.regimes[self.regime]
        
        # Price movement
        dt = self.config.tick_interval_ms / 1000.0 / 390.0  # fraction of trading day
        mu = regime_params["drift"] * dt
        sigma = regime_params["vol"] * np.sqrt(dt)
        
        # Mean reversion if configured
        if self.config.mean_reversion > 0:
            mr = self.config.mean_reversion * (self.config.start_price - self.price) * dt
        
        shock = self.rng.normal(0, 1)
        self.price *= np.exp(mu + sigma * shock + mr)
        
        # Bid-ask spread (random between 1-5 ticks)
        tick_size = 0.01
        spread_ticks = self.rng.integers(1, 6)
        spread = spread_ticks * tick_size
        
        timestamp = self._base_time + __import__("timedelta").timedelta(
            milliseconds=self.tick_count * self.config.tick_interval_ms
        )
        
        return Tick(
            timestamp=timestamp,
            symbol="MOCK",
            exchange="MOCK",
            bid_price=max(0.01, self.price - spread / 2),
            ask_price=self.price + spread / 2,
            bid_size=int(self.rng.integers(100, 1000)),
            ask_size=int(self.rng.integers(100, 1000)),
            trade_price=self.price,
            trade_size=int(self.rng.integers(10, 500)),
        )
    
    def next_bar(self) -> Bar:
        """Generate a OHLCV bar (aggregated from ticks)."""
        # Simplified: just return a bar from current state
        return Bar(
            timestamp=self._base_time,
            symbol="MOCK",
            exchange="MOCK",
            open=self.price,
            high=self.price * 1.001,
            low=self.price * 0.999,
            close=self.price,
            volume=int(self.rng.integers(1000, 10000)),
            tick_count=int(self.rng.integers(10, 100)),
        )
    
    def has_more(self) -> bool:
        """Always true for infinite replay."""
        return True
    
    def reset(self) -> None:
        """Reset to initial state."""
        self.rng = np.random.default_rng(self.config.seed)
        self.price = self.config.start_price
        self.regime = "range_bound"
        self.regime_timer = 0
        self.tick_count = 0


class ReplayFromFile:
    """
    Replay ticks from a pre-generated JSON/Parquet file.
    
    Useful for deterministic backtesting with fixed data.
    """
    
    def __init__(self, filepath: Path) -> None:
        self.filepath = filepath
        self.index = 0
        self._ticks: list[Tick] = []
        self._load()
        
    def _load(self) -> None:
        """Load ticks from file."""
        if self.filepath.suffix == ".json":
            with open(self.filepath, "r") as f:
                data = json.load(f)
            for item in data:
                self._ticks.append(Tick(
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                    symbol=item["symbol"],
                    exchange=item["exchange"],
                    bid_price=item["bid_price"],
                    ask_price=item["ask_price"],
                    bid_size=item.get("bid_size", 0),
                    ask_size=item.get("ask_size", 0),
                    trade_price=item["trade_price"],
                    trade_size=item.get("trade_size", 0),
                ))
        elif self.filepath.suffix == ".parquet":
            import pandas as pd
            df = pd.read_parquet(self.filepath)
            for _, row in df.iterrows():
                self._ticks.append(Tick(
                    timestamp=pd.Timestamp(row["timestamp"]).to_pydatetime(),
                    symbol=row["symbol"],
                    exchange=row["exchange"],
                    bid_price=float(row["bid_price"]),
                    ask_price=float(row["ask_price"]),
                    bid_size=int(row.get("bid_size", 0)),
                    ask_size=int(row.get("ask_size", 0)),
                    trade_price=float(row["trade_price"]),
                    trade_size=int(row.get("trade_size", 0)),
                ))
    
    def next_tick(self) -> Tick:
        if self.index >= len(self._ticks):
            raise StopIteration("Replay exhausted")
        tick = self._ticks[self.index]
        self.index += 1
        return tick
    
    def next_bar(self) -> Bar:
        raise NotImplementedError("Bars not supported in file replay")
    
    def has_more(self) -> bool:
        return self.index < len(self._ticks)
    
    def reset(self) -> None:
        self.index = 0


class MockReplayAdapter(MarketDataAdapter):
    """
    Mock/Replay market-data adapter.
    
    Implements the MarketDataAdapter interface to provide either
    synthetic tick generation or file-based replay.
    """
    
    def __init__(self, config: Optional[MockConfig] = None, replay_file: Optional[Path] = None) -> None:
        self.config = config or MockConfig()
        self.replay_file = replay_file
        self._source: Optional[TickSource] = None
        self._running = False
        self._callbacks: Dict[str, list] = {}
        
        if replay_file and replay_file.exists():
            self._source = ReplayFromFile(replay_file)
        else:
            self._source = DeterministicMockSource(self.config)
    
    def connect(self) -> None:
        """Connect to the mock data source."""
        if hasattr(self._source, "reset"):
            self._source.reset()
        self._running = True
    
    def disconnect(self) -> None:
        """Disconnect from the mock data source."""
        self._running = False
    
    @property
    def is_connected(self) -> bool:
        return self._running
    
    def subscribe(self, symbol: str, callback: callable) -> None:
        """Subscribe to ticks for a symbol."""
        if symbol not in self._callbacks:
            self._callbacks[symbol] = []
        self._callbacks[symbol].append(callback)
    
    def unsubscribe(self, symbol: str, callback: callable) -> None:
        """Unsubscribe from ticks for a symbol."""
        if symbol in self._callbacks:
            self._callbacks[symbol] = [cb for cb in self._callbacks[symbol] if cb != callback]
    
    def get_ticks(self, count: int = 100) -> list[Tick]:
        """Get next N ticks from the source."""
        ticks = []
        for _ in range(count):
            try:
                tick = self._source.next_tick()
                ticks.append(tick)
                # Notify callbacks
                for cb in self._callbacks.get(tick.symbol, []):
                    cb(tick)
            except StopIteration:
                break
        return ticks
    
    def get_bars(self, count: int = 100) -> list[Bar]:
        """Get next N bars from the source."""
        bars = []
        for _ in range(count):
            try:
                bars.append(self._source.next_bar())
            except (StopIteration, NotImplementedError):
                break
        return bars
    
    def has_more(self) -> bool:
        """Check if more data is available."""
        return self._source.has_more() if self._source else False
    
    def reset(self) -> None:
        """Reset the data source."""
        if hasattr(self._source, "reset"):
            self._source.reset()
    
    def get_order_book(self, symbol: str, depth: int = 20) -> Optional[OrderBookL2]:
        """Generate a synthetic order book snapshot."""
        if self._source is None:
            return None
        
        # Get current price from last tick
        price = self.config.start_price
        if isinstance(self._source, DeterministicMockSource):
            price = self._source.price
        
        bids = []
        asks = []
        tick_size = 0.01
        
        for i in range(depth):
            level_price = price - tick_size * (i + 1)
            bids.append({
                "price": level_price,
                "size": int(np.random.integers(100, 1000)),
                "orders": int(np.random.integers(1, 10)),
            })
            asks.append({
                "price": price + tick_size * (i + 1),
                "size": int(np.random.integers(100, 1000)),
                "orders": int(np.random.integers(1, 10)),
            })
        
        return OrderBookL2(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(timezone.utc),
        )
    
    def generate_replay_file(self, output_path: Path, num_ticks: int = 10000) -> Path:
        """Generate a deterministic replay file for backtesting."""
        import pandas as pd
        
        ticks = []
        for _ in range(num_ticks):
            tick = self._source.next_tick()
            ticks.append({
                "timestamp": tick.timestamp.isoformat(),
                "symbol": tick.symbol,
                "exchange": tick.exchange,
                "bid_price": tick.bid_price,
                "ask_price": tick.ask_price,
                "bid_size": tick.bid_size,
                "ask_size": tick.ask_size,
                "trade_price": tick.trade_price,
                "trade_size": tick.trade_size,
            })
        
        df = pd.DataFrame(ticks)
        
        if output_path.suffix == ".json":
            df.to_json(output_path, orient="records", indent=2)
        elif output_path.suffix == ".parquet":
            df.to_parquet(output_path, index=False)
        
        return output_path