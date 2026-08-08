"""Transaction Cost Analysis (TCA) — every fill scored vs arrival price and VWAP.

Implementation shortfall attributed to spread/impact/timing and fed back into
the placement model and the backtest slippage calibration.

Rules:
- TCA is computed for every fill, not sampled
- Benchmark: arrival price (limit order submission time) + VWAP of the underlying
- Attribution: spread (limit vs market), impact (market move during fill), timing (delay cost)
- Results stored in `tca_records` table + streamed to Redis for Grafana
- Dashboard: per-strategy, per-underlying, per-symbol cost breakdown
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class FillRecord:
    """Raw fill data for TCA computation."""
    correlation_id: str
    symbol: str
    exchange: str
    side: str  # BUY or SELL
    quantity: int
    fill_price: float
    fill_time: datetime
    arrival_price: float  # Price at order submission time
    benchmark_price: float  # VWAP or TWAP benchmark
    order_type: str  # LIMIT, MARKET, SL, SL-M
    urgency: str  # IMMEDIATE, NORMAL, PACING
    strategy: str
    leg_label: Optional[str] = None
    algo_id: str = ""
    broker_order_id: str = ""
    spread_bps: float = 0.0  # Computed
    implementation_shortfall_bps: float = 0.0  # Computed
    impact_bps: float = 0.0  # Computed
    timing_cost_bps: float = 0.0  # Computed
    fill_ratio: float = 1.0  # filled_qty / order_qty
    time_to_fill_ms: float = 0.0  # Milliseconds from arrival to fill


@dataclass
class TCAResult:
    """Aggregated TCA result for a single fill."""
    correlation_id: str
    symbol: str
    side: str
    quantity: int
    arrival_price: float
    fill_price: float
    benchmark_price: float
    implementation_shortfall_bps: float
    spread_cost_bps: float
    impact_cost_bps: float
    timing_cost_bps: float
    total_cost_inr: float  # Absolute rupee cost
    total_cost_bps: float  # Total in basis points
    fill_ratio: float
    time_to_fill_ms: float
    quality_rating: str = "GOOD"  # EXCELLENT, GOOD, FAIR, POOR
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TCASummary:
    """Aggregated TCA across multiple fills."""
    period_start: datetime
    period_end: datetime
    total_fills: int
    total_volume_inr: float
    total_cost_inr: float
    avg_cost_bps: float
    median_cost_bps: float
    max_cost_bps: float
    p5_cost_bps: float  # 5th percentile
    p95_cost_bps: float  # 95th percentile
    fill_rate: float  # % of orders fully filled
    market_order_pct: float  # % market orders
    limit_order_pct: float  # % limit orders
    per_symbol: dict[str, dict] = field(default_factory=dict)
    per_strategy: dict[str, dict] = field(default_factory=dict)
    quality_distribution: dict[str, int] = field(default_factory=dict)


class TransactionCostAnalyzer:
    """Computes TCA for every fill and stores results."""

    def __init__(
        self,
        event_bus: Optional[Any] = None,
        store: Optional[Any] = None,  # TCAStore (DB-backed)
        benchmark_window_seconds: int = 300,  # 5-min VWAP window
    ) -> None:
        self._event_bus = event_bus
        self._store = store
        self._window = benchmark_window_seconds
        self._records: list[TCAResult] = []
        self._max_records = 100_000  # In-memory ring buffer

    def analyze_fill(self, fill: FillRecord) -> TCAResult:
        """Compute TCA for a single fill.
        
        This is the core computation — called for every fill in the system.
        The result feeds back into the placement model and backtest calibration.
        """
        # 1. Implementation shortfall (bps)
        if fill.side == "BUY":
            # Buying: shortfall = fill_price - arrival (we paid more than arrival)
            is_bps = ((fill.fill_price - fill.arrival_price) / fill.arrival_price) * 10000
        else:
            # Selling: shortfall = arrival - fill_price (we sold lower than arrival)
            is_bps = ((fill.arrival_price - fill.fill_price) / fill.arrival_price) * 10000

        # 2. Spread cost (bps) — half-spread for limit, full for market
        mid_price = (fill.arrival_price + fill.benchmark_price) / 2
        if fill.order_type == "MARKET":
            # Market order: full spread cost
            spread_bps = abs(fill.fill_price - mid_price) / mid_price * 10000
        else:
            # Limit order: half-spread (you provided liquidity or got partial)
            spread_bps = abs(fill.fill_price - mid_price) / mid_price * 10000 * 0.5

        # 3. Impact cost (bps) — price move after fill
        # If we bought and price went up, that's positive impact (we're lucky)
        # If we bought and price went down, that's negative impact (adverse selection)
        impact_bps = ((fill.benchmark_price - fill.fill_price) / fill.fill_price) * 10000

        # 4. Timing cost (bps) — cost of waiting
        # Approximate: if fill took > window, we paid timing cost
        time_hours = fill.time_to_fill_ms / 3_600_000
        # Annualized vol assumption for timing cost (simplified)
        annual_vol = 0.20  # 20% annual vol assumption
        daily_vol = annual_vol / 16  # Per-session vol
        timing_bps = daily_vol * (time_hours ** 0.5) * 10000 if time_hours > 0 else 0

        # 5. Total cost
        total_bps = spread_bps + impact_bps + timing_bps
        total_inr = fill.fill_price * fill.quantity * total_bps / 10000

        # 6. Quality rating
        quality = self._rate_quality(abs(is_bps), fill.fill_ratio, fill.time_to_fill_ms)

        result = TCAResult(
            correlation_id=fill.correlation_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            arrival_price=fill.arrival_price,
            fill_price=fill.fill_price,
            benchmark_price=fill.benchmark_price,
            implementation_shortfall_bps=is_bps,
            spread_cost_bps=spread_bps,
            impact_cost_bps=impact_bps,
            timing_cost_bps=timing_bps,
            total_cost_inr=total_inr,
            total_cost_bps=total_bps,
            fill_ratio=fill.fill_ratio,
            time_to_fill_ms=fill.time_to_fill_ms,
            quality_rating=quality,
        )

        # Store record
        self._records.append(result)
        if len(self._records) > self._max_records:
            self._records.pop(0)

        # Stream to event bus for Grafana
        if self._event_bus:
            self._event_bus.publish(
                "tca.fill",
                {
                    "correlation_id": result.correlation_id,
                    "symbol": result.symbol,
                    "cost_bps": round(result.total_cost_bps, 2),
                    "quality": result.quality_rating,
                    "timestamp": result.timestamp.isoformat(),
                },
            )

        # Persist to store
        if self._store:
            self._store.save(result)

        return result

    def compute_summary(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        symbols: Optional[list[str]] = None,
        strategies: Optional[list[str]] = None,
    ) -> TCASummary:
        """Compute aggregated TCA summary for a period."""
        records = self._records

        # Filter by time
        if start:
            records = [r for r in records if r.timestamp >= start]
        if end:
            records = [r for r in records if r.timestamp <= end]

        # Filter by symbols
        if symbols:
            records = [r for r in records if r.symbol in symbols]

        # Filter by strategies (would need strategy tag on TCAResult — add if needed)

        if not records:
            return TCASummary(
                period_start=start or datetime.now(timezone.utc),
                period_end=end or datetime.now(timezone.utc),
                total_fills=0,
                total_volume_inr=0.0,
                total_cost_inr=0.0,
                avg_cost_bps=0.0,
                median_cost_bps=0.0,
                max_cost_bps=0.0,
                p5_cost_bps=0.0,
                p95_cost_bps=0.0,
                fill_rate=0.0,
                market_order_pct=0.0,
                limit_order_pct=0.0,
            )

        costs_bps = [r.total_cost_bps for r in records]
        costs_bps_sorted = sorted(costs_bps)
        volumes = [r.fill_price * r.quantity for r in records]

        # Compute percentiles
        def percentile(data: list[float], p: float) -> float:
            if not data:
                return 0.0
            k = (len(data) - 1) * p / 100
            j = int(k)
            d = k - j
            if j + 1 < len(data):
                return data[j] * (1 - d) + data[j + 1] * d
            return data[j]

        total_volume = sum(volumes)
        fill_rate = sum(1 for r in records if r.fill_ratio >= 0.95) / len(records)

        # Per-symbol breakdown
        per_symbol: dict[str, dict] = {}
        for r in records:
            if r.symbol not in per_symbol:
                per_symbol[r.symbol] = {"fills": 0, "volume": 0.0, "cost_bps": []}
            per_symbol[r.symbol]["fills"] += 1
            per_symbol[r.symbol]["volume"] += r.fill_price * r.quantity
            per_symbol[r.symbol]["cost_bps"].append(r.total_cost_bps)

        for sym in per_symbol:
            cbs = per_symbol[sym]["cost_bps"]
            per_symbol[sym]["avg_cost_bps"] = sum(cbs) / len(cbs)
            per_symbol[sym]["max_cost_bps"] = max(cbs)
            del per_symbol[sym]["cost_bps"]  # Don't keep raw list

        # Per-strategy breakdown (would need strategy field on TCAResult)

        # Quality distribution
        quality_dist: dict[str, int] = {}
        for r in records:
            quality_dist[r.quality_rating] = quality_dist.get(r.quality_rating, 0) + 1

        return TCASummary(
            period_start=min(r.timestamp for r in records),
            period_end=max(r.timestamp for r in records),
            total_fills=len(records),
            total_volume_inr=total_volume,
            total_cost_inr=sum(costs_bps) * total_volume / 10000 / len(records) if records else 0,
            avg_cost_bps=sum(costs_bps) / len(costs_bps),
            median_cost_bps=percentile(costs_bps_sorted, 50),
            max_cost_bps=max(costs_bps),
            p5_cost_bps=percentile(costs_bps_sorted, 5),
            p95_cost_bps=percentile(costs_bps_sorted, 95),
            fill_rate=fill_rate,
            market_order_pct=0.0,  # Would need order_type field on records
            limit_order_pct=0.0,
            per_symbol=per_symbol,
            quality_distribution=quality_dist,
        )

    def get_recent_results(self, n: int = 100) -> list[TCAResult]:
        """Get the N most recent TCA results."""
        return self._records[-n:]

    def export_for_backtest_calibration(self) -> dict:
        """Export TCA data for backtest slippage calibration.
        
        Returns a summary that can be fed back into the backtest engine
        to adjust slippage parameters based on real fill data.
        """
        summary = self.compute_summary()
        return {
            "avg_cost_bps": round(summary.avg_cost_bps, 3),
            "median_cost_bps": round(summary.median_cost_bps, 3),
            "p95_cost_bps": round(summary.p95_cost_bps, 3),
            "fill_rate": round(summary.fill_rate, 4),
            "total_fills": summary.total_fills,
            "total_volume_inr": round(summary.total_volume_inr, 2),
            "per_symbol": {
                sym: {
                    "avg_cost_bps": round(data["avg_cost_bps"], 3),
                    "fills": data["fills"],
                    "volume": round(data["volume"], 2),
                }
                for sym, data in summary.per_symbol.items()
            },
        }

    def _rate_quality(self, abs_is_bps: float, fill_ratio: float, ttfs_ms: float) -> str:
        """Rate execution quality."""
        score = 100

        # Penalize high cost
        if abs_is_bps > 20:
            score -= 40
        elif abs_is_bps > 10:
            score -= 25
        elif abs_is_bps > 5:
            score -= 10

        # Penalize partial fills
        if fill_ratio < 0.5:
            score -= 30
        elif fill_ratio < 0.9:
            score -= 15

        # Penalize slow fills (for non-pacing urgency)
        if ttfs_ms > 60_000:
            score -= 20
        elif ttfs_ms > 30_000:
            score -= 10

        if score >= 80:
            return "EXCELLENT"
        elif score >= 60:
            return "GOOD"
        elif score >= 40:
            return "FAIR"
        else:
            return "POOR"


class TCAStore:
    """Persistent storage for TCA records — Postgres-backed."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._initialized = False

    async def initialize(self) -> None:
        """Create TCA tables if they don't exist."""
        if self._initialized:
            return
        await self._engine.execute("""
            CREATE TABLE IF NOT EXISTS tca_records (
                correlation_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                arrival_price NUMERIC(18,4) NOT NULL,
                fill_price NUMERIC(18,4) NOT NULL,
                benchmark_price NUMERIC(18,4) NOT NULL,
                implementation_shortfall_bps NUMERIC(12,4),
                spread_cost_bps NUMERIC(12,4),
                impact_cost_bps NUMERIC(12,4),
                timing_cost_bps NUMERIC(12,4),
                total_cost_inr NUMERIC(18,4),
                total_cost_bps NUMERIC(12,4),
                fill_ratio NUMERIC(8,4),
                time_to_fill_ms NUMERIC(12,2),
                quality_rating TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_tca_symbol ON tca_records(symbol);
            CREATE INDEX IF NOT EXISTS idx_tca_created ON tca_records(created_at);
            CREATE INDEX IF NOT EXISTS idx_tca_quality ON tca_records(quality_rating);
        """)
        self._initialized = True

    async def save(self, result: TCAResult) -> None:
        """Save a single TCA result."""
        await self._engine.execute(
            """INSERT INTO tca_records (
                correlation_id, symbol, side, quantity,
                arrival_price, fill_price, benchmark_price,
                implementation_shortfall_bps, spread_cost_bps,
                impact_cost_bps, timing_cost_bps,
                total_cost_inr, total_cost_bps,
                fill_ratio, time_to_fill_ms, quality_rating
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            ON CONFLICT (correlation_id) DO UPDATE SET
                fill_price = EXCLUDED.fill_price,
                benchmark_price = EXCLUDED.benchmark_price,
                total_cost_inr = EXCLUDED.total_cost_inr,
                total_cost_bps = EXCLUDED.total_cost_bps,
                quality_rating = EXCLUDED.quality_rating,
                created_at = NOW()""",
            result.correlation_id, result.symbol, result.side, result.quantity,
            result.arrival_price, result.fill_price, result.benchmark_price,
            result.implementation_shortfall_bps, result.spread_cost_bps,
            result.impact_cost_bps, result.timing_cost_bps,
            result.total_cost_inr, result.total_cost_bps,
            result.fill_ratio, result.time_to_fill_ms, result.quality_rating,
        )

    async def get_summary(
        self,
        start: datetime,
        end: datetime,
    ) -> dict:
        """Get aggregated TCA for a period from the database."""
        rows = await self._engine.fetch(
            """SELECT 
                COUNT(*) as fills,
                SUM(fill_price * quantity) as volume,
                AVG(total_cost_bps) as avg_cost_bps,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_cost_bps) as median_cost_bps,
                MAX(total_cost_bps) as max_cost_bps,
                AVG(CASE WHEN quality_rating = 'EXCELLENT' THEN 1.0 ELSE 0.0 END) as excellent_pct,
                AVG(CASE WHEN quality_rating = 'GOOD' THEN 1.0 ELSE 0.0 END) as good_pct,
                AVG(CASE WHEN quality_rating = 'FAIR' THEN 1.0 ELSE 0.0 END) as fair_pct,
                AVG(CASE WHEN quality_rating = 'POOR' THEN 1.0 ELSE 0.0 END) as poor_pct
            FROM tca_records
            WHERE created_at BETWEEN $1 AND $2""",
            start, end,
        )
        return dict(rows[0]) if rows else {}