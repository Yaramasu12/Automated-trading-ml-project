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
* **Model prices, not market prices.** Legs are priced with Black-Scholes off
  a single daily IV (India VIX for NIFTY; for other indices use their own
  chain-derived series from scripts/build_index_iv_history.py -- NSE's free
  bhavcopy makes per-index IV recoverable, so India VIX must NOT be fed to
  non-NIFTY indices). Real fills
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


# ─── Traded-leg quote book (Phase 4) ──────────────────────────────────────────

@dataclass
class TradedQuoteBook:
    """Point-in-time index of REAL traded EOD option quotes, for the
    ``leg_pricing='traded'`` backtest mode (Phase 4).

    Built once from ``eod_option_quote`` rows (via MarketDataStore / the DB) and
    passed into ShortVolBacktester. A leg is priced ONLY from its own
    ``trade_date`` row — the key includes trade_date, so there is no lookahead:
    asking for a price on day D can never return a quote from day D+1. Holds
    plain dicts so it pickles cheaply if a caller runs the sweep across
    processes.

    The traded mark is the official EOD SETTLEMENT price (``settle``) when
    present and positive, else the traded ``close`` — never the modeled
    implied_vol, which is a separate column and is never read here.
    """
    # (trade_date, underlying, expiry, strike_key, option_type) -> traded price
    prices: dict = field(default_factory=dict)
    # (underlying, trade_date) -> sorted tuple of expiries quoted that day
    expiries_by_day: dict = field(default_factory=dict)

    @staticmethod
    def _k(strike) -> float:
        # 23350 and 23350.0 must map to one key; keep a half-point strike distinct.
        return round(float(strike), 2)

    @classmethod
    def from_rows(cls, rows) -> "TradedQuoteBook":
        prices: dict = {}
        exps: dict = {}
        for r in rows:
            td, exp = r.get("trade_date"), r.get("expiry")
            strike, ot = r.get("strike"), (r.get("option_type") or "").upper()
            und = (r.get("underlying") or "").upper()
            if td is None or exp is None or strike is None or ot not in ("CE", "PE"):
                continue
            px = r.get("settle")
            if px is None or px <= 0:
                px = r.get("close")
            if px is None or px <= 0:
                continue
            prices[(td, und, exp, cls._k(strike), ot)] = float(px)
            exps.setdefault((und, td), set()).add(exp)
        return cls(prices=prices,
                   expiries_by_day={k: tuple(sorted(v)) for k, v in exps.items()})

    def expiries_on(self, underlying: str, trade_date: date) -> tuple:
        return self.expiries_by_day.get((underlying.upper(), trade_date), ())

    def price(self, trade_date: date, underlying: str, expiry: date,
              strike: float, option_type: str) -> float | None:
        return self.prices.get(
            (trade_date, underlying.upper(), expiry, self._k(strike),
             (option_type or "").upper()))

    def __len__(self) -> int:
        return len(self.prices)


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
    # audit_results/2026-09-13/AUDIT_REPORT.md finding #3 (high, second
    # half): gross per-leg premium at ENTRY, split by side — needed at exit
    # to compute real STT (charged on gross SELL-side premium turnover per
    # leg, not a structure's net credit) across the full round trip. See
    # _leg_turnovers() and _charges()'s own docstring.
    entry_sell_gross_points: float = 0.0
    entry_buy_gross_points: float = 0.0
    # Phase 4 (traded-leg mode). expiry = the real listed expiry this trade's
    # legs were priced against from eod_option_quote; None in model mode.
    # entry_modeled / exit_modeled are True when AT LEAST ONE leg had no traded
    # quote that day and fell back to a Black-Scholes model mark — so a reader
    # can see exactly which trades' credit/close rested on model fallback rather
    # than a real traded price. Always False in the default model mode.
    expiry: date | None = None
    entry_modeled: bool = False
    exit_modeled: bool = False

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
    # Phase 4: which pricing mode produced this result, and the traded-vs-modeled
    # leg coverage accounting (counted over every leg TRANSACTED — entry + exit —
    # not the daily mark-to-market). In model mode these stay at the defaults.
    leg_pricing: str = "model"
    traded_legs: int = 0
    modeled_legs: int = 0

    @property
    def traded_coverage(self) -> float:
        """Fraction of transacted legs (entry+exit) priced from a REAL traded
        quote rather than a Black-Scholes model fallback. 0.0 when no legs were
        priced (e.g. model mode, or no trades)."""
        total = self.traded_legs + self.modeled_legs
        return (self.traded_legs / total) if total else 0.0

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
        wing_pct: float | None = None,       # None = fixed INDEX_SPECS wing (default)
        skew_per_10pct: float = 0.25,        # smile steepness used to mark/close legs
        entry_filter=None,                   # optional callable(date) -> bool gating NEW entries
        leg_pricing: str = "model",          # 'model' (default, unchanged) | 'traded'
        quote_book: "TradedQuoteBook | None" = None,  # real traded quotes for 'traded' mode
    ) -> None:
        spec = INDEX_SPECS.get(underlying.upper(), INDEX_SPECS["NIFTY"])
        self.underlying = underlying.upper()
        self.lot_size = int(spec["lot_size"])
        self.strike_step = int(spec["strike_step"])
        self.wing = float(spec["wing"])
        # 2026-09-19: optional spot-scaled wing (matches the live executor's
        # SHORTVOL_WING_PCT=1.25% of spot, rounded to the strike step, never
        # narrower than two steps). The fixed default is a poor fit for an
        # index that doubles over the sample (BANKNIFTY 28k -> 56k): the same
        # 700-pt wing is a 2.5% wing in 2019 and 1.25% in 2026. None keeps the
        # original fixed-wing behaviour exactly.
        self.wing_pct = wing_pct
        # 2026-09-19: exposed so the smile can be stress-tested. NSE settlement
        # prices show NIFTY 5%-OTM puts at ~1.3x ATM vol at ~25 DTE (up to ~2x
        # at very short expiries) vs the 1.125x this default implies.
        self.skew_per_10pct = skew_per_10pct
        # 2026-09-19: optional regime gate on NEW entries only -- open
        # positions are always managed/exited regardless. Lets a slow
        # "is the vol premium rich right now?" filter be tested without
        # touching ShortVolStrategy.decide() (see
        # scripts/research_premium_regime_filter.py).
        self.entry_filter = entry_filter
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
        # Phase 4 — traded-leg pricing. 'traded' prices each leg from the real
        # eod_option_quote settle/close for (trade_date, underlying, expiry,
        # strike, CE/PE) via the quote_book, falling back to the model leg when
        # a quote is absent (labeled). 'model' (default) leaves every pricing
        # path below byte-identical to before Phase 4.
        self.leg_pricing = (leg_pricing or "model").lower()
        self._book = quote_book
        # The listed expiry the CURRENT open trade's legs are priced against
        # (traded mode only); set when a trade opens, cleared when it closes.
        self._cur_expiry: date | None = None

    def _traded_mode(self) -> bool:
        return self.leg_pricing == "traded" and self._book is not None

    def _choose_expiry(self, entry_day: date, hold_days: int) -> date | None:
        """Pick the real listed expiry a trade entered on `entry_day` and held
        ~`hold_days` TRADING days would actually be struck into: the quoted
        expiry (present in the book for this underlying on this day) nearest to
        the hold horizon in calendar terms (hold_days * 7/5). Prefers an expiry
        on/after entry_day. Returns None when the book has no quote for this
        underlying that day (caller then prices the whole trade as model)."""
        exps = self._book.expiries_on(self.underlying, entry_day)
        if not exps:
            return None
        target_cal = max(1, round(hold_days * 7 / 5))
        future = [e for e in exps if e >= entry_day]
        cands = future or list(exps)
        return min(cands, key=lambda e: abs((e - entry_day).days - target_cal))

    def _price_leg(self, trade_date: date, leg, spot: float, atm_iv: float,
                   t_years: float) -> tuple[float, bool]:
        """(price, was_traded) for one leg on `trade_date`. Traded mode: the
        real quote for the current trade's expiry/strike/type if present
        (positive), else the Black-Scholes model leg (was_traded=False). Model
        mode never calls this."""
        exp = self._cur_expiry
        if exp is not None:
            q = self._book.price(trade_date, self.underlying, exp, leg.strike,
                                 leg.option_type.value)
            if q is not None and q > 0:
                return q, True
        iv = smile_iv(atm_iv, spot, leg.strike, leg.option_type, self.skew_per_10pct)
        return black_scholes(spot, leg.strike, t_years, iv, leg.option_type), False

    def _price_structure(self, trade_date: date, legs, spot: float, atm_iv: float,
                         t_years: float) -> tuple[float, int, int]:
        """(signed_value, n_traded, n_modeled) for a structure in traded mode.

        signed_value = Σ(+price if the leg is SOLD else -price) — identical in
        meaning to `_structure_value` (cost to CLOSE / credit RECEIVED share the
        same per-leg sign), so the run()-loop arithmetic is unchanged; only the
        per-leg price source differs."""
        signed = 0.0
        nt = nm = 0
        for leg in legs:
            px, traded = self._price_leg(trade_date, leg, spot, atm_iv, t_years)
            signed += px if leg.side == Side.SELL else -px
            if traded:
                nt += 1
            else:
                nm += 1
        return signed, nt, nm

    def _wing_at(self, spot: float) -> float:
        if self.wing_pct is None:
            return self.wing
        step = self.strike_step
        return float(max(round((self.wing_pct * spot) / step) * step, 2 * step))

    # -- structure valuation ------------------------------------------------

    def _structure_value(self, legs, spot: float, atm_iv: float, t_years: float,
                         *, trade_date: date | None = None) -> float:
        """Cost to CLOSE the structure, in index points (positive = we pay).

        Short legs must be bought back, long legs sold — so this is
        sum(short marks) - sum(long marks), each priced at its own skew-
        adjusted IV.

        Phase 4: in traded mode the per-leg marks come from the real quote book
        (model fallback when a quote is missing). `trade_date` is ignored in the
        default model mode, so that path stays byte-identical.
        """
        if self._traded_mode():
            return self._price_structure(trade_date, legs, spot, atm_iv, t_years)[0]
        value = 0.0
        for leg in legs:
            iv = smile_iv(atm_iv, spot, leg.strike, leg.option_type, self.skew_per_10pct)
            px = black_scholes(spot, leg.strike, t_years, iv, leg.option_type)
            value += px if leg.side == Side.SELL else -px
        return value

    def _leg_spread_cost(self, legs) -> float:
        """Spread paid crossing every leg once, in points."""
        return self.spread_points * len(legs)

    def _leg_turnovers(self, legs, spot: float, atm_iv: float, t_years: float,
                       *, trade_date: date | None = None) -> tuple[float, float]:
        """Gross per-leg premium, in index points, split by the leg's own
        SELL/BUY side: (sell_side_gross, buy_side_gross).

        audit_results/2026-09-13/AUDIT_REPORT.md finding #3 (high, second
        half): a condor's real STT is charged on the GROSS premium of each
        SOLD leg individually — a 4-leg condor with 2 short legs and 2 long
        wings owes STT on the sum of the two short legs' own premiums, not
        on (short premiums minus long premiums). The old _charges() was
        handed the structure's NET credit and treated the whole thing as
        one sell-side turnover, understating STT whenever the wings' own
        premium was non-trivial relative to the shorts'. Mirrors
        _structure_value's per-leg pricing exactly, just keeping the two
        sides separate instead of netting them.

        Phase 4: traded mode draws each leg's gross premium from the real quote
        book (model fallback when absent); `trade_date` is ignored in model mode
        so STT/turnover accounting stays byte-identical there.
        """
        sell_gross = 0.0
        buy_gross = 0.0
        traded = self._traded_mode()
        for leg in legs:
            if traded:
                px, _ = self._price_leg(trade_date, leg, spot, atm_iv, t_years)
            else:
                iv = smile_iv(atm_iv, spot, leg.strike, leg.option_type, self.skew_per_10pct)
                px = black_scholes(spot, leg.strike, t_years, iv, leg.option_type)
            if leg.side == Side.SELL:
                sell_gross += px
            else:
                buy_gross += px
        return sell_gross, buy_gross

    # -- charges ------------------------------------------------------------

    def _charges(self, legs, lots: int, sell_turnover_points: float, buy_turnover_points: float) -> float:
        """Real Indian F&O option costs on GROSS premium turnover.

        Deliberately computed here rather than through `ChargesModel.estimate()`:
        that API takes an `OrderIntent` bound to a live `Instrument`, which this
        offline replay has no way to construct. The rates below mirror
        backtesting/charges.py's `_options_charges` — STT 0.15% on SELL-side
        premium (2026-04-01 rate; see charges.py's own citation — this offline
        replay always prices at the current rate, it has no as_of concept),
        exchange txn 0.05%, GST 18% on (brokerage + txn), SEBI 0.0001%, stamp
        0.003% on buy — keeping this consistent with the live cost model.

        `sell_turnover_points`/`buy_turnover_points` must already be the
        GROSS points across the full round trip (entry + exit), summed by
        which side each leg transaction actually was — see the callers in
        run(), which combine _leg_turnovers() at entry and at exit correctly
        (a leg opened by selling is closed by buying, and vice versa).
        """
        qty = lots * self.lot_size
        n_legs = max(1, len(legs))
        sell_turnover = abs(sell_turnover_points) * qty
        buy_turnover = abs(buy_turnover_points) * qty

        brokerage = min(20.0, 0.0003 * (sell_turnover + buy_turnover)) * n_legs * 2
        stt = 0.0015 * sell_turnover
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
        result.leg_pricing = self.leg_pricing
        self._cur_expiry = None   # no open trade yet (traded-mode leg pricing)
        # long_straddle/strangle (added 2026-09-08) BUY every leg — no credit
        # is ever received, so the whole rest of this method's P&L convention
        # ("credit received minus cost to close, capped loss = wing - credit")
        # doesn't apply. Branch explicitly rather than trying to force a debit
        # structure through credit-shaped arithmetic; getting the sign wrong
        # here would silently misprice every trade in the backtest, exactly
        # the kind of bug this codebase's own honesty discipline exists to
        # catch, not commit.
        is_long_vol = self.structure in ("long_straddle", "strangle")

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
                structure_value = self._structure_value(open_legs, bar.close, vix / 100.0, t_left,
                                                         trade_date=bar.day)
                spread = self._leg_spread_cost(open_legs)

                if is_long_vol:
                    # entry_credit_points holds the DEBIT PAID (positive) for
                    # a long-vol trade — see the entry branch below. Closing
                    # a BUY-only structure means SELLING it: _structure_value
                    # is negative for an all-long book (its own docstring:
                    # "positive = we pay" to close; buying legs back is what
                    # costs money, selling them is proceeds), so -structure_value
                    # is what we actually receive, minus the spread crossed.
                    debit = open_trade.entry_credit_points
                    proceeds = -structure_value - spread
                    pnl_points = proceeds - debit
                else:
                    close_cost = structure_value + spread
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
                if is_long_vol:
                    # No stop-loss branch: a long-only structure's max loss is
                    # already capped at the debit paid by construction (you
                    # cannot lose more than you spent) — an early stop would
                    # protect against nothing holding to expiry doesn't
                    # already bound, and "stop at N x debit" is meaningless
                    # once N >= 1.
                    if pnl_points >= self.profit_target_pct * debit:
                        reason = "profit_target"
                    elif dte_left <= 0:
                        reason = "expiry"
                else:
                    if pnl_points >= self.profit_target_pct * credit:
                        reason = "profit_target"
                    elif pnl_points <= -self.stop_loss_multiple * credit:
                        reason = "stop_loss"
                    elif dte_left <= 0:
                        reason = "expiry"

                if reason:
                    # Loss is bounded — a defined-risk structure cannot lose
                    # more than max_loss_points per lot (wing - credit for a
                    # short structure; the debit itself for a long-only one).
                    pnl_points = max(pnl_points, -open_trade.max_loss_points)
                    qty = open_trade.lots * self.lot_size
                    exit_points = proceeds if is_long_vol else close_cost
                    # Gross per-leg turnover, not the structure's net credit/
                    # debit (finding #3, second half — see _leg_turnovers'
                    # own docstring). A leg opened by SELLING is closed by
                    # BUYING it back and vice versa, so the round trip's
                    # total sell-side (STT-liable) turnover is entry's sell
                    # legs plus exit's buy-side closes, and symmetrically for
                    # buy-side (stamp-liable) turnover — this formula is the
                    # same for every structure, short or long-only, since it
                    # only ever reads each leg's own recorded side.
                    exit_sell_gross, exit_buy_gross = self._leg_turnovers(
                        open_legs, bar.close, vix / 100.0, t_left, trade_date=bar.day,
                    )
                    total_sell_turnover = open_trade.entry_sell_gross_points + exit_buy_gross
                    total_buy_turnover = open_trade.entry_buy_gross_points + exit_sell_gross
                    charges = self._charges(open_legs, open_trade.lots, total_sell_turnover, total_buy_turnover)
                    pnl = pnl_points * qty - charges
                    open_trade.exit_day = bar.day
                    open_trade.exit_debit_points = exit_points
                    open_trade.pnl = pnl
                    open_trade.charges = charges
                    open_trade.exit_reason = reason
                    # Phase 4: count the EXIT legs as traded-vs-modeled and label
                    # whether this close rested on any model-fallback leg.
                    if self._traded_mode():
                        _, exit_nt, exit_nm = self._price_structure(
                            bar.day, open_legs, bar.close, vix / 100.0, t_left)
                        result.traded_legs += exit_nt
                        result.modeled_legs += exit_nm
                        open_trade.exit_modeled = exit_nm > 0
                    equity += pnl
                    result.trades.append(open_trade)
                    open_trade, open_legs = None, None
                    self._cur_expiry = None
                    unrealized = 0.0

            # ---- consider a new entry ----------------------------------
            if (open_trade is None and bar.day.weekday() == self.entry_weekday
                    and (self.entry_filter is None or self.entry_filter(bar.day))):
                closes = [b.close for b in bars[max(0, i - 60): i + 1]]
                wing_now = self._wing_at(bar.close)
                decision = self.strategy.decide(
                    spot=bar.close,
                    vix=vix,
                    closes=closes,
                    capital=equity,
                    lot_size=self.lot_size,
                    strike_step=self.strike_step,
                    wing_width=wing_now,
                    hold_days=self.hold_days,
                    structure=self.structure,
                )
                if decision.enter and decision.legs and decision.lots > 0:
                    t_entry = self.hold_days / TRADING_DAYS
                    # Phase 4: traded mode prices the ENTRY credit/debit from the
                    # real quote book for the listed expiry nearest the hold
                    # horizon (model fallback per missing leg). The entry
                    # DECISION (strikes, lots, VRP gate) is unchanged — only the
                    # credit each leg is actually worth comes from traded prices.
                    chosen_expiry = None
                    entry_modeled = False
                    entry_nt = entry_nm = 0
                    if self._traded_mode():
                        chosen_expiry = self._choose_expiry(bar.day, self.hold_days)
                        self._cur_expiry = chosen_expiry
                        signed, entry_nt, entry_nm = self._price_structure(
                            bar.day, decision.legs, bar.close, vix / 100.0, t_entry)
                        entry_modeled = entry_nm > 0
                        if is_long_vol:
                            entry_amount = -signed + self._leg_spread_cost(decision.legs)
                        else:
                            entry_amount = signed - self._leg_spread_cost(decision.legs)
                    elif is_long_vol:
                        # decision.net_credit is negative-by-convention for a
                        # debit structure (see short_vol.py::decide()) — flip
                        # sign to get the actual amount paid, then add the
                        # spread crossed on entry same as the credit side does.
                        entry_amount = -decision.net_credit + self._leg_spread_cost(decision.legs)
                    else:
                        entry_amount = decision.net_credit - self._leg_spread_cost(decision.legs)
                    if entry_amount > 0:
                        entry_sell_gross, entry_buy_gross = self._leg_turnovers(
                            decision.legs, bar.close, vix / 100.0, t_entry, trade_date=bar.day,
                        )
                        open_trade = ShortVolTrade(
                            entry_day=bar.day,
                            underlying=self.underlying,
                            structure=self.structure,
                            lots=decision.lots,
                            # Dual-purpose field: the credit RECEIVED for a
                            # short structure, or the debit PAID for a long-
                            # only one — sign convention is always "positive
                            # = the reference amount for this trade", branched
                            # on `is_long_vol` everywhere it's read back above.
                            entry_credit_points=entry_amount,
                            max_loss_points=entry_amount if is_long_vol else wing_now - entry_amount,
                            entry_sell_gross_points=entry_sell_gross,
                            entry_buy_gross_points=entry_buy_gross,
                            expiry=chosen_expiry,
                            entry_modeled=entry_modeled,
                        )
                        open_legs = decision.legs
                        open_entry_idx = i
                        if self._traded_mode():
                            result.traded_legs += entry_nt
                            result.modeled_legs += entry_nm
                    else:
                        self._cur_expiry = None   # no trade opened; drop the tentative expiry
                        reason_key = "debit too small vs spread cost" if is_long_vol else "credit below spread cost"
                        result.skipped_reasons[reason_key] = result.skipped_reasons.get(reason_key, 0) + 1
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
    structure: str = "condor",
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
            structure=structure,
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

    from trading_platform.validation.cpcv import CombinatorialPurgedCrossValidator
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

        # §5 gate 2: CPCV (combinatorial purged CV with embargo). Section 17
        # audit (2026-09-17): this was never called anywhere in the real
        # pipeline (only in tests) — every recorded "PASS" for short_vol/
        # BANKNIFTY/FINNIFTY reflected only 4 of the 7 documented gates
        # (walk-forward and paper_days are legitimately evaluated
        # elsewhere/later — see IMPLEMENTATION_STATUS.md Section 17 — but
        # CPCV had no substitute anywhere). The DSR/PBO gates above use
        # simple contiguous-block CSCV splits with no purge/embargo, so a
        # trade whose holding period straddled a fold boundary could leak
        # information across the IS/OOS split undetected. Reuses the real
        # purge/embargo fold machinery already built and tested for ML-model
        # validation (CombinatorialPurgedCrossValidator), applied here to
        # the sweep's per-day returns panel instead of a classifier's
        # features/labels — a rule-based options strategy has no predictive
        # AUC to deflate, so this reports OOS Sharpe only (see
        # GateEvaluator.evaluate_cpcv).
        cpcv = CombinatorialPurgedCrossValidator()
        if cpcv.generate_folds(width):
            oos_sharpes = []
            for path in cpcv.get_combinatorial_paths(n_test_groups=2):
                built = cpcv.build_path(path, width)
                if len(built.train_indices) < cpcv.min_train_size or len(built.test_indices) < cpcv.min_test_size:
                    continue
                train_sharpes = [_sharpe(list(matrix[built.train_indices, j])) for j in range(matrix.shape[1])]
                best_train_variant = int(_np.argmax(train_sharpes))
                oos_sharpes.append(_sharpe(list(matrix[built.test_indices, best_train_variant])))
            if oos_sharpes:
                from types import SimpleNamespace
                evaluator.evaluate_cpcv(SimpleNamespace(out_of_sample_metrics={
                    "mean_oos_sharpe": float(_np.mean(oos_sharpes)),
                    "n_paths": len(oos_sharpes),
                    "n_folds": cpcv.n_folds,
                }))

    evaluator.evaluate_monte_carlo(
        [{"pnl": t.pnl} for t in best.trades],
        starting_capital=best.starting_capital,
    )
    net_pnl = best.final_equity - best.starting_capital
    evaluator.evaluate_cost_model(net_pnl, best.total_charges)

    results = evaluator.finalize(bt_id, strategy_id)
    evaluator.evaluate_promotion_ladder(results.all_passed)
    return evaluator.finalize(bt_id, strategy_id)
