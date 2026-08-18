"""Historical backtest for the short-vol (index options) strategy.

REDESIGN_PROMPT.md §5 asks every strategy to earn promotion through real
validation gates. Short-vol could not, because nothing here could produce an
equity curve for it: the generic `BacktestEngine` drives single-instrument
directional strategies off `MarketBar`s, whereas short-vol sells multi-leg
defined-risk option structures.

This module fills that gap. Design constraints, in priority order:

1. **Drive the REAL strategy.** Entry decisions come from
   `ShortVolStrategy.decide()` — the same pure function the live executor
   calls — not a reimplementation. If the live gating changes, this backtest
   changes with it. Only *simulation* of the resulting position lives here.
2. **Use real market inputs.** Underlying closes and India VIX both come from
   Angel One daily history. IV is NEVER proxied from realized vol: VRP is
   defined as implied minus realized, so deriving implied from realized would
   manufacture the very edge under test.
3. **Be pessimistic where uncertain.** Option marks are Black-Scholes model
   prices, not traded prices (see LIMITATIONS). Every modelling choice that
   could flatter the result is deliberately biased against the strategy —
   spread crossed on entry AND exit, on every leg.

LIMITATIONS (read before trusting any number this produces)
-----------------------------------------------------------
* **Model prices, not market prices.** There is no historical option-chain
  data here, so legs are priced with Black-Scholes off India VIX. Real fills
  differ — most importantly, index puts trade above a flat-vol model because
  of skew, which this approximates (see `smile_iv`) but does not fit to a
  real surface.
* **A single IV per day.** India VIX is a 30-day NIFTY-based measure. Using
  it for other indices, or for a non-30-day tenor, is an approximation.
* **No intraday path.** Exits are evaluated on daily closes, so an intraday
  spike through a stop is only seen at that day's close. For a defined-risk
  structure this bounds error (max loss is capped by the wing) but it does
  understate stop-out frequency.
* **No liquidity/impact model** beyond the spread assumption; no partial
  fills; assumes every constructed strike was actually listed and tradeable.

The honest reading: this measures whether the strategy's *logic* has edge
under a realistic-but-idealised cost model. It is evidence for promotion to
paper, not proof of live profitability.
"""
from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trading_platform.domain.enums import OptionType, Side
from trading_platform.strategies.short_vol import ShortVolStrategy

logger = logging.getLogger(__name__)

# Per-index contract specs (mirrors data/instrument_master.INDEX_UNDERLYINGS;
# duplicated as plain data so the backtest doesn't need a live InstrumentMaster).
INDEX_SPECS: dict[str, dict[str, float]] = {
    "NIFTY": {"lot_size": 50, "strike_step": 50, "wing": 300},
    "BANKNIFTY": {"lot_size": 15, "strike_step": 100, "wing": 700},
    "FINNIFTY": {"lot_size": 40, "strike_step": 50, "wing": 300},
}

# Round-turn cost per option leg, in index points, applied on BOTH entry and
# exit of EVERY leg. NSE index options quote in rupees per unit with typical
# OTM weekly bid-ask of ~0.5-2.0 points; 1.0 is a mid-range, deliberately
# non-flattering assumption for a 4-leg condor (8 crossings per round trip).
DEFAULT_SPREAD_POINTS = 1.0

RISK_FREE_RATE = 0.065
TRADING_DAYS = 252.0


