"""Defined-risk short-volatility strategy — harvests the NIFTY volatility risk premium.

This is the first strategy in the platform built on a VALIDATED edge. Research
(scripts/research_vol_premium.py, backtest_short_vol.py) established:
  * India VIX (implied vol) exceeds subsequently-realized vol ~76% of the time —
    a real, structural volatility risk premium.
  * A weekly defined-risk iron condor, entered ONLY when the premium is genuinely
    rich (VIX - realized >= min_vrp), backtested at ~11% CAGR / Sharpe 2.8 /
    -11% max drawdown on 2024-2026 data (SD 1.25, 5% risk budget, VRP>=2).

Honesty caveats baked into the defaults:
  * DEFINED RISK ONLY — always an iron condor (long protective wings), never naked
    short options. One crash caps the loss at the wing width, it cannot blow up
    the account.
  * VRP FILTER — do not sell vol unless it is actually rich; selling always
    quartered the Sharpe in the backtest.
  * The backtest sample had NO major crash. Forward returns will be lower and a
    2020-style gap tests the tail. Size conservatively.

This module is pure logic (signal, strike spec, sizing) so it is unit-testable and
independent of the execution/multi-leg plumbing that consumes it.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np

from trading_platform.domain.enums import OptionType, Side


@dataclass(frozen=True)
class CondorLegSpec:
    """One leg of the iron condor (strike + type + buy/sell)."""
    option_type: OptionType
    strike: float
    side: Side           # SELL = short (collect premium), BUY = long (protection)
    is_wing: bool


@dataclass(frozen=True)
class ShortVolDecision:
    enter: bool
    reason: str
    vrp: float                       # VIX - realized vol, in vol points
    legs: tuple[CondorLegSpec, ...] = ()
    lots: int = 0
    net_credit: float = 0.0          # per-lot, index points
    max_loss: float = 0.0            # per-lot, index points


class ShortVolStrategy:
    """Weekly defined-risk short-vol on an index (NIFTY/BANKNIFTY).

    Config is env-tunable so the deployed risk posture can change without a code
    change. Defaults are the best risk-adjusted config from the sweep.
    """

    def __init__(
        self,
        *,
        sd: float | None = None,
        wing_width: float | None = None,
        risk_budget: float | None = None,
        min_vrp: float | None = None,
        strike_step: int = 50,
        hold_days: int = 5,
    ) -> None:
        self.sd = sd if sd is not None else float(os.getenv("SHORTVOL_SD", "1.25"))
        self.wing_width = wing_width if wing_width is not None else float(os.getenv("SHORTVOL_WING", "300"))
        self.risk_budget = risk_budget if risk_budget is not None else float(os.getenv("SHORTVOL_RISK", "0.05"))
        self.min_vrp = min_vrp if min_vrp is not None else float(os.getenv("SHORTVOL_MIN_VRP", "2.0"))
        self.strike_step = strike_step
        self.hold_days = hold_days

    # ── signal ────────────────────────────────────────────────────────────────

    @staticmethod
    def realized_vol(closes: list[float] | np.ndarray, window: int = 20) -> float:
        """Annualized realized vol (%) from the last `window` daily closes."""
        c = np.asarray(closes, float)
        if len(c) < window + 1:
            return 0.0
        logret = np.diff(np.log(c[-(window + 1):]))
        return float(logret.std() * math.sqrt(252) * 100.0)

    def expected_realized(self, closes: list[float] | np.ndarray, forecast_vol: float | None = None) -> float:
        """Best estimate of the volatility that WILL be realized over the hold.

        VRP is implied vol minus *future* realized vol. Trailing 20-day realized
        is only a proxy; when a validated forward forecast is supplied (e.g. GARCH
        conditional vol, which captures mean-reversion), use it instead — this is
        the correct reference for the premium and sharpens every entry."""
        if forecast_vol is not None and forecast_vol > 0:
            return float(forecast_vol)
        return self.realized_vol(closes)

    def vrp(self, vix: float, closes: list[float] | np.ndarray, forecast_vol: float | None = None) -> float:
        """Volatility risk premium in vol points: implied (VIX) minus expected realized."""
        return float(vix) - self.expected_realized(closes, forecast_vol)

    # ── construction + sizing ──────────────────────────────────────────────────

    def _bs(self, S: float, K: float, T: float, sig: float, call: bool, r: float = 0.065) -> float:
        if T <= 0 or sig <= 0:
            return max(0.0, (S - K) if call else (K - S))
        d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
        d2 = d1 - sig * math.sqrt(T)
        nd = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
        if call:
            return S * nd(d1) - K * math.exp(-r * T) * nd(d2)
        return K * math.exp(-r * T) * nd(-d2) - S * nd(-d1)

    def decide(
        self,
        *,
        spot: float,
        vix: float,
        closes: list[float] | np.ndarray,
        capital: float,
        lot_size: int,
        strike_step: int | None = None,
        wing_width: float | None = None,
        forecast_vol: float | None = None,
    ) -> ShortVolDecision:
        """The full entry decision: signal → strikes → sizing. Pure/deterministic.

        `vix` is the underlying's own implied vol in vol-points (%) — for NIFTY
        this is India VIX; for other indices it must be that index's ATM IV, NOT
        India VIX (which would miscompute VRP). `strike_step`/`wing_width` let the
        caller pass the index's real strike spacing and a price-scaled wing so the
        same logic works across NIFTY/BANKNIFTY/SENSEX etc.; both fall back to the
        NIFTY-tuned defaults when omitted."""
        vrp = self.vrp(vix, closes, forecast_vol)
        if spot <= 0 or vix <= 0:
            return ShortVolDecision(False, "no spot/vix", vrp)
        if vrp < self.min_vrp:
            return ShortVolDecision(False, f"vrp {vrp:.1f} < min {self.min_vrp:.1f} (premium not rich)", vrp)

        iv = vix / 100.0
        T = self.hold_days / 252.0
        move = spot * iv * math.sqrt(T)                      # 1-SD expected move
        step = int(strike_step) if strike_step else self.strike_step
        wing = float(wing_width) if wing_width else self.wing_width
        call_short = round((spot + self.sd * move) / step) * step
        put_short = round((spot - self.sd * move) / step) * step
        call_wing = call_short + wing
        put_wing = put_short - wing

        # per-lot credit (index points) from BS at the current IV
        credit = (
            self._bs(spot, call_short, T, iv, True) - self._bs(spot, call_wing, T, iv, True)
            + self._bs(spot, put_short, T, iv, False) - self._bs(spot, put_wing, T, iv, False)
        )
        max_loss = wing - credit
        if credit <= 0 or max_loss <= 0:
            return ShortVolDecision(False, "no net credit / non-positive risk", vrp)

        lots = int((capital * self.risk_budget) / (max_loss * lot_size))
        if lots < 1:
            return ShortVolDecision(False, "risk budget too small for one lot", vrp)

        legs = (
            CondorLegSpec(OptionType.CE, float(call_short), Side.SELL, False),
            CondorLegSpec(OptionType.CE, float(call_wing), Side.BUY, True),
            CondorLegSpec(OptionType.PE, float(put_short), Side.SELL, False),
            CondorLegSpec(OptionType.PE, float(put_wing), Side.BUY, True),
        )
        return ShortVolDecision(
            True,
            f"VRP {vrp:.1f}>={self.min_vrp:.1f}; iron condor {put_wing:.0f}/{put_short:.0f}-"
            f"{call_short:.0f}/{call_wing:.0f} x{lots}",
            vrp, legs=legs, lots=lots, net_credit=round(credit, 2), max_loss=round(max_loss, 2),
        )
