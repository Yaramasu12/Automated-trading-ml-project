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

import logging
from datetime import date, datetime, timedelta
from typing import Any

from trading_platform.domain.enums import Segment, Side
from trading_platform.strategies.short_vol import ShortVolStrategy

logger = logging.getLogger(__name__)

# India VIX candle token (verified fetchable 2026-07-16).
_INDIA_VIX_TOKEN = "99926017"


class ShortVolExecutor:
    def __init__(self, runtime: Any, strategy: ShortVolStrategy | None = None) -> None:
        self._rt = runtime
        self.strategy = strategy or ShortVolStrategy()

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

    # ── decision + leg resolution ───────────────────────────────────────────

    def build(self, underlying: str = "NIFTY") -> dict:
        """Compute the full short-vol decision with resolved contracts & prices.
        Does NOT execute. Returns a dict describing what WOULD be traded."""
        closes, spot = self._closes_and_spot(underlying)
        vix = self._current_vix()
        capital = float(getattr(self._rt.portfolio, "equity", 0) or self._rt.portfolio.cash)
        lot_size = int(getattr(self._rt.instrument_master.get(underlying), "lot_size", 50) or 50)

        decision = self.strategy.decide(
            spot=spot, vix=vix, closes=closes, capital=capital, lot_size=lot_size,
        )
        out: dict = {
            "underlying": underlying, "spot": round(spot, 2), "vix": round(vix, 2),
            "realized_vol": round(self.strategy.realized_vol(closes), 2),
            "vrp": round(decision.vrp, 2), "enter": decision.enter, "reason": decision.reason,
            "lots": decision.lots, "net_credit_pts": decision.net_credit,
            "max_loss_pts": decision.max_loss, "legs": [],
        }
        if not decision.enter:
            return out

        expiry = self._rt.instrument_master.nearest_expiry(underlying, date.today(), segment=Segment.OPTIONS)
        qty = decision.lots * lot_size
        for leg in decision.legs:
            inst = self._resolve_option(underlying, leg.strike, leg.option_type, expiry)
            if inst is None:
                out["enter"] = False
                out["reason"] = f"could not resolve {leg.option_type.value} {leg.strike:.0f} @ {expiry}"
                out["legs"] = []
                return out
            # price the leg (live tick -> BS via the agent's resolver would also work)
            T = self.strategy.hold_days / 252.0
            premium = self.strategy._bs(spot, float(inst.strike), T, vix / 100.0,
                                        call=(leg.option_type.value == "CE"))
            out["legs"].append({
                "symbol": inst.symbol, "strike": float(inst.strike),
                "option_type": leg.option_type.value, "side": leg.side.value,
                "is_wing": leg.is_wing, "price": round(float(premium), 2), "quantity": qty,
            })
        out["expiry"] = expiry.isoformat()

        # SAFETY: a valid iron condor needs 4 DISTINCT strikes with the wings
        # strictly beyond the shorts (real protection). When the option chain is
        # too narrow, _resolve_option snaps the short and wing to the same strike,
        # collapsing the condor into an unprotected/zero-width position. Never
        # execute that — decline and say why (the chain must be wider).
        by = {(l["option_type"], l["side"]): l["strike"] for l in out["legs"]}
        cs, cw = by.get(("CE", "SELL")), by.get(("CE", "BUY"))
        ps, pw = by.get(("PE", "SELL")), by.get(("PE", "BUY"))
        if None in (cs, cw, ps, pw) or not (cw > cs and pw < ps):
            out["enter"] = False
            out["reason"] = (
                f"option chain too narrow at {underlying} {expiry}: condor legs "
                f"collapsed (CE {cs}/{cw}, PE {ps}/{pw}) — need wider strikes"
            )
            out["legs"] = []
        return out

    def preview(self, underlying: str = "NIFTY") -> dict:
        return {"mode": "preview", **self.build(underlying)}

    async def enter(self, underlying: str = "NIFTY") -> dict:
        """Build then SUBMIT the iron condor as a multi-leg order."""
        plan = self.build(underlying)
        if not plan.get("enter") or not plan.get("legs"):
            return {"submitted": False, **plan}
        legs_payload = [
            {
                "symbol": l["symbol"], "side": l["side"], "price": l["price"],
                "quantity": l["quantity"], "strategy_name": "short_vol_condor",
                "metadata": {"is_wing": l["is_wing"], "vrp": plan["vrp"]},
            }
            for l in plan["legs"]
        ]
        result = await self._rt.submit_multi_leg({
            "legs": legs_payload, "strategy_name": "short_vol_condor",
        })
        return {"submitted": True, "plan": plan, "execution": result}
