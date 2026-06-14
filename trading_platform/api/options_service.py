"""OptionsService — option chain, greeks, and IV-surface endpoints, extracted from runtime.

Phase 2 of the runtime decomposition. Read-only derivatives endpoints (expiries,
option chains synthetic + live, per-contract greeks, IV surface). Deps are injected
under their EXACT runtime attribute names so the bodies are a verbatim move.

NOTE: expiry_calendar / option_chain_builder are rebuilt by the runtime on
instrument refresh, so the runtime reconstructs this service in
_rebuild_market_engines to avoid holding stale builders.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from trading_platform.domain.enums import OptionType
from trading_platform.agent.market_hours import now_ist


class OptionsService:
    def __init__(
        self,
        *,
        expiry_calendar: Any,
        option_chain_builder: Any,
        live_feed: Any,
        greeks_calculator: Any,
        iv_surface_builder: Any,
    ) -> None:
        self.expiry_calendar = expiry_calendar
        self.option_chain_builder = option_chain_builder
        self.live_feed = live_feed
        self.greeks_calculator = greeks_calculator
        self.iv_surface_builder = iv_surface_builder

    def expiries(self, underlying: str) -> dict:
        today = now_ist().date()
        expiries = self.expiry_calendar.expiries(underlying.upper(), today)
        return {
            "underlying": underlying.upper(),
            "count": len(expiries),
            "expiries": [expiry.isoformat() for expiry in expiries],
            "nearest": expiries[0].isoformat() if expiries else None,
        }

    def option_chain(self, underlying: str, expiry: str | None = None, spot_price: float | None = None) -> dict:
        underlying = underlying.upper()
        if spot_price is None:
            tick = self.live_feed.latest_tick(underlying)
            if tick and tick.last_price > 0:
                spot_price = tick.last_price
        spot = float(spot_price or 0.0)
        expiry_date = date.fromisoformat(expiry) if expiry else self.expiry_calendar.nearest(underlying, now_ist().date())
        chain = self.option_chain_builder.build(underlying, expiry_date)
        dte = max(1, (expiry_date - now_ist().date()).days)
        liquid_strikes = chain.liquid_strikes(spot) if spot else chain.strikes

        def enrich(instrument, option_type: str) -> dict:
            payload = self._serialize_instrument(instrument)
            tick = self.live_feed.latest_tick(instrument.symbol)
            ltp = tick.last_price if tick and tick.last_price > 0 else None
            delta = None
            if spot > 0 and instrument.strike:
                try:
                    greeks = self.calculate_greeks(
                        {
                            "spot_price": spot,
                            "strike": instrument.strike,
                            "days_to_expiry": dte,
                            "volatility": 0.18,
                            "option_type": option_type,
                        }
                    )
                    delta = round(float(greeks.get("delta", 0.0)), 3)
                except Exception:
                    delta = None
            payload["ltp"] = ltp
            payload["delta"] = delta
            payload["live"] = ltp is not None
            return payload

        calls = [enrich(instrument, "CE") for instrument in chain.calls]
        puts = [enrich(instrument, "PE") for instrument in chain.puts]
        calls_by_strike = {item["strike"]: item for item in calls}
        puts_by_strike = {item["strike"]: item for item in puts}
        rows = [
            {
                "strike": strike,
                "call": calls_by_strike.get(strike),
                "put": puts_by_strike.get(strike),
            }
            for strike in chain.strikes
            if not liquid_strikes or strike in liquid_strikes
        ]
        option_symbols = [
            leg["symbol"]
            for row in rows
            for leg in (row.get("call"), row.get("put"))
            if leg and leg.get("symbol")
        ]
        if option_symbols:
            self.live_feed.add_subscriptions(option_symbols)
        return {
            "underlying": chain.underlying,
            "expiry": chain.expiry.isoformat(),
            "dte": dte,
            "spot_price": spot,
            "call_count": len(chain.calls),
            "put_count": len(chain.puts),
            "strikes": chain.strikes,
            "liquid_strikes": liquid_strikes,
            "calls": calls,
            "puts": puts,
            "rows": rows,
        }

    def calculate_greeks(self, payload: dict) -> dict:
        greeks = self.greeks_calculator.calculate(
            spot_price=float(payload["spot_price"]),
            strike=float(payload["strike"]),
            days_to_expiry=int(payload["days_to_expiry"]),
            volatility=float(payload.get("volatility", 0.20)),
            option_type=OptionType(str(payload.get("option_type", "CE")).upper()),
            risk_free_rate=float(payload.get("risk_free_rate", 0.06)),
        )
        return asdict(greeks)

    def option_chain_live(self, underlying: str, expiry: str | None = None, spot_price: float | None = None) -> dict:
        """Option chain enriched with live tick prices and BS-calculated greeks."""
        underlying = underlying.upper()
        # Use live feed spot if not provided
        if spot_price is None:
            tick = self.live_feed.latest_tick(underlying)
            if tick and tick.last_price > 0:
                spot_price = tick.last_price
        spot = spot_price or 0.0

        expiry_date = (
            date.fromisoformat(expiry) if expiry
            else self.expiry_calendar.nearest(underlying, now_ist().date())
        )
        chain = self.option_chain_builder.build(underlying, expiry_date)
        dte = max(1, (expiry_date - now_ist().date()).days)

        def _enrich(instrument, opt_type: str) -> dict:
            base = self._serialize_instrument(instrument)
            tick = self.live_feed.latest_tick(instrument.symbol)
            ltp = tick.last_price if (tick and tick.last_price > 0) else None
            delta = None
            if spot > 0:
                try:
                    g = self.calculate_greeks({
                        "spot_price": spot,
                        "strike": instrument.strike,
                        "days_to_expiry": dte,
                        "volatility": 0.18,
                        "option_type": opt_type,
                    })
                    delta = round(g.get("delta", 0.0), 3)
                except Exception:
                    pass
            base["ltp"] = ltp
            base["delta"] = delta
            base["live"] = ltp is not None
            return base

        liquid = chain.liquid_strikes(spot) if spot else chain.strikes
        calls_map = {i.strike: _enrich(i, "CE") for i in chain.calls}
        puts_map  = {i.strike: _enrich(i, "PE") for i in chain.puts}

        strikes = sorted(set(calls_map) | set(puts_map))
        rows = []
        for s in strikes:
            if liquid and s not in liquid:
                continue
            rows.append({
                "strike": s,
                "call": calls_map.get(s),
                "put": puts_map.get(s),
            })
        return {
            "underlying": underlying,
            "expiry": expiry_date.isoformat(),
            "dte": dte,
            "spot_price": spot,
            "rows": rows,
        }

    def _serialize_instrument(self, instrument) -> dict:
        return {
            "symbol": instrument.symbol,
            "exchange": instrument.exchange.value,
            "segment": instrument.segment.value,
            "type": instrument.instrument_type.value,
            "underlying": instrument.underlying,
            "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
            "strike": instrument.strike,
            "option_type": instrument.option_type.value if instrument.option_type else None,
            "lot_size": instrument.lot_size,
            "token": instrument.token,
        }

    def iv_surface_compute(self, payload: dict) -> dict:
        underlying = str(payload["underlying"]).upper()
        spot_price = float(payload["spot_price"])
        expiry_raw = payload.get("expiry")
        market_prices: dict[str, float] = {str(k): float(v) for k, v in (payload.get("market_prices") or {}).items()}
        risk_free_rate = float(payload.get("risk_free_rate", 0.06))

        as_of = date.fromisoformat(str(payload.get("as_of", now_ist().date().isoformat())))
        if expiry_raw:
            expiry_date = date.fromisoformat(str(expiry_raw))
        else:
            expiry_date = self.expiry_calendar.nearest(underlying, as_of)

        chain = self.option_chain_builder.build(underlying, expiry_date)

        if not market_prices:
            # Synthetic premiums using Black-Scholes with a flat 20% vol assumption
            volatility = float(payload.get("volatility", 0.20))
            dte = max((expiry_date - as_of).days, 1)
            for instrument in [*chain.calls, *chain.puts]:
                if instrument.strike and instrument.option_type:
                    greeks = self.greeks_calculator.calculate(
                        spot_price=spot_price,
                        strike=instrument.strike,
                        days_to_expiry=dte,
                        volatility=volatility,
                        option_type=instrument.option_type,
                        risk_free_rate=risk_free_rate,
                    )
                    # Use vega-scaled price as synthetic premium
                    intrinsic = max(0.0, (spot_price - instrument.strike) if instrument.option_type.value == "CE" else (instrument.strike - spot_price))
                    premium = max(intrinsic + greeks.vega * volatility * 100, 0.50)
                    market_prices[instrument.symbol] = premium

        surface = self.iv_surface_builder.build(
            underlying=underlying,
            option_chain=chain,
            spot_price=spot_price,
            market_prices=market_prices,
            as_of=as_of,
            risk_free_rate=risk_free_rate,
        )
        return surface.to_dict()

    # ------------------------------------------------------------------
    # Walk-forward backtesting
    # ------------------------------------------------------------------

