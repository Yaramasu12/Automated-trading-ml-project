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
import os
import time
from datetime import date, datetime, timedelta
from typing import Any

from trading_platform.derivatives.engine import ImpliedVolatilityCalculator
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
        """Latest India VIX value (implied vol, %)."""
        try:
            import dataclasses
            from trading_platform.domain.enums import Exchange
            nifty = self._rt.instrument_master.get("NIFTY")
            inst = dataclasses.replace(nifty, symbol="INDIAVIX", token=_INDIA_VIX_TOKEN, exchange=Exchange("NSE"))
            to_dt = datetime.now(); from_dt = to_dt - timedelta(days=10)
            bars = self._rt.angel_one_history.get_candles(inst, from_dt, to_dt, "ONE_DAY")
            if bars:
                return float(bars[-1].close)
        except Exception as exc:
            logger.warning("short-vol: VIX fetch failed: %s", exc)
        return 0.0

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
        """Most recent traded price of an option contract (daily candle close).

        Cached per (symbol, day) and retried with backoff on rate-limit, because
        the candle API throttles hard when several strikes are fetched in a burst."""
        sym = getattr(inst, "symbol", "?")
        key = (sym, date.today())
        cached = self._price_cache.get(key)
        if cached is not None:
            return cached
        to_dt = datetime.now(); from_dt = to_dt - timedelta(days=10)
        for attempt in range(3):
            try:
                bars = self._rt.angel_one_history.get_candles(inst, from_dt, to_dt, "ONE_DAY")
                if bars:
                    price = float(bars[-1].close)
                    self._price_cache[key] = price
                    return price
                return 0.0
            except Exception as exc:
                if "rate" in str(exc).lower() and attempt < 2:
                    time.sleep(0.6 * (attempt + 1))   # 0.6s, 1.2s backoff
                    continue
                logger.warning("short-vol: option price fetch failed for %s: %s", sym, exc)
                return 0.0
        return 0.0

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
        )
        out: dict = {
            **base, "vix": round(iv, 2), "vrp": round(decision.vrp, 2),
            "structure": structure, "expiry": expiry.isoformat(), "dte": dte,
            "forecast_vol": round(forecast_vol, 2) if forecast_vol else None,
            "vol_reference": "garch_forecast" if forecast_vol else "trailing_realized",
            "enter": decision.enter, "reason": decision.reason, "lots": decision.lots,
            "net_credit_pts": decision.net_credit, "max_loss_pts": decision.max_loss,
        }
        if not decision.enter:
            return out

        vix = iv
        qty = decision.lots * lot_size
        for leg in decision.legs:
            inst = self._resolve_option(underlying, leg.strike, leg.option_type, expiry)
            if inst is None:
                out["enter"] = False
                out["reason"] = f"could not resolve {leg.option_type.value} {leg.strike:.0f} @ {expiry}"
                out["legs"] = []
                return out
            # price the leg at the actual days-to-expiry (multi-expiry aware)
            T = dte / 252.0
            premium = self.strategy._bs(spot, float(inst.strike), T, vix / 100.0,
                                        call=(leg.option_type.value == "CE"))
            out["legs"].append({
                "symbol": inst.symbol, "strike": float(inst.strike),
                "option_type": leg.option_type.value, "side": leg.side.value,
                "is_wing": leg.is_wing, "price": round(float(premium), 2), "quantity": qty,
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
        plan = self.build(underlying, expiry, structure)
        if not plan.get("enter") or not plan.get("legs"):
            return {"submitted": False, **plan}
        exp = plan.get("expiry", "")
        group_id = f"{structure}_{underlying}_{exp}"
        legs_payload = [
            {
                "symbol": l["symbol"], "side": l["side"], "price": l["price"],
                "quantity": l["quantity"], "strategy_name": f"short_vol_{structure}",
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

    async def auto_enter(self, now_ist: datetime) -> dict:
        """Attempt a condor entry on each configured underlying if we don't
        already hold one. Honours SHORTVOL_AUTO_ENABLED (default off). VRP and
        chain-width gates live in build(); this only decides *when* to look."""
        if not _env_flag("SHORTVOL_AUTO_ENABLED", False):
            return {"ran": False, "reason": "SHORTVOL_AUTO_ENABLED=false"}
        results: list[dict] = []
        structures = self.structures
        first = True
        for underlying in self.auto_underlyings:
            for i, expiry in enumerate(self.target_expiries(underlying)):
                if not first:
                    # Non-blocking spacing so the per-leg candle fetches don't burst
                    # into the Angel One rate limit (see _option_last_price).
                    await asyncio.sleep(1.0)
                first = False
                # One structure per expiry SLOT (condor on the near expiry,
                # put_spread on the next, …) so both edges trade on DIFFERENT
                # expiries and their downside never stacks on the same one.
                structure = structures[i % len(structures)]
                tag = {"underlying": underlying, "expiry": expiry.isoformat(), "structure": structure}
                if self.has_open_condor(underlying, expiry):
                    results.append({**tag, "submitted": False, "reason": "position already open"})
                    continue
                try:
                    res = await self.enter(underlying, expiry, structure)
                except Exception as exc:
                    logger.warning("short-vol auto-enter failed for %s %s %s: %s",
                                   underlying, expiry, structure, exc)
                    res = {"submitted": False, "reason": f"error: {exc}"}
                results.append({**tag, **res})
        return {"ran": True, "results": results}
