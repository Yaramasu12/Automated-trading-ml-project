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
Two families:

1. Single-instrument OHLCV strategies (make_exposure_fn/validate_strategy):
   equity_momentum, swing_trend, gap_strategy, mean_reversion, breakout.
   futures_trend is excluded (already validated and REJECTED via
   trend_backtest.py — PBO 0.571) — and so, by inheritance, are
   hedge_futures/pair_hedge/expiry_rollover: all three are bare
   FuturesTrendStrategy subclasses in derivatives.py with ZERO method
   overrides (confirmed by reading the class bodies), so their behavior is
   byte-for-byte identical to futures_trend's already-rejected logic. No
   separate backtest can produce a different verdict for them.

2. Single-leg option-buying strategies (make_option_exposure_fn/
   validate_option_strategy): defined_risk_option_spread and
   volatility_breakout_options, whose entries are priced via derivatives.py's
   own `_atm_option_premium()` Brenner-Subrahmanyam approximation (spot,
   realized vol, days-to-expiry only — no real strike/IV-surface data
   needed). bull_call_spread and bear_put_spread are UNMODIFIED subclasses
   of the same DefinedRiskOptionSpreadStrategy code path (OptionsTemplateStrategy
   only relabels the signal), so defined_risk_option_spread's verdict covers
   them too.

NOT validatable at all today: long_straddle, short_straddle, strangle,
iron_condor, calendar_spread, delta_neutral_hedge. Their generate_signal()
unconditionally returns None via derivatives.py's own `_multi_leg_disabled()`
helper — "Multi-leg strategies require atomically submitting both legs at
once... Return None to suppress all signals until the feature is
implemented." These are not rejected by backtesting; they are structurally
inert pending a MultiLegOrderManager that does not exist yet, and no
validation approach can produce a signal from code that always returns None.
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

# Single-leg option-buying strategies validatable via the ATM-premium
# approximation (see module docstring's SCOPE section, family 2).
OPTION_STRATEGIES: tuple[str, ...] = (
    "defined_risk_option_spread", "volatility_breakout_options",
)

# Bare FuturesTrendStrategy subclasses with zero method overrides (confirmed
# by reading derivatives.py) — REJECTED by inheritance from futures_trend's
# own already-recorded trend_backtest.py verdict (PBO 0.571), not by a
# separate backtest run. Exposed here so callers can record this
# relationship explicitly rather than silently omitting these three.
INHERITS_FUTURES_TREND_VERDICT: tuple[str, ...] = ("hedge_futures", "pair_hedge", "expiry_rollover")

# generate_signal() unconditionally returns None (derivatives.py's own
# _multi_leg_disabled() helper) pending a MultiLegOrderManager that doesn't
# exist yet. Not rejected by backtesting -- structurally untestable today.
STRUCTURALLY_DISABLED_STRATEGIES: tuple[str, ...] = (
    "long_straddle", "short_straddle", "strangle", "iron_condor",
    "calendar_spread", "delta_neutral_hedge",
)

# bull_call_spread/bear_put_spread are unmodified subclasses of
# OptionsTemplateStrategy(DefinedRiskOptionSpreadStrategy) -- the parent
# only relabels the signal's reason/metadata, entry/exit math is identical.
# defined_risk_option_spread's verdict applies to these by inheritance too.
INHERITS_DEFINED_RISK_SPREAD_VERDICT: tuple[str, ...] = ("bull_call_spread", "bear_put_spread")

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


# ── Option-buying strategies (defined_risk_option_spread, ────────────────────
#    volatility_breakout_options, and by inheritance bull_call_spread/
#    bear_put_spread) ─────────────────────────────────────────────────────────
#
# These strategies price their entries via derivatives.py's own
# _atm_option_premium(spot, realized_vol, days_to_expiry) — a closed-form
# Brenner-Subrahmanyam approximation needing only the underlying's own
# spot/vol path, no real strike-level chain or IV surface. That means the
# *actual* profit/loss of holding one of these positions is driven by how
# the OPTION PREMIUM moves day to day (leveraged, convex), not by the
# underlying's raw price return — feeding HypothesisHarness the underlying's
# own close series (as make_exposure_fn does for directional strategies)
# would radically understate an option buyer's real volatility and P&L.
#
# Fix: build a SEPARATE, rolling synthetic-premium DailyBar series —
# "what would a constant-7-day-tenor ATM option on this underlying be worth
# today" — recomputed every trading day from that day's own realized vol.
# This is well-defined for every day regardless of whether a position is
# actually open (unlike "the specific contract this backtest entered",
# which only exists between its own entry and exit), so it can be built up
# front, independent of any exposure decision, and handed to
# HypothesisHarness as if it were the traded instrument's own price series
# — every existing piece of _simulate()/DSR/PBO/Monte-Carlo machinery then
# applies unchanged, just to premium returns instead of spot returns.
_SYNTHETIC_OPTION_DTE = 7


