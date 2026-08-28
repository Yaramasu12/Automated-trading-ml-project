"""ShortVolExecutor — ties the validated short-vol strategy to live market data
and the multi-leg execution path.

Two entry points:
  * preview()  — gather spot/VIX/closes, run the decision, resolve the real option
    contracts and BS prices. Returns a full picture WITHOUT placing any order.
    Safe to call anytime; used to verify the strategy before enabling live entry.
  * enter()    — same, then submit the iron condor as a multi-leg order.

Kept separate from the runtime god-object and pure-ish (only reads runtime data)
so it is testable and the preview path can be deployed and observed before any
real order flows.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import date, datetime, timedelta
from typing import Any

from trading_platform.derivatives.engine import (
    MIN_IV_RANK_OBSERVATIONS,
    ImpliedVolatilityCalculator,
    compute_iv_rank,
)
from trading_platform.domain.enums import OptionType, Segment, Side
from trading_platform.neural.vol_forecaster import VolatilityForecaster
from trading_platform.strategies.short_vol import ShortVolStrategy

logger = logging.getLogger(__name__)

# India VIX candle token (verified fetchable 2026-07-16). India VIX IS NIFTY's
# implied-vol index, so it is only used as the IV source/fallback for NIFTY;
# every other index uses its OWN ATM implied vol (see _atm_iv_and_lot).
_INDIA_VIX_TOKEN = "99926017"


def _env_flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# REDESIGN §5 promotion ladder. This id is what short-vol's gate results and
# promotion record are keyed under — keep it stable, it's a DB key.
SHORT_VOL_STRATEGY_ID = "short_vol"
# Rungs on which auto-entry is permitted. "paper" is included because paper
# trading IS the point of the rung; research/shadow/disabled are not.
_SHORTVOL_TRADING_STATUSES = {"paper", "live_canary", "live_approved"}


class ShortVolExecutor:
    def __init__(self, runtime: Any, strategy: ShortVolStrategy | None = None) -> None:
        self._rt = runtime
        self.strategy = strategy or ShortVolStrategy()
        self._vol_forecaster = VolatilityForecaster()
        # Per-day cache of option last-prices keyed by (symbol, date). The Angel One
        # candle API is aggressively rate-limited; caching means each ATM contract is
        # fetched at most once per day (shared by previews and the daily auto-entry),
        # so we don't re-hammer it and trip "exceeding access rate".
        self._price_cache: dict[tuple[str, date], float] = {}
        # Short-TTL negative cache: a contract whose fetch just tripped the rate
        # limit is skipped for SHORTVOL_FETCH_NEG_TTL seconds so one throttled
        # underlying (e.g. BANKNIFTY) doesn't burn the whole scan re-hammering the
        # same denied strike — but it DOES retry on a later scan cycle.
        self._neg_cache: dict[tuple[str, date], float] = {}

    # ── market data ─────────────────────────────────────────────────────────

    def _closes_and_spot(self, underlying: str) -> tuple[list[float], float]:
        """Recent daily closes + current spot (live tick preferred)."""
        bars = self._rt.decision_pipeline._fetch_bars(underlying, date.today() - timedelta(days=60), 40)
        closes = [b.close for b in bars] if bars else []
        spot = 0.0
        try:
            tick = self._rt.live_feed.latest_tick(underlying)
            if tick and getattr(tick, "last_price", 0) and tick.last_price > 0:
                spot = float(tick.last_price)
        except Exception:
            pass
        if spot <= 0 and closes:
            spot = float(closes[-1])
        return closes, spot

    def _current_vix(self) -> float:
        """Latest India VIX value (implied vol, %).

        Prefers a live tick via PriceResolutionService — the same source
        every other real-time price in the platform now goes through — over
        the historical-candle API. INDIAVIX isn't part of the default scan
        universe, so it's registered + subscribed to the live feed lazily
        here; both calls are idempotent, safe to make on every invocation.
        """
        try:
            import dataclasses
            from trading_platform.domain.enums import Exchange
            nifty = self._rt.instrument_master.get("NIFTY")
            inst = dataclasses.replace(nifty, symbol="INDIAVIX", token=_INDIA_VIX_TOKEN, exchange=Exchange("NSE"))
            try:
                self._rt.live_feed.register_instruments([inst])
                self._rt.live_feed.add_subscriptions(["INDIAVIX"])
            except Exception as exc:
                logger.warning("short-vol: could not subscribe INDIAVIX to live feed: %s", exc)
            resolution = self._rt.price_service.resolve("INDIAVIX", instrument=inst)
            if resolution.price is not None and resolution.price > 0:
                return float(resolution.price)
            to_dt = datetime.now(); from_dt = to_dt - timedelta(days=10)
            bars = self._rt.angel_one_history.get_candles(inst, from_dt, to_dt, "ONE_DAY")
            if bars:
                return float(bars[-1].close)
        except Exception as exc:
            logger.warning("short-vol: VIX fetch failed: %s", exc)
        return 0.0

    def _iv_rank_history(self, underlying: str, lookback_days: int = 365) -> list[float]:
        """Trailing IV series (vol points, same units as _atm_iv_and_lot's
        output) for IV-rank/percentile — see compute_iv_rank in
        derivatives.engine. NIFTY uses India VIX's own long, candle-fetchable
        published history; every other index falls back to whatever ATM IV
        history OptionsChainCollector has accumulated from its own EOD
        snapshots. That's likely far short of MIN_IV_RANK_OBSERVATIONS for a
        long while for non-NIFTY underlyings — the correct, honest outcome
        (compute_iv_rank declines rather than fabricate a rank) until enough
        real history exists, not a bug to work around here."""
        if underlying.strip().upper() == "NIFTY":
            try:
                import dataclasses
                from trading_platform.domain.enums import Exchange
                nifty = self._rt.instrument_master.get("NIFTY")
                inst = dataclasses.replace(nifty, symbol="INDIAVIX", token=_INDIA_VIX_TOKEN, exchange=Exchange("NSE"))
                to_dt = datetime.now(); from_dt = to_dt - timedelta(days=lookback_days)
                bars = self._rt.angel_one_history.get_candles(inst, from_dt, to_dt, "ONE_DAY")
                return [float(b.close) for b in bars]
            except Exception as exc:
                logger.warning("short-vol: VIX history fetch failed for IV rank: %s", exc)
                return []
        try:
            collector = getattr(self._rt, "options_chain_collector", None)
            if collector is None:
                return []
            return collector.atm_iv_history(underlying, lookback_days=lookback_days)
        except Exception as exc:
            logger.warning("short-vol: chain-history IV read failed for %s: %s", underlying, exc)
            return []

    def _goal_scaling_factor(self) -> tuple[float, str]:
        """GoalGovernance's current phase-based scaling factor for live
        position sizing (REDESIGN_PROMPT.md §9) — reduces (or, in HALTED,
        zeroes) new entries' lot count when annual-return progress is
        lagging/at-risk. Deliberately separate from, and layered ON TOP OF,
        RiskEngine/CapitalProtection's own hard drawdown limits: those stay
        authoritative regardless of this — GoalGovernance checks drawdown
        independently and returns HALTED for its OWN reason (protecting the
        annual-target trajectory), not as a substitute risk gate.

        Uses ONLY the phase-based factor, not PositionScaler.scale()'s
        additional volatility-Kelly adjustment — ShortVolStrategy.kelly_lots()
        already does its own vol-aware Kelly sizing, so layering
        PositionScaler's own vol scaling on top would double-count that.

        Returns (factor, phase_name); (1.0, "unavailable") if governance
        isn't wired (e.g. a bare test executor) or is disabled via
        ENABLE_GOAL_GOVERNOR=false.
        """
        if not _env_flag("ENABLE_GOAL_GOVERNOR", True):
            return 1.0, "disabled"
        governance = getattr(self._rt, "goal_governance", None)
        portfolio = getattr(self._rt, "portfolio", None)
        if governance is None or portfolio is None:
            return 1.0, "unavailable"
        try:
            snapshot = portfolio.mark_to_market(datetime.now(), {})
            state = governance.evaluate(current_equity=snapshot.equity, drawdown=snapshot.drawdown)
            return state.scaling_factor, state.phase.value
        except Exception as exc:
            logger.warning("short-vol: goal governance evaluation failed: %s", exc)
            return 1.0, "error"

    def _resolve_option(self, underlying: str, strike: float, option_type, expiry: date):
        """Find the real option Instrument for a strike/type on the nearest expiry."""
        opts = [
            i for i in self._rt.instrument_master.by_underlying(underlying, Segment.OPTIONS)
            if i.expiry == expiry and i.option_type == option_type
        ]
        if not opts:
            return None
        return min(opts, key=lambda i: abs((i.strike or 0) - strike))

    # ── per-underlying market structure (works for any index) ────────────────

    def _infer_strike_step(self, underlying: str, expiry: date) -> int:
        """Smallest gap between adjacent listed strikes = the index's strike step
        (NIFTY 50, BANKNIFTY/SENSEX 100, …). Inferred from the live chain so it is
        correct for any underlying without a hardcoded table."""
        strikes = sorted({
            i.strike for i in self._rt.instrument_master.by_underlying(underlying, Segment.OPTIONS)
            if i.expiry == expiry and i.option_type == OptionType.CE and i.strike
        })
        gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
        return int(min(gaps)) if gaps else 50

    def _option_last_price(self, inst) -> float:
        """Most recent price of an option contract for entry/exit decisions.

        Goes through PriceResolutionService (live tick -> theoretical
        Black-Scholes mark) before ever touching the historical daily-candle
        API below. Confirmed 2026-08-05: this method used to go straight to
        the candle API, which is rate-limited and returns nothing before
        today's candle exists (e.g. pre-market), while live_feed already had
        the real price the whole time. That silently blinded
        evaluate_exit()/close_structure() exactly when a stop-loss had been
        breached — two disconnected price sources for what should be one
        "current price" concept. Falls through to the candle API unchanged
        when PriceResolutionService has neither a live tick nor a model mark.

        The candle fetch itself does one call to angel_one_history.get_candles(),
        which already serialises AND retries/backs off on rate limits via its
        own process-wide class-level throttle (see AngelOneHistoricalDataProvider
        — shared by every caller: decision pipeline, options-chain collector,
        this method). This used to duplicate that with a second private
        throttle + retry loop; confirmed 2026-08-06 that was pure redundant
        latency (up to 3x4 stacked retries with drifted-apart backoff
        constants) on top of an already-centralized rate limiter, not
        additional safety — removed. What genuinely IS specific to this
        method and worth keeping: the per-day price cache and the short-TTL
        negative cache below, since get_candles() itself caches nothing
        across calls.
        """
        sym = getattr(inst, "symbol", "?")
        resolution = self._rt.price_service.resolve(sym, instrument=inst)
        if resolution.price is not None and resolution.price > 0:
            return float(resolution.price)

        key = (sym, date.today())
        cached = self._price_cache.get(key)
        if cached is not None:
            return cached
        neg_ttl = float(os.getenv("SHORTVOL_FETCH_NEG_TTL", "20"))
        failed_at = self._neg_cache.get(key)
        if failed_at is not None and (time.monotonic() - failed_at) < neg_ttl:
            return 0.0   # recently rate-limited; skip until it ages out (retries next scan)
        to_dt = datetime.now(); from_dt = to_dt - timedelta(days=10)
        try:
            bars = self._rt.angel_one_history.get_candles(inst, from_dt, to_dt, "ONE_DAY")
        except Exception as exc:
            if "rate" in str(exc).lower():
                self._neg_cache[key] = time.monotonic()
            logger.warning("short-vol: option price fetch failed for %s: %s", sym, exc)
            return 0.0
        if not bars:
            return 0.0
        price = float(bars[-1].close)
        self._price_cache[key] = price
        self._neg_cache.pop(key, None)
        return price

    def _atm_iv_and_lot(self, underlying: str, spot: float, expiry: date) -> tuple[float, int]:
        """The underlying's OWN implied vol (%) from its ATM call+put market prices
        (Black-Scholes inversion), plus the option lot size. Returns (0.0, lot) if
        prices can't be recovered — the caller then declines to trade rather than
        guess. This is what makes multi-index correct: each index is priced off its
        own vol surface, never India VIX."""
        calc = ImpliedVolatilityCalculator()
        dte = max((expiry - date.today()).days, 1)
        ivs: list[float] = []
        lot: int | None = None
        for ot in (OptionType.CE, OptionType.PE):
            inst = self._resolve_option(underlying, spot, ot, expiry)   # ATM = nearest to spot
            if inst is None:
                continue
            lot = int(getattr(inst, "lot_size", 0) or 0) or lot
            price = self._option_last_price(inst)
            if price <= 0 or not inst.strike:
                continue
            try:
                iv = calc.calculate(price, spot, float(inst.strike), dte, ot)   # annualised fraction
                if 0.01 < iv < 3.0:
                    ivs.append(iv * 100.0)
            except Exception as exc:
                logger.warning("short-vol: IV inversion failed for %s: %s", inst.symbol, exc)
        iv_pct = sum(ivs) / len(ivs) if ivs else 0.0
        return iv_pct, int(lot or 50)

    def _wing_width(self, spot: float, step: int) -> float:
        """Protective-wing width scaled to the index price level (default 1.25% of
        spot, matching the NIFTY 300pt/24000 tuning), rounded to the strike step and
        never narrower than two steps. Keeps the condor's risk profile comparable
        across NIFTY (~300), BANKNIFTY (~640), SENSEX (~1025)."""
        pct = float(os.getenv("SHORTVOL_WING_PCT", "0.0125"))
        w = round((pct * spot) / step) * step
        return float(max(w, 2 * step))

    # ── decision + leg resolution ───────────────────────────────────────────

    def build(self, underlying: str = "NIFTY", expiry: date | None = None,
              structure: str = "condor") -> dict:
        """Compute the full short-vol decision with resolved contracts & prices.
        Does NOT execute. Returns a dict describing what WOULD be traded.

        Prices every input off the underlying's OWN option chain — its ATM implied
        vol, strike step, price-scaled wing, and option lot size — so the exact
        same path is correct for NIFTY and every other index. `expiry` selects the
        target expiry (multi-expiry); defaults to the nearest."""
        closes, spot = self._closes_and_spot(underlying)
        capital = float(getattr(self._rt.portfolio, "equity", 0) or self._rt.portfolio.cash)
        base = {
            "underlying": underlying, "spot": round(spot, 2),
            "realized_vol": round(self.strategy.realized_vol(closes), 2),
            "enter": False, "lots": 0, "net_credit_pts": 0.0, "max_loss_pts": 0.0, "legs": [],
        }
        # Fail closed: _closes_and_spot() -> DecisionPipeline._fetch_bars() may
        # have silently substituted fabricated daily bars for real Angel One
        # candles (rate-limit, outage, etc). realized_vol/VRP computed off
        # fake history is not a real signal — refuse rather than trade on it.
        # Mirrors the SHORTVOL_REQUIRE_REAL_QUOTES guard below, one layer
        # earlier: that one protects the fill PRICE, this protects the
        # decision INPUT. getattr-guarded: real DecisionPipeline instances
        # always have this (see pipeline.py __init__); lightweight test
        # doubles that predate this check default to "not synthetic".
        bars_were_synthetic = getattr(self._rt.decision_pipeline, "bars_were_synthetic", None)
        if bars_were_synthetic is not None and bars_were_synthetic(underlying):
            return {**base, "vix": 0.0, "vrp": 0.0,
                    "reason": f"SYNTHETIC-DATA FALLBACK for {underlying} this scan — "
                              f"refusing to enter on fabricated history"}
        if spot <= 0:
            return {**base, "vix": 0.0, "vrp": 0.0, "reason": "no spot price"}

        if expiry is None:
            expiry = self._rt.instrument_master.nearest_expiry(underlying, date.today(), segment=Segment.OPTIONS)
        if expiry is None:
            return {**base, "vix": 0.0, "vrp": 0.0, "reason": f"no listed options/expiry for {underlying}"}
        dte = max((expiry - date.today()).days, 1)

        step = self._infer_strike_step(underlying, expiry)
        iv, lot_size = self._atm_iv_and_lot(underlying, spot, expiry)
        # India VIX is a valid IV source only for NIFTY; other indices must use
        # their own ATM IV and are declined if it can't be recovered.
        if iv <= 0 and underlying.upper() == "NIFTY":
            iv = self._current_vix()
        if iv <= 0:
            return {**base, "vix": 0.0, "vrp": 0.0,
                    "reason": f"could not compute {underlying} implied vol (illiquid ATM?)"}
        wing = self._wing_width(spot, step)
        iv_rank_result = compute_iv_rank(iv, self._iv_rank_history(underlying))

        # GARCH volatility forecast for the VRP reference (implied vs *forecast*
        # realized, not trailing realized). Opt-in via SHORTVOL_USE_VOL_FORECAST;
        # falls back to trailing realized when disabled or the fit is degenerate.
        forecast_vol = None
        if _env_flag("SHORTVOL_USE_VOL_FORECAST", False):
            fv = self._vol_forecaster.forecast_pct(closes, underlying)
            forecast_vol = fv if fv > 0 else None

        decision = self.strategy.decide(
            spot=spot, vix=iv, closes=closes, capital=capital, lot_size=lot_size,
            strike_step=step, wing_width=wing, forecast_vol=forecast_vol, hold_days=dte,
            structure=structure,
            # "Delta bands" strike selection (REDESIGN_PROMPT.md §4.2) — an
            # opt-in alternative to the default sd-multiple strikes. Still
            # fully defined-risk either way; see decide()'s own docstring.
            strike_mode=os.getenv("SHORTVOL_STRIKE_MODE", "sd_multiple"),
            target_delta=float(os.getenv("SHORTVOL_TARGET_DELTA", "0.16")),
        )
        out: dict = {
            **base, "vix": round(iv, 2), "vrp": round(decision.vrp, 2),
            "structure": structure, "expiry": expiry.isoformat(), "dte": dte,
            "forecast_vol": round(forecast_vol, 2) if forecast_vol else None,
            "vol_reference": "garch_forecast" if forecast_vol else "trailing_realized",
            # Diagnostic only by default — see the SHORTVOL_MIN_IV_RANK gate
            # below for the opt-in secondary confirm. None here means either
            # not enough IV history yet (non-NIFTY underlyings especially —
            # see _iv_rank_history) or a genuinely flat lookback range.
            "iv_rank": iv_rank_result.rank if iv_rank_result else None,
            "iv_percentile": iv_rank_result.percentile if iv_rank_result else None,
            "iv_rank_lookback_n": iv_rank_result.lookback_n if iv_rank_result else 0,
            "enter": decision.enter, "reason": decision.reason, "lots": decision.lots,
            "net_credit_pts": decision.net_credit, "max_loss_pts": decision.max_loss,
        }
        if not decision.enter:
            return out

        # Opt-in secondary confirm (REDESIGN_PROMPT.md §4.2): VRP-rich alone
        # (checked above, inside self.strategy.decide) is the validated,
        # backtested entry signal and stays the default with this at 0/off —
        # "models must earn deployment" means a NEW gate doesn't get to
        # silently start rejecting trades on an already-profitable strategy.
        # An operator who explicitly sets this expects it enforced, so
        # insufficient history fails closed (declines) rather than skipping
        # the check it was asked to apply.
        min_iv_rank = float(os.getenv("SHORTVOL_MIN_IV_RANK", "0"))
        if min_iv_rank > 0:
            if iv_rank_result is None:
                out["enter"] = False
                out["reason"] = (
                    f"SHORTVOL_MIN_IV_RANK={min_iv_rank:.0f} set but insufficient IV history for "
                    f"{underlying} ({MIN_IV_RANK_OBSERVATIONS}+ observations needed) — declining "
                    f"rather than trade an unvalidated signal"
                )
                return out
            if iv_rank_result.rank < min_iv_rank:
                out["enter"] = False
                out["reason"] = (
                    f"IV rank {iv_rank_result.rank:.0f} < required {min_iv_rank:.0f} "
                    f"(VRP alone was rich enough, but IV isn't historically elevated)"
                )
                return out

        # Goal governance (REDESIGN_PROMPT.md §9): scales lots down (or to
        # zero) when annual-return progress is lagging/at-risk, layered on
        # top of — never a substitute for — RiskEngine/CapitalProtection's
        # own hard drawdown limits. See _goal_scaling_factor's docstring.
        goal_scale, goal_phase = self._goal_scaling_factor()
        out["goal_phase"] = goal_phase
        out["goal_scaling_factor"] = round(goal_scale, 2)
        lots = decision.lots
        if goal_scale < 1.0:
            lots = math.floor(decision.lots * goal_scale)
            if lots < 1:
                out["enter"] = False
                out["lots"] = 0
                out["reason"] = (
                    f"goal governance phase={goal_phase} (scaling={goal_scale:.2f}) "
                    f"reduced {decision.lots} lot(s) to 0"
                )
                return out
            out["reason"] = f"{decision.reason} [goal governance {goal_phase}: lots {decision.lots}->{lots}]"
        out["lots"] = lots

        vix = iv
        # Quantity is in LOTS (contracts). notional_value and the ledger already
        # multiply by lot_size, so passing lots*lot_size here double-counts the
        # multiplier — inflating option notional ~lot_size× (→ position_size
        # rejections that roll back the whole structure) and P&L on fill.
        # `lots` (not decision.lots) — already goal-governance-scaled above.
        qty = lots
        for leg in decision.legs:
            inst = self._resolve_option(underlying, leg.strike, leg.option_type, expiry)
            if inst is None:
                out["enter"] = False
                out["reason"] = f"could not resolve {leg.option_type.value} {leg.strike:.0f} @ {expiry}"
                out["legs"] = []
                return out
            # REAL QUOTE FIRST, theoretical only as a labelled fallback.
            #
            # This used to price EVERY leg with Black-Scholes unconditionally,
            # so entries were decided — and paper fills recorded — at
            # theoretical premiums that never existed in the market. The
            # giveaway was fill prices like 12.73664836769972 on an instrument
            # whose tick size is 0.05. P&L computed from those is model-vs-model
            # and tells you nothing about real edge or execution.
            #
            # `_option_last_price` resolves a genuine price (live tick, else the
            # Angel One candle close). If none is available we do NOT silently
            # substitute a model number: SHORTVOL_REQUIRE_REAL_QUOTES (default
            # on) refuses the entry instead. Trading on fabricated prices is
            # exactly the "fake edge" this codebase exists to prevent — and a
            # refusal is recoverable, a fabricated fill is not.
            T = dte / 252.0
            theo = self.strategy._bs(spot, float(inst.strike), T, vix / 100.0,
                                     call=(leg.option_type.value == "CE"))
            market = self._option_last_price(inst)
            if market and market > 0:
                premium = market
                priced_from = "market"
            elif _env_flag("SHORTVOL_REQUIRE_REAL_QUOTES", True):
                out["enter"] = False
                out["reason"] = (
                    f"no real quote for {inst.symbol} — refusing to enter on a "
                    f"theoretical price (set SHORTVOL_REQUIRE_REAL_QUOTES=false to override)"
                )
                out["legs"] = []
                return out
            else:
                premium = theo
                priced_from = "theoretical"
            out["legs"].append({
                "symbol": inst.symbol, "strike": float(inst.strike),
                "option_type": leg.option_type.value, "side": leg.side.value,
                "is_wing": leg.is_wing, "price": round(float(premium), 2), "quantity": qty,
                "priced_from": priced_from, "theoretical": round(float(theo), 2),
            })
        out["expiry"] = expiry.isoformat()

        # SAFETY: defined-risk structures need the wing strictly beyond the short
        # (real protection). A too-narrow chain snaps short and wing to the same
        # strike, collapsing into unprotected/zero-width risk — never execute that.
        by = {(l["option_type"], l["side"]): l["strike"] for l in out["legs"]}
        cs, cw = by.get(("CE", "SELL")), by.get(("CE", "BUY"))
        ps, pw = by.get(("PE", "SELL")), by.get(("PE", "BUY"))
        put_ok = ps is not None and pw is not None and pw < ps    # put wing below short
        call_ok = cs is not None and cw is not None and cw > cs   # call wing above short
        if structure == "put_spread":
            ok, detail = put_ok, f"(PE {ps}/{pw})"
        elif structure == "call_spread":
            ok, detail = call_ok, f"(CE {cs}/{cw})"
        else:  # condor needs both
            ok, detail = (put_ok and call_ok), f"(CE {cs}/{cw}, PE {ps}/{pw})"
        if not ok:
            out["enter"] = False
            out["reason"] = (
                f"option chain too narrow at {underlying} {expiry}: {structure} legs "
                f"collapsed {detail} — need wider strikes"
            )
            out["legs"] = []
        return out

    def preview(self, underlying: str = "NIFTY", structure: str = "condor") -> dict:
        return {"mode": "preview", **self.build(underlying, structure=structure)}

    def validate_vol_forecast(self, underlying: str = "NIFTY", days: int = 1000) -> dict:
        """Walk-forward test on REAL history: does GARCH predict future realized
        vol better than the trailing-realized baseline? Only if this says
        beats_naive=True should SHORTVOL_USE_VOL_FORECAST be enabled — the same
        'earn deployment' rule the rest of the platform follows."""
        bars = self._rt.decision_pipeline._fetch_bars(
            underlying, date.today() - timedelta(days=days), 400)
        closes = [b.close for b in bars] if bars else []
        v = self._vol_forecaster.validate_beats_naive(closes)
        return {
            "underlying": underlying, "n_bars": len(closes), "samples": v.n,
            "naive_rmse": round(v.naive_rmse, 3) if v.naive_rmse != float("inf") else None,
            "garch_rmse": round(v.garch_rmse, 3) if v.garch_rmse != float("inf") else None,
            "beats_naive": v.beats_naive,
            "verdict": ("GARCH earns deployment" if v.beats_naive
                        else "keep trailing-realized (GARCH did not beat baseline)"),
        }

    async def enter(self, underlying: str = "NIFTY", expiry: date | None = None,
                    structure: str = "condor") -> dict:
        """Build then SUBMIT the defined-risk structure as a multi-leg order."""
        # build() does blocking candle reads with throttle/backoff sleeps; run it
        # off the event loop so a rate-limited fetch can't stall the agent scan.
        plan = await asyncio.to_thread(self.build, underlying, expiry, structure)
        if not plan.get("enter") or not plan.get("legs"):
            return {"submitted": False, **plan}
        exp = plan.get("expiry", "")
        group_id = f"{structure}_{underlying}_{exp}"
        legs_payload = [
            {
                "symbol": l["symbol"], "side": l["side"], "price": l["price"],
                "quantity": l["quantity"], "strategy_name": f"short_vol_{structure}",
                "product_type": "CARRYFORWARD",   # held for days, not squared off intraday
                "metadata": {"is_wing": l["is_wing"], "vrp": plan["vrp"],
                             "expiry": exp, "structure": structure},
            }
            for l in plan["legs"]
        ]
        result = await self._rt.submit_multi_leg({
            "legs": legs_payload, "strategy_name": f"short_vol_{structure}", "group_id": group_id,
        })
        return {"submitted": True, "plan": plan, "execution": result}

    # ── weekly auto-entry ─────────────────────────────────────────────────────

    def target_expiries(self, underlying: str) -> list[date]:
        """The expiries to trade this cycle: the nearest N distinct expiries
        (SHORTVOL_MAX_EXPIRIES, default 1). Setting 2 adds the next expiry
        (e.g. weekly + monthly) → more condors on the same VRP edge."""
        n = max(1, int(os.getenv("SHORTVOL_MAX_EXPIRIES", "1")))
        # Skip expiries too close to expiry — near-expiry short-vol is thin premium
        # and high gamma. Continuous entry relies on this to keep quality.
        min_dte = max(0, int(os.getenv("SHORTVOL_MIN_DTE", "1")))
        today = date.today()
        try:
            exps = sorted(e for e in self._rt.instrument_master.expiries(underlying, Segment.OPTIONS)
                          if (e - today).days >= min_dte)
        except Exception as exc:
            logger.warning("short-vol: expiry list failed for %s: %s", underlying, exc)
            exps = []
        return exps[:n]

    def has_open_condor(self, underlying: str, expiry: date | None = None) -> bool:
        """True if we already hold an open option position on this underlying
        (and, when given, this expiry) — avoids stacking condors on the same
        underlying/expiry while still allowing different expiries to coexist."""
        u = underlying.strip().upper()
        try:
            for pos in self._rt.portfolio.positions.values():
                inst = pos.instrument
                if (
                    pos.quantity != 0
                    and inst.segment == Segment.OPTIONS
                    and (inst.underlying or "").strip().upper() == u
                    and (expiry is None or inst.expiry == expiry)
                ):
                    return True
        except Exception as exc:
            logger.warning("short-vol: open-condor check failed for %s: %s", underlying, exc)
        return False

    @property
    def auto_underlyings(self) -> list[str]:
        raw = os.getenv("SHORTVOL_AUTO_UNDERLYINGS", "NIFTY")
        return [u.strip().upper() for u in raw.split(",") if u.strip()]

    @property
    def structures(self) -> list[str]:
        """Which defined-risk structures to run: condor (symmetric vol premium)
        and/or put_spread (downside skew premium). One structure per (underlying,
        expiry) — they don't stack, to avoid concentrating downside risk."""
        raw = os.getenv("SHORTVOL_STRUCTURES", "condor")
        valid = {"condor", "put_spread", "call_spread"}
        out = [s.strip().lower() for s in raw.split(",") if s.strip().lower() in valid]
        return out or ["condor"]

    def is_entry_window(self, now_ist: datetime) -> bool:
        """Weekly entry cadence: enter on the configured weekday once the market
        has settled (default Monday, from 10:00 IST). VRP/chain checks still gate
        the actual order inside build()."""
        weekday = int(os.getenv("SHORTVOL_ENTRY_WEEKDAY", "0"))   # 0 = Monday
        hour = int(os.getenv("SHORTVOL_ENTRY_HOUR", "10"))
        return now_ist.weekday() == weekday and now_ist.hour >= hour

    def promotion_block_reason(self) -> str | None:
        """REDESIGN §5: auto-entry requires this strategy to have been promoted
        to a trading rung (paper/live_canary/live_approved) through the
        validation-gate ladder — which itself requires its backtest gates
        (CPCV/DSR/PBO/Monte-Carlo-DD/cost model) to have passed.

        Returns a human-readable block reason, or None when clear to trade.
        Set SHORTVOL_REQUIRE_PROMOTION=false to bypass (escape hatch for
        operating the strategy while its gate history is being built up).
        Fails OPEN on a lookup error: a promotion-service outage must not
        silently halt a running strategy — that's an availability decision,
        not a safety one, since RiskEngine/kill-switch still gate every order.
        """
        if not _env_flag("SHORTVOL_REQUIRE_PROMOTION", True):
            return None
        service = getattr(self._rt, "_strategy_promotion_service", None)
        if service is None:
            return None
        try:
            record = service.get_record(SHORT_VOL_STRATEGY_ID)
        except Exception as exc:
            logger.warning("short-vol promotion lookup failed (allowing): %s", exc)
            return None
        if record.status in _SHORTVOL_TRADING_STATUSES:
            return None
        return (
            f"strategy '{SHORT_VOL_STRATEGY_ID}' is at promotion status "
            f"'{record.status}' — needs one of {sorted(_SHORTVOL_TRADING_STATUSES)}. "
            f"Run backtest gates (POST /strategies/evaluate with evaluate_gates=true), "
            f"then POST /strategies/promote. Bypass with SHORTVOL_REQUIRE_PROMOTION=false."
        )

    async def auto_enter(self, now_ist: datetime) -> dict:
        """Attempt a condor entry on each configured underlying if we don't
        already hold one. Honours SHORTVOL_AUTO_ENABLED (default off) and the
        §5 promotion ladder. VRP and chain-width gates live in build(); this
        only decides *when* to look."""
        if not _env_flag("SHORTVOL_AUTO_ENABLED", False):
            return {"ran": False, "reason": "SHORTVOL_AUTO_ENABLED=false"}
        blocked = self.promotion_block_reason()
        if blocked:
            return {"ran": False, "reason": blocked, "promotion_blocked": True}
        results: list[dict] = []
        structures = self.structures
        # STAGGER entries across scans: only attempt a few NEW slots per call so
        # the ATM-IV candle fetches stay under Angel One's throttle. Slots with an
        # open position are skipped for free (no fetch); once all are filled the
        # scan is a no-op. Positions hold for days, so staggered entry is fine.
        max_per_scan = max(1, int(os.getenv("SHORTVOL_MAX_ENTRIES_PER_SCAN", "3")))
        attempts = 0
        first = True
        for underlying in self.auto_underlyings:
            for i, expiry in enumerate(self.target_expiries(underlying)):
                structure = structures[i % len(structures)]
                tag = {"underlying": underlying, "expiry": expiry.isoformat(), "structure": structure}
                if self.has_open_condor(underlying, expiry):
                    results.append({**tag, "submitted": False, "reason": "position already open"})
                    continue
                if attempts >= max_per_scan:
                    results.append({**tag, "submitted": False, "reason": "deferred (per-scan cap)"})
                    continue
                if not first:
                    await asyncio.sleep(float(os.getenv("SHORTVOL_ENTRY_SPACING", "2.5")))
                first = False
                attempts += 1
                try:
                    res = await self.enter(underlying, expiry, structure)
                except Exception as exc:
                    logger.warning("short-vol auto-enter failed for %s %s %s: %s",
                                   underlying, expiry, structure, exc)
                    res = {"submitted": False, "reason": f"error: {exc}"}
                results.append({**tag, **res})
        return {"ran": True, "results": results}

    # ── active exit management (profit target / stop loss) ───────────────────

    def find_open_structures(self, underlying: str | None = None) -> list[dict]:
        """Group open options positions by (underlying, expiry) — one entry per
        condor/spread structure currently held. Mirrors has_open_condor's
        filter but returns the full leg set instead of a bool."""
        groups: dict[tuple[str, date], list] = {}
        for pos in self._rt.portfolio.positions.values():
            if pos.quantity == 0:
                continue
            inst = pos.instrument
            if inst.segment != Segment.OPTIONS:
                continue
            u = (inst.underlying or "").strip().upper()
            if underlying is not None and u != underlying.strip().upper():
                continue
            if inst.expiry is None:
                continue
            groups.setdefault((u, inst.expiry), []).append(pos)
        return [
            {"underlying": u, "expiry": exp, "positions": positions}
            for (u, exp), positions in groups.items()
        ]

    def evaluate_exit(self, underlying: str, expiry: date, positions: list) -> dict:
        """Decide whether to close an open structure now: profit target
        (captured enough of the credit) or stop loss (lost too much of it).
        Otherwise hold — the per-leg expiry-triggered ExitPlan is the backstop.

        Reuses Position.unrealized_pnl() per leg rather than re-deriving P&L
        math: total P&L if closed now = sum of each leg's unrealized P&L at
        its current mark. Net credit received at entry is the negative of the
        entry cash flow (shorts received cash, longs paid cash)."""
        profit_target_pct = float(os.getenv("SHORTVOL_PROFIT_TARGET_PCT", "0.50"))
        stop_loss_multiple = float(os.getenv("SHORTVOL_STOP_LOSS_MULTIPLE", "1.5"))

        net_credit = 0.0
        total_pnl = 0.0
        for pos in positions:
            inst = pos.instrument
            current_price = self._option_last_price(inst)
            if current_price <= 0:
                return {"action": "hold", "reason": f"no current price for {inst.symbol}"}
            net_credit += -(pos.quantity * pos.average_price * inst.lot_size)
            total_pnl += pos.unrealized_pnl(current_price)

        if net_credit <= 0:
            # Net-debit structure (paid more than received) — the target/stop
            # rules below assume a net-credit condor; decline to manage rather
            # than misapply them to a shape they weren't designed for.
            return {"action": "hold", "reason": "non-credit structure — no active exit rule"}

        captured_pct = total_pnl / net_credit
        if captured_pct >= profit_target_pct:
            return {
                "action": "close", "reason": f"profit target: captured {captured_pct:.0%} of credit "
                                              f"(target {profit_target_pct:.0%})",
                "captured_pct": captured_pct,
            }
        if total_pnl <= -stop_loss_multiple * net_credit:
            return {
                "action": "close", "reason": f"stop loss: lost {-captured_pct:.0%} of credit "
                                              f"(limit {stop_loss_multiple:.0%})",
                "captured_pct": captured_pct,
            }
        return {
            "action": "hold", "reason": f"holding, captured {captured_pct:.0%} of credit "
                                         f"(target {profit_target_pct:.0%})",
            "captured_pct": captured_pct,
        }

    async def close_structure(self, underlying: str, expiry: date, positions: list, reason: str) -> dict:
        """Submit the closing legs (opposite side, full quantity) for an open
        structure. opens_position=False + a non-ENTRY priority route the fill
        through _on_fill's exit branch, which clears the ExitPlan registered
        at entry via the normal on_exit_fill path (symbol-based fallback,
        since no specific exit_plan_id is threaded through here)."""
        legs_payload = []
        for pos in positions:
            inst = pos.instrument
            current_price = self._option_last_price(inst)
            if current_price <= 0:
                return {"submitted": False, "reason": f"no current price for {inst.symbol}"}
            legs_payload.append({
                "symbol": inst.symbol,
                "side": "SELL" if pos.quantity > 0 else "BUY",
                "price": current_price,
                "quantity": abs(pos.quantity),
                "strategy_name": "short_vol_exit",
                "product_type": "CARRYFORWARD",
                "priority": "TARGET",
                "metadata": {"opens_position": False, "exit_reason": reason,
                             "expiry": expiry.isoformat()},
            })
        result = await self._rt.submit_multi_leg({
            "legs": legs_payload, "strategy_name": "short_vol_exit",
            "group_id": f"close_{underlying}_{expiry.isoformat()}",
        })
        return {"submitted": True, "result": result}
