"""Strategy validator — puts the 17 dormant strategies in strategies/factory.py
through the SAME DSR/PBO/Monte-Carlo gates that validated short_vol and
correctly rejected futures_trend, before any of them is allowed to touch
live capital.

WHY THIS EXISTS
---------------
Found 2026-09-07: MasterOrchestrator._node_execution_plan() (the live
AI-council money path) never uses strategies/factory.py's 19 registered
Strategy classes at all — it fabricates a generic "orchestrator_<regime>"
buy/sell candidate. Confirmed against real trade history (GET /db/trades):
of 172 trades ever recorded, every one is short_vol_condor/short_vol_exit/
exit_manager:expiry — zero from any of the other 18 catalogued strategies,
zero from the orchestrator's own path. Those 18 strategies are not stubs —
derivatives.py/equity.py implement real, specific entry logic — they are
simply never invoked, and (short_vol and futures_trend aside) never
validated either. Wiring the orchestrator to actually use them (a separate,
later change) would be reintroducing "fake edge" if it used unvalidated
strategies, so validation has to come first.

WHAT THIS DOES
--------------
Strategy.generate_signal(instrument, bars, now) is a single-shot "given the
history so far, what's today's signal" call — a different shape than
HypothesisHarness's exposure_fn(bars, params) -> full exposure series. This
module bridges the two: `make_exposure_fn()` walks a fresh Strategy instance
forward bar-by-bar, feeding it the growing bars-so-far window exactly as the
live pipeline would, opens a position on a BUY/SELL signal, and holds it
until the strategy's OWN exit_rules() (stop/target/max_holding_days) fire —
mirroring what ExitManager actually enforces live, not an idealized
mark-to-close. The resulting exposure series then goes through
HypothesisHarness's existing, already-trusted simulation and statistical
gates unchanged.

SCOPE
-----
Only strategies whose generate_signal() needs a single instrument's own
OHLCV history (no options chain / IV surface, no second correlated
instrument) can be validated this way: equity_momentum, swing_trend,
gap_strategy, mean_reversion, breakout. futures_trend is excluded (already
validated and REJECTED via trend_backtest.py — PBO 0.571). The remaining
strategies (options spreads needing a modeled IV surface; hedge_futures/
pair_hedge/expiry_rollover needing multi-instrument or expiry-aware data)
need their own validation approach and are deliberately left for later —
see the plan this module was built under.
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from trading_platform.backtesting.short_vol_backtest import DailyBar
from trading_platform.domain.enums import AssetClass, Exchange, InstrumentType, Segment, Side
from trading_platform.domain.models import Instrument, MarketBar
from trading_platform.research.hypothesis_harness import ExposureFn
from trading_platform.strategies.base import Strategy

logger = logging.getLogger(__name__)

# Strategies validatable with single-instrument OHLCV alone (see module
# docstring's SCOPE section). Keep this list explicit rather than deriving
# it from the factory — a strategy needing options/multi-instrument data
# would silently get bogus, misleadingly-flat exposure here otherwise.
SINGLE_INSTRUMENT_STRATEGIES: tuple[str, ...] = (
    "equity_momentum", "swing_trend", "gap_strategy", "mean_reversion", "breakout",
)

# Single source of truth for the validation results, written by
# scripts/validate_dormant_strategies.py and read by both that script's own
# summary and MasterOrchestrator's strategy-selection node (see
# load_accepted_strategies() below) — one file, one acceptance rule, applied
# consistently wherever "is this strategy allowed to trade live" is asked.
REGISTRY_PATH = Path("data/strategy_validation_registry.json")

# A strategy passing on only one of several independently-tested symbols is
# exactly the single-trial luck DSR/PBO exist to catch across a param grid,
# not across symbols — testing N symbols and keeping whichever one passed is
# its own multiple-comparisons problem (see HypothesisHarness.evaluate_all's
# own warning about this trap). Require a real majority.
ACCEPTANCE_PASS_RATE = 0.5


def load_accepted_strategies(registry_path: Path | str = REGISTRY_PATH) -> set[str]:
    """Strategy names that passed on a majority of the symbols they were
    tested against, per the most recent scripts/validate_dormant_strategies.py
    run. Returns an empty set (never raises) if the registry doesn't exist
    yet or is malformed — the live orchestrator must degrade to "no
    additional strategy available" rather than crash on a missing research
    artifact."""
    path = Path(registry_path)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("strategy_validator: could not read registry %s: %s", path, exc)
        return set()

    by_strategy: dict[str, list[bool]] = {}
    for row in data.get("results", []):
        name = row.get("strategy_name")
        if not name:
            continue
        by_strategy.setdefault(name, []).append(bool(row.get("passed")))

    return {
        name for name, outcomes in by_strategy.items()
        if outcomes and (sum(outcomes) / len(outcomes)) > ACCEPTANCE_PASS_RATE
    }


def load_market_bars_csv(path: Path | str, symbol: str) -> list[MarketBar]:
    """Load a full-OHLCV historical CSV (timestamp,open,high,low,close,volume
    — the same files short_vol_backtest.load_daily_closes reads, but keeping
    every column instead of close-only) into MarketBar objects, sorted by
    date. Strategy.generate_signal() needs high/low/volume that DailyBar
    deliberately doesn't carry (FeatureEngine.compute() uses ATR/volume_ratio
    from them)."""
    bars: list[MarketBar] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = str(row.get("timestamp") or "").strip()
            try:
                ts = datetime.fromisoformat(raw)
                close = float(row["close"])
            except (ValueError, KeyError, TypeError):
                continue
            if close <= 0:
                continue
            bars.append(MarketBar(
                timestamp=ts, symbol=symbol,
                open=float(row.get("open", close) or close),
                high=float(row.get("high", close) or close),
                low=float(row.get("low", close) or close),
                close=close,
                volume=int(float(row.get("volume", 0) or 0)),
            ))
    bars.sort(key=lambda b: b.timestamp)
    return bars


def to_daily_bars(market_bars: Sequence[MarketBar]) -> list[DailyBar]:
    """The same date/close series as market_bars, in HypothesisHarness's own
    DailyBar shape — the two lists stay index-aligned by construction here,
    which make_exposure_fn's date_to_idx lookup relies on."""
    return [DailyBar(day=b.timestamp.date(), close=b.close) for b in market_bars]