def build_synthetic_option_premium_bars(market_bars: Sequence[MarketBar]) -> list[DailyBar]:
    """One synthetic DailyBar per market_bars entry: the theoretical value of
    a constant-_SYNTHETIC_OPTION_DTE-day ATM option on this underlying, using
    the SAME _atm_option_premium() formula generate_signal() itself prices
    entries with (expiry=None on the synthetic instrument defaults
    generate_signal()'s own days_to_expiry to 7 — kept in sync with this
    constant deliberately). The first 21 bars (FeatureEngine's own minimum)
    repeat the first computable value rather than guessing."""
    from trading_platform.ai.features import FeatureEngine
    from trading_platform.strategies.derivatives import _atm_option_premium

    out: list[DailyBar] = []
    first_value: float | None = None
    for i in range(len(market_bars)):
        day = market_bars[i].timestamp.date()
        if i < 20:
            out.append(DailyBar(day=day, close=0.0))  # placeholder, backfilled below
            continue
        try:
            f = FeatureEngine().compute(list(market_bars[: i + 1]))
            premium = _atm_option_premium(f.close, f.realized_volatility, _SYNTHETIC_OPTION_DTE)
        except Exception:
            premium = out[-1].close if out and out[-1].close > 0 else 1.0
        if first_value is None:
            first_value = premium
        out.append(DailyBar(day=day, close=premium))

    if first_value is not None:
        for i in range(min(20, len(out))):
            out[i] = DailyBar(day=out[i].day, close=first_value)
    return out


def _option_instrument(symbol: str, option_type) -> Instrument:
    return Instrument(
        symbol=f"{symbol}_SYNTH_OPT", name=symbol, exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.EQUITY, instrument_type=InstrumentType.OPTION,
        token="VALIDATION_OPT", lot_size=1, option_type=option_type, underlying=symbol,
        expiry=None,  # generate_signal() defaults days_to_expiry to 7 when None
    )


def make_option_exposure_fn(
    strategy_name: str, symbol: str, market_bars: Sequence[MarketBar],
) -> ExposureFn:
    """Like make_exposure_fn, but for the option-buying strategies described
    in this section's module comment above: the `bars` the returned function
    receives (and that HypothesisHarness simulates P&L from) must be the
    SYNTHETIC PREMIUM series from build_synthetic_option_premium_bars(), not
    the underlying's own OHLCV — both entry decisions (via market_bars, real
    underlying data feeding generate_signal()'s momentum/vol features) and
    exit/P&L (via the premium bars) matter and come from different series.
    """
    from trading_platform.domain.enums import OptionType
    from trading_platform.strategies.factory import StrategyFactory

    date_to_idx = {b.timestamp.date(): i for i, b in enumerate(market_bars)}

    def exposure_fn(bars: Sequence[DailyBar], params: dict) -> list[float]:
        # bars here IS the synthetic premium series (see docstring) — same
        # length/dates as market_bars by construction.
        scale = float(params.get("stop_target_scale", 1.0))
        strategy: Strategy = StrategyFactory().get(strategy_name)
        base_exit = strategy.exit_rules()
        stop_pct = base_exit.stop_loss_pct * scale
        target_pct = base_exit.target_pct * scale
        max_holding_days = base_exit.max_holding_days
        n = len(bars)
        exposures = [0.0] * n
        position = 0.0
        entry_premium = 0.0
        entry_idx = -1

        for i in range(n):
            idx = date_to_idx.get(bars[i].day)

            if position != 0.0 and entry_premium > 0:
                held_days = i - entry_idx
                ret = (bars[i].close - entry_premium) / entry_premium
                # All strategies in this family are BUY-only (Side.BUY —
                # confirmed in derivatives.py: DefinedRiskOptionSpreadStrategy
                # and VolatilityBreakoutOptionsStrategy both only ever return
                # Side.BUY) so exit logic is long-only, unlike
                # make_exposure_fn's directional stop/target branches.
                hit_stop = ret <= -stop_pct
                hit_target = ret >= target_pct
                if hit_stop or hit_target or held_days >= max_holding_days:
                    position = 0.0
                    entry_idx = -1

            if position == 0.0 and idx is not None and idx >= 20:
                mb = market_bars[idx]
                # Mirrors decision/pipeline.py's own _select_instrument()
                # CE/PE choice for the options family: today's bar direction.
                option_type = OptionType.CE if mb.close >= mb.open else OptionType.PE
                instrument = _option_instrument(symbol, option_type)
                try:
                    signal = strategy.generate_signal(
                        instrument, list(market_bars[: idx + 1]),
                        now=datetime.combine(bars[i].day, datetime.min.time(), tzinfo=timezone.utc),
                    )
                except Exception as exc:
                    logger.debug("strategy_validator: %s.generate_signal failed on %s: %s",
                                 strategy_name, bars[i].day, exc)
                    signal = None
                if signal is not None and bars[i].close > 0:
                    position = 1.0
                    entry_premium = bars[i].close
                    entry_idx = i

            exposures[i] = position

        return exposures

    return exposure_fn


def validate_option_strategy(strategy_name: str, symbol: str, csv_path: Path | str) -> ValidationRecord:
    """Same contract as validate_strategy(), for the option-buying family —
    see this module's option-strategies section for why P&L is simulated
    against a synthetic rolling ATM-premium series rather than the
    underlying's own close."""
    from trading_platform.research.hypothesis_harness import HypothesisHarness, HypothesisSpec

    market_bars = load_market_bars_csv(csv_path, symbol)
    if len(market_bars) < 100:
        raise ValueError(f"insufficient history for {symbol} ({len(market_bars)} bars)")
    premium_bars = build_synthetic_option_premium_bars(market_bars)

    spec = HypothesisSpec(
        name=f"{strategy_name}::{symbol}",
        exposure_fn=make_option_exposure_fn(strategy_name, symbol, market_bars),
        param_grid=_stop_target_grid(),
        description=(
            f"validation.py: live-shaped generate_signal()+exit_rules() walk-forward for "
            f"{strategy_name}, P&L simulated against a synthetic rolling ATM-premium series"
        ),
    )
    verdict = HypothesisHarness().evaluate(spec, premium_bars)

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