# ─── Pricing ─────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot: float, strike: float, t_years: float, iv: float,
                  option_type: OptionType, r: float = RISK_FREE_RATE) -> float:
    """European option price. At/after expiry (or zero vol) returns intrinsic."""
    if t_years <= 0 or iv <= 0:
        intrinsic = (spot - strike) if option_type == OptionType.CE else (strike - spot)
        return max(0.0, intrinsic)
    sig_t = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / sig_t
    d2 = d1 - sig_t
    if option_type == OptionType.CE:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def smile_iv(atm_iv: float, spot: float, strike: float, option_type: OptionType,
             skew_per_10pct: float = 0.25) -> float:
    """ATM IV adjusted for index volatility skew.

    A flat-vol model systematically UNDER-prices OTM index puts, which is
    exactly the leg short-vol sells — pricing the whole structure at ATM vol
    would overstate the credit received and flatter the strategy. This applies
    the well-documented equity-index skew: IV rises as strikes move down.

    `skew_per_10pct` = additional IV (as a fraction of ATM IV) per 10% of
    downside moneyness. 0.25 means a strike 10% below spot prices at 1.25x ATM
    IV — a conservative reading of typical NIFTY skew. Calls get a milder
    reverse adjustment, since index call skew is much flatter than put skew.
    """
    if spot <= 0 or atm_iv <= 0:
        return atm_iv
    moneyness = (strike - spot) / spot          # negative = below spot
    if moneyness < 0:
        adj = 1.0 + skew_per_10pct * (abs(moneyness) / 0.10)
    else:
        # Upside: mild smile, capped so far-OTM calls don't price at ~0 vol.
        adj = max(0.85, 1.0 - 0.5 * skew_per_10pct * (moneyness / 0.10))
    return atm_iv * adj


# ─── Data loading ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyBar:
    day: date
    close: float


def load_daily_closes(path: Path | str) -> list[DailyBar]:
    """Load `timestamp,open,high,low,close,volume` CSV written by the history
    fetch. Rows with an unparseable date or non-positive close are skipped."""
    out: list[DailyBar] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = str(row.get("timestamp") or "").strip()
            try:
                day = datetime.fromisoformat(raw).date()
                close = float(row["close"])
            except (ValueError, KeyError, TypeError):
                continue
            if close > 0:
                out.append(DailyBar(day, close))
    out.sort(key=lambda b: b.day)
    return out


# ─── Simulation ──────────────────────────────────────────────────────────────

@dataclass
class ShortVolTrade:
    entry_day: date
    exit_day: date | None = None
    underlying: str = ""
    structure: str = ""
    lots: int = 0
    entry_credit_points: float = 0.0     # per lot, net of spread
    exit_debit_points: float = 0.0       # per lot, net of spread
    pnl: float = 0.0                     # rupees, net of costs
    charges: float = 0.0
    exit_reason: str = ""
    max_loss_points: float = 0.0

    @property
    def held_days(self) -> int:
        if self.exit_day is None:
            return 0
        return (self.exit_day - self.entry_day).days