def _test_instrument(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol, name=symbol, exchange=Exchange.NSE, segment=Segment.CASH,
        asset_class=AssetClass.EQUITY, instrument_type=InstrumentType.EQUITY,
        token="VALIDATION", lot_size=1,
    )


def make_exposure_fn(strategy_name: str, symbol: str, market_bars: Sequence[MarketBar]) -> ExposureFn:
    """Build a HypothesisHarness-compatible exposure_fn for `strategy_name`.

    `market_bars` must be the SAME dates, same order, as the DailyBar
    sequence the harness will later call this function with (build both from
    the same load_market_bars_csv() call via to_daily_bars() — see
    validate_strategy() below). A fresh Strategy instance is constructed
    per call since generate_signal() itself is stateless, but this wrapper
    tracks open-position state across the walk-forward loop.

    `params["stop_target_scale"]` (default 1.0) scales the strategy's OWN
    exit_rules().stop_loss_pct/target_pct by a constant factor. This is the
    one tunable knob exposed here: these Strategy classes hardcode their
    entry thresholds as class constants rather than taking a params dict, so
    entry logic itself is not swept (rewriting each strategy to accept entry
    params is out of scope for validation) — but DSR/PBO are selection-bias
    gates that only activate with >=2 real variants (HypothesisHarness only
    calls evaluate_dsr/evaluate_pbo when len(runs) >= 2), so a single fixed
    variant would silently skip the two gates that matter most. Sweeping
    stop/target sizing is a genuine, honest search dimension — it changes
    risk-taking, not the signal itself — rather than a cosmetic grid added
    only to make the gates fire.
    """
    from trading_platform.strategies.factory import StrategyFactory

    instrument = _test_instrument(symbol)
    date_to_idx = {b.timestamp.date(): i for i, b in enumerate(market_bars)}

    def exposure_fn(bars: Sequence[DailyBar], params: dict) -> list[float]:
        scale = float(params.get("stop_target_scale", 1.0))
        strategy: Strategy = StrategyFactory().get(strategy_name)
        base_exit = strategy.exit_rules()
        stop_pct = base_exit.stop_loss_pct * scale
        target_pct = base_exit.target_pct * scale
        max_holding_days = base_exit.max_holding_days
        n = len(bars)
        exposures = [0.0] * n
        position = 0.0       # +1.0 long, -1.0 short, 0.0 flat
        entry_price = 0.0
        entry_idx = -1

        for i in range(n):
            idx = date_to_idx.get(bars[i].day)

            if position != 0.0 and entry_price > 0:
                held_days = i - entry_idx
                ret = (bars[i].close - entry_price) / entry_price
                if position > 0:
                    hit_stop = ret <= -stop_pct
                    hit_target = ret >= target_pct
                else:
                    hit_stop = ret >= stop_pct
                    hit_target = ret <= -target_pct
                if hit_stop or hit_target or held_days >= max_holding_days:
                    position = 0.0
                    entry_idx = -1

            if position == 0.0 and idx is not None:
                bars_so_far = market_bars[: idx + 1]
                try:
                    signal = strategy.generate_signal(
                        instrument, list(bars_so_far),
                        now=datetime.combine(bars[i].day, datetime.min.time(), tzinfo=timezone.utc),
                    )
                except Exception as exc:
                    logger.debug("strategy_validator: %s.generate_signal failed on %s: %s",
                                 strategy_name, bars[i].day, exc)
                    signal = None
                if signal is not None:
                    position = 1.0 if signal.side == Side.BUY else -1.0
                    entry_price = bars[i].close
                    entry_idx = i

            exposures[i] = position

        return exposures

    return exposure_fn


@dataclass
class ValidationRecord:
    strategy_name: str
    symbol: str
    passed: bool
    cagr: float
    sharpe: float
    max_drawdown: float
    dsr_message: str
    pbo_message: str
    evaluated_at: str


def _stop_target_grid() -> list[dict]:
    return [{"stop_target_scale": s} for s in (0.75, 1.0, 1.25, 1.5)]


def validate_strategy(strategy_name: str, symbol: str, csv_path: Path | str) -> ValidationRecord:
    """Run one strategy against one symbol's real historical OHLCV through
    HypothesisHarness's existing gates, sweeping stop/target sizing (see
    make_exposure_fn's docstring) as the real, honest param_grid DSR/PBO
    need to mean anything."""
    from trading_platform.research.hypothesis_harness import HypothesisHarness, HypothesisSpec

    market_bars = load_market_bars_csv(csv_path, symbol)
    daily_bars = to_daily_bars(market_bars)
    if len(daily_bars) < 100:
        raise ValueError(f"insufficient history for {symbol} ({len(daily_bars)} bars)")

    spec = HypothesisSpec(
        name=f"{strategy_name}::{symbol}",
        exposure_fn=make_exposure_fn(strategy_name, symbol, market_bars),
        param_grid=_stop_target_grid(),
        description=f"validation.py: live-shaped generate_signal()+exit_rules() walk-forward for {strategy_name}",
    )
    verdict = HypothesisHarness().evaluate(spec, daily_bars)

    return ValidationRecord(
        strategy_name=strategy_name,
        symbol=symbol,
        passed=verdict.passed,
        cagr=verdict.best_cagr,
        sharpe=verdict.best_sharpe,
        max_drawdown=verdict.best_max_drawdown,
        dsr_message=verdict.gates.dsr.message if verdict.gates.dsr else "",
        pbo_message=verdict.gates.pbo.message if verdict.gates.pbo else "",
        evaluated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