@dataclass
class ShortVolBacktestResult:
    underlying: str
    params: dict[str, float]
    starting_capital: float
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    trades: list[ShortVolTrade] = field(default_factory=list)
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def trade_pnls(self) -> list[float]:
        return [t.pnl for t in self.trades]

    @property
    def total_charges(self) -> float:
        return sum(t.charges for t in self.trades)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_capital

    @property
    def daily_returns(self) -> list[float]:
        vals = [v for _, v in self.equity_curve]
        return [
            (vals[i] - vals[i - 1]) / vals[i - 1]
            for i in range(1, len(vals))
            if vals[i - 1] > 0
        ]

    def to_dict(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        return {
            "underlying": self.underlying,
            "params": self.params,
            "trades": len(self.trades),
            "wins": len(wins),
            "win_rate": (len(wins) / len(self.trades)) if self.trades else 0.0,
            "net_pnl": round(self.final_equity - self.starting_capital, 2),
            "total_charges": round(self.total_charges, 2),
            "final_equity": round(self.final_equity, 2),
            "skipped_reasons": dict(sorted(
                self.skipped_reasons.items(), key=lambda kv: -kv[1]
            )[:6]),
        }


class ShortVolBacktester:
    """Replays daily history through the real `ShortVolStrategy.decide()`."""

    def __init__(
        self,
        *,
        underlying: str = "NIFTY",
        starting_capital: float = 1_000_000.0,
        hold_days: int = 5,
        profit_target_pct: float = 0.50,     # matches SHORTVOL_PROFIT_TARGET_PCT
        stop_loss_multiple: float = 1.5,     # matches SHORTVOL_STOP_LOSS_MULTIPLE
        spread_points: float = DEFAULT_SPREAD_POINTS,
        entry_weekday: int = 0,              # Monday, matches SHORTVOL_ENTRY_WEEKDAY
        structure: str = "condor",           # condor | put_spread | call_spread
        strategy: ShortVolStrategy | None = None,
    ) -> None:
        spec = INDEX_SPECS.get(underlying.upper(), INDEX_SPECS["NIFTY"])
        self.underlying = underlying.upper()
        self.lot_size = int(spec["lot_size"])
        self.strike_step = int(spec["strike_step"])
        self.wing = float(spec["wing"])
        self.starting_capital = starting_capital
        self.hold_days = hold_days
        self.profit_target_pct = profit_target_pct
        self.stop_loss_multiple = stop_loss_multiple
        self.spread_points = spread_points
        self.entry_weekday = entry_weekday
        # REDESIGN §4.2: condor harvests two-sided premium; put_spread targets
        # the downside skew that makes OTM index puts systematically rich —
        # a genuinely different premium source, not a parameter tweak.
        self.structure = (structure or "condor").lower()
        self.strategy = strategy or ShortVolStrategy()

    # -- structure valuation ------------------------------------------------

    def _structure_value(self, legs, spot: float, atm_iv: float, t_years: float) -> float:
        """Cost to CLOSE the structure, in index points (positive = we pay).

        Short legs must be bought back, long legs sold — so this is
        sum(short marks) - sum(long marks), each priced at its own skew-
        adjusted IV.
        """
        value = 0.0
        for leg in legs:
            iv = smile_iv(atm_iv, spot, leg.strike, leg.option_type)
            px = black_scholes(spot, leg.strike, t_years, iv, leg.option_type)
            value += px if leg.side == Side.SELL else -px
        return value

    def _leg_spread_cost(self, legs) -> float:
        """Spread paid crossing every leg once, in points."""
        return self.spread_points * len(legs)

    # -- charges ------------------------------------------------------------

    def _charges(self, legs, lots: int, entry_points: float, exit_points: float) -> float:
        """Real Indian F&O option costs on premium turnover.

        Deliberately computed here rather than through `ChargesModel.estimate()`:
        that API takes an `OrderIntent` bound to a live `Instrument`, which this
        offline replay has no way to construct. The rates below mirror
        backtesting/charges.py's `_options_charges` — STT 0.0625% on SELL-side
        premium, exchange txn 0.05%, GST 18% on (brokerage + txn), SEBI 0.0001%,
        stamp 0.003% on buy — keeping this consistent with the live cost model.
        """
        qty = lots * self.lot_size
        n_legs = max(1, len(legs))
        # Premium turnover across both sides of the round trip.
        sell_turnover = abs(entry_points) * qty
        buy_turnover = abs(exit_points) * qty

        brokerage = min(20.0, 0.0003 * (sell_turnover + buy_turnover)) * n_legs * 2
        stt = 0.000625 * sell_turnover
        exch = 0.0005 * (sell_turnover + buy_turnover)
        gst = 0.18 * (brokerage + exch)
        sebi = 0.000001 * (sell_turnover + buy_turnover)
        stamp = 0.00003 * buy_turnover
        return brokerage + stt + exch + gst + sebi + stamp

    # -- main loop ----------------------------------------------------------

    def run(
        self,
        bars: list[DailyBar],
        vix_by_day: dict[date, float],
        *,
        warmup: int = 21,
    ) -> ShortVolBacktestResult:
        result = ShortVolBacktestResult(
            underlying=self.underlying,
            params={
                "sd": self.strategy.sd,
                "wing": self.wing,
                "min_vrp": self.strategy.min_vrp,
                "risk_budget": self.strategy.risk_budget,
                "kelly_fraction": self.strategy.kelly_fraction,
                "hold_days": float(self.hold_days),
                "profit_target_pct": self.profit_target_pct,
                "stop_loss_multiple": self.stop_loss_multiple,
                "spread_points": self.spread_points,
            },
            starting_capital=self.starting_capital,
        )
        equity = self.starting_capital
        open_trade: ShortVolTrade | None = None
        open_legs = None
        open_entry_idx = 0

        for i, bar in enumerate(bars):
            if i < warmup:
                result.equity_curve.append((bar.day, equity))
                continue
            vix = vix_by_day.get(bar.day)
            if vix is None or vix <= 0:
                result.equity_curve.append((bar.day, equity))
                continue

            # ---- manage an open position -------------------------------
            unrealized = 0.0
            if open_trade is not None and open_legs is not None:
                held = i - open_entry_idx
                dte_left = max(0, self.hold_days - held)
                t_left = dte_left / TRADING_DAYS
                close_cost = self._structure_value(
                    open_legs, bar.close, vix / 100.0, t_left
                ) + self._leg_spread_cost(open_legs)
                credit = open_trade.entry_credit_points
                pnl_points = credit - close_cost
                # Mark the OPEN position to market every day. Without this the
                # equity curve only moved on trade close, leaving ~90% of daily
                # returns at exactly zero — which collapses the return series'
                # standard deviation and inflates Sharpe (and therefore DSR)
                # via the classic stale-pricing artifact. Measured on NIFTY:
                # 0.71 marked daily vs 2.28 on close-days-only.
                unrealized = max(pnl_points, -open_trade.max_loss_points) * (
                    open_trade.lots * self.lot_size
                )

                reason = ""
                if pnl_points >= self.profit_target_pct * credit:
                    reason = "profit_target"
                elif pnl_points <= -self.stop_loss_multiple * credit:
                    reason = "stop_loss"
                elif dte_left <= 0:
                    reason = "expiry"

                if reason:
                    # Loss is bounded by the wing — a defined-risk structure
                    # cannot lose more than (wing - credit) per lot.
                    pnl_points = max(pnl_points, -open_trade.max_loss_points)
                    qty = open_trade.lots * self.lot_size
                    charges = self._charges(
                        open_legs, open_trade.lots, credit, close_cost
                    )
                    pnl = pnl_points * qty - charges
                    open_trade.exit_day = bar.day
                    open_trade.exit_debit_points = close_cost
                    open_trade.pnl = pnl
                    open_trade.charges = charges
                    open_trade.exit_reason = reason
                    equity += pnl
                    result.trades.append(open_trade)
                    open_trade, open_legs = None, None
                    unrealized = 0.0

            # ---- consider a new entry ----------------------------------
            if open_trade is None and bar.day.weekday() == self.entry_weekday:
                closes = [b.close for b in bars[max(0, i - 60): i + 1]]
                decision = self.strategy.decide(
                    spot=bar.close,
                    vix=vix,
                    closes=closes,
                    capital=equity,
                    lot_size=self.lot_size,
                    strike_step=self.strike_step,
                    wing_width=self.wing,
                    hold_days=self.hold_days,
                    structure=self.structure,
                )
                if decision.enter and decision.legs and decision.lots > 0:
                    entry_credit = decision.net_credit - self._leg_spread_cost(decision.legs)
                    if entry_credit > 0:
                        open_trade = ShortVolTrade(
                            entry_day=bar.day,
                            underlying=self.underlying,
                            structure=self.structure,
                            lots=decision.lots,
                            entry_credit_points=entry_credit,
                            max_loss_points=self.wing - entry_credit,
                        )
                        open_legs = decision.legs
                        open_entry_idx = i
                    else:
                        result.skipped_reasons["credit below spread cost"] = (
                            result.skipped_reasons.get("credit below spread cost", 0) + 1
                        )
                else:
                    key = (decision.reason or "no decision")[:60]
                    result.skipped_reasons[key] = result.skipped_reasons.get(key, 0) + 1

            result.equity_curve.append((bar.day, equity + unrealized))

        return result


# ─── Parameter sweep + validation gates (REDESIGN §5) ────────────────────────

# The sweep grid. DSR and PBO are *selection-bias* statistics: they ask "given
# that I searched N configurations and kept the best, how much of that winner's
# performance is luck?" So the grid must be the honest set of configurations
# actually considered — padding it inflates the deflation, shrinking it hides
# the search. These are the parameters the live strategy exposes as env-tunable.
SWEEP_GRID: list[dict[str, float]] = [
    {"sd": sd, "min_vrp": vrp, "kelly_fraction": kf}
    for sd in (1.0, 1.25, 1.5)
    for vrp in (1.0, 2.0, 3.0)
    for kf in (0.15, 0.30, 0.50)
]


def run_sweep(
    bars: list[DailyBar],
    vix_by_day: dict[date, float],
    *,
    underlying: str = "NIFTY",
    starting_capital: float = 1_000_000.0,
    grid: list[dict[str, float]] | None = None,
) -> list[ShortVolBacktestResult]:
    """Backtest every parameter combination over the same window."""
    results: list[ShortVolBacktestResult] = []
    for params in (grid if grid is not None else SWEEP_GRID):
        strategy = ShortVolStrategy(
            sd=params.get("sd"),
            min_vrp=params.get("min_vrp"),
            kelly_fraction=params.get("kelly_fraction"),
        )
        bt = ShortVolBacktester(
            underlying=underlying,
            starting_capital=starting_capital,
            strategy=strategy,
        )
        results.append(bt.run(bars, vix_by_day))
    return results


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    import numpy as _np
    arr = _np.asarray(returns, float)
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return 0.0
    return float(arr.mean() / sd) * math.sqrt(TRADING_DAYS)


def evaluate_short_vol_gates(
    sweep: list[ShortVolBacktestResult],
    *,
    settings: object | None = None,
    strategy_id: str = "short_vol",
    backtest_id: str | None = None,
):
    """Run the §5 gates over a short-vol parameter sweep.

    The N sweep variants are the "trials" DSR needs and the columns of the
    T x N matrix PBO's CSCV needs. Monte-Carlo-DD and the cost model are
    evaluated on the WINNING variant (best Sharpe) — the one that would
    actually be promoted.
    """
    import numpy as _np

    from trading_platform.validation.gates import GateEvaluator

    evaluator = GateEvaluator(settings=settings)
    bt_id = backtest_id or f"shortvol-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    if not sweep:
        return evaluator.finalize(bt_id, strategy_id)

    # Align every variant's daily returns onto the shortest common length so
    # the CSCV matrix is rectangular (all variants share the same window, so
    # any difference is only a ragged tail).
    series = [r.daily_returns for r in sweep]
    width = min((len(s) for s in series), default=0)
    sharpes = [_sharpe(s) for s in series]
    best_idx = max(range(len(sweep)), key=lambda i: sharpes[i])
    best = sweep[best_idx]

    if width >= 30 and len(series) >= 2:
        matrix = _np.column_stack([_np.asarray(s[:width], float) for s in series])
        evaluator.evaluate_dsr(sharpes[best_idx], sharpes, matrix[:, best_idx])
        evaluator.evaluate_pbo(matrix)

    evaluator.evaluate_monte_carlo(
        [{"pnl": t.pnl} for t in best.trades],
        starting_capital=best.starting_capital,
    )
    net_pnl = best.final_equity - best.starting_capital
    evaluator.evaluate_cost_model(net_pnl, best.total_charges)

    results = evaluator.finalize(bt_id, strategy_id)
    evaluator.evaluate_promotion_ladder(results.all_passed)
    return evaluator.finalize(bt_id, strategy_id)
