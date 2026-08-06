"""Unit tests for the defined-risk short-vol strategy logic."""
from __future__ import annotations

import asyncio
import math
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import numpy as np

from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    Segment,
    Side,
)
from trading_platform.domain.models import Instrument, Position
from trading_platform.strategies.short_vol import ShortVolStrategy
from trading_platform.strategies.short_vol_executor import ShortVolExecutor


def _flat_closes(price=24000.0, n=40, daily_vol=0.006, seed=1):
    """Synthetic closes with a known daily vol -> ~annualized daily_vol*sqrt(252)."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0, daily_vol, n)
    return list(price * np.exp(np.cumsum(r)))


class ShortVolTests(unittest.TestCase):
    def setUp(self):
        self.s = ShortVolStrategy(sd=1.25, wing_width=300, risk_budget=0.05, min_vrp=2.0)

    def test_realized_vol_reasonable(self):
        # daily vol 0.006 -> ann ~ 0.006*sqrt(252)*100 ~ 9.5%
        rv = ShortVolStrategy.realized_vol(_flat_closes(daily_vol=0.006))
        self.assertTrue(6.0 < rv < 14.0, rv)

    def test_no_entry_when_premium_thin(self):
        # realized ~9.5%, VIX 10 -> VRP ~0.5 < 2 -> no entry
        d = self.s.decide(spot=24000, vix=10.0, closes=_flat_closes(daily_vol=0.006),
                          capital=1_000_000, lot_size=50)
        self.assertFalse(d.enter)
        self.assertIn("premium not rich", d.reason)

    def test_entry_when_premium_rich(self):
        # realized ~9.5%, VIX 16 -> VRP ~6.5 >= 2 -> enter with a full condor
        d = self.s.decide(spot=24000, vix=16.0, closes=_flat_closes(daily_vol=0.006),
                          capital=1_000_000, lot_size=50)
        self.assertTrue(d.enter, d.reason)
        self.assertEqual(len(d.legs), 4)
        self.assertGreaterEqual(d.lots, 1)
        self.assertGreater(d.net_credit, 0)
        self.assertGreater(d.max_loss, 0)

    def test_condor_is_defined_risk(self):
        d = self.s.decide(spot=24000, vix=18.0, closes=_flat_closes(daily_vol=0.006),
                          capital=1_000_000, lot_size=50)
        sells = [l for l in d.legs if l.side == Side.SELL]
        buys = [l for l in d.legs if l.side == Side.BUY]
        # exactly 2 short (income) + 2 long wings (protection) = defined risk
        self.assertEqual(len(sells), 2)
        self.assertEqual(len(buys), 2)
        # wings are further OTM than the shorts (real protection)
        call_short = next(l.strike for l in sells if l.option_type == OptionType.CE)
        call_wing = next(l.strike for l in buys if l.option_type == OptionType.CE)
        put_short = next(l.strike for l in sells if l.option_type == OptionType.PE)
        put_wing = next(l.strike for l in buys if l.option_type == OptionType.PE)
        self.assertGreater(call_wing, call_short)
        self.assertLess(put_wing, put_short)
        # max loss can never exceed the wing width (the whole point of defined risk)
        self.assertLessEqual(d.max_loss, self.s.wing_width)

    def test_risk_budget_caps_lots(self):
        small = self.s.decide(spot=24000, vix=16.0, closes=_flat_closes(daily_vol=0.006),
                              capital=100_000, lot_size=50)
        big = self.s.decide(spot=24000, vix=16.0, closes=_flat_closes(daily_vol=0.006),
                            capital=2_000_000, lot_size=50)
        if small.enter and big.enter:
            self.assertLess(small.lots, big.lots)


def _option(underlying="NIFTY", strike=24000.0, ot=OptionType.CE, expiry=None):
    return Instrument(
        symbol=f"{underlying}{int(strike)}{ot.value}", name=underlying,
        exchange=Exchange.NFO,
        segment=Segment.OPTIONS, asset_class=AssetClass.INDEX,
        instrument_type=InstrumentType.OPTION, token="1", lot_size=50, tick_size=0.05,
        expiry=expiry, strike=strike, option_type=ot, underlying=underlying,
    )


class ShortVolAutoEntryTests(unittest.TestCase):
    def _executor(self, positions, expiries=None):
        from datetime import date as _date
        exps = expiries if expiries is not None else [_date(2100, 1, 7)]
        master = SimpleNamespace(expiries=lambda u, seg=None: list(exps))
        rt = SimpleNamespace(
            portfolio=SimpleNamespace(positions=positions),
            instrument_master=master,
        )
        return ShortVolExecutor(rt)

    def test_has_open_condor_true_when_option_position_open(self):
        pos = {"NIFTY24000CE": Position(instrument=_option(), quantity=-50)}
        ex = self._executor(pos)
        self.assertTrue(ex.has_open_condor("NIFTY"))
        self.assertFalse(ex.has_open_condor("BANKNIFTY"))

    def test_has_open_condor_false_when_flat(self):
        pos = {"NIFTY24000CE": Position(instrument=_option(), quantity=0)}
        self.assertFalse(self._executor(pos).has_open_condor("NIFTY"))

    def test_is_entry_window(self):
        ex = self._executor({})
        with mock.patch.dict(os.environ, {"SHORTVOL_ENTRY_WEEKDAY": "0", "SHORTVOL_ENTRY_HOUR": "10"}):
            self.assertTrue(ex.is_entry_window(datetime(2026, 7, 13, 10, 30)))   # Monday 10:30
            self.assertFalse(ex.is_entry_window(datetime(2026, 7, 13, 9, 30)))   # Monday 09:30 (too early)
            self.assertFalse(ex.is_entry_window(datetime(2026, 7, 14, 10, 30)))  # Tuesday

    def test_auto_enter_disabled_by_default(self):
        ex = self._executor({})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHORTVOL_AUTO_ENABLED", None)
            out = asyncio.run(ex.auto_enter(datetime(2026, 7, 13, 10, 30)))
        self.assertFalse(out["ran"])

    def test_auto_enter_skips_when_condor_open(self):
        from datetime import date as _date
        exp = _date(2100, 1, 7)
        pos = {"NIFTY24000CE": Position(instrument=_option(expiry=exp), quantity=-50)}
        ex = self._executor(pos, expiries=[exp])
        with mock.patch.dict(os.environ, {"SHORTVOL_AUTO_ENABLED": "true", "SHORTVOL_AUTO_UNDERLYINGS": "NIFTY",
                                          "SHORTVOL_MAX_EXPIRIES": "1"}):
            out = asyncio.run(ex.auto_enter(datetime(2026, 7, 13, 10, 30)))
        self.assertTrue(out["ran"])
        self.assertIn("already open", out["results"][0]["reason"])
        self.assertFalse(out["results"][0]["submitted"])

    def test_multi_expiry_targets_two(self):
        from datetime import date as _date
        exps = [_date(2100, 1, 7), _date(2100, 1, 28)]
        ex = self._executor({}, expiries=exps)
        with mock.patch.dict(os.environ, {"SHORTVOL_MAX_EXPIRIES": "2"}):
            self.assertEqual(len(ex.target_expiries("NIFTY")), 2)
        with mock.patch.dict(os.environ, {"SHORTVOL_MAX_EXPIRIES": "1"}):
            self.assertEqual(len(ex.target_expiries("NIFTY")), 1)


class MultiIndexTests(unittest.TestCase):
    """The same path must be correct across indices with very different price
    levels and strike steps — using each index's OWN implied vol, not India VIX."""

    def _executor(self, master=None):
        rt = SimpleNamespace(
            portfolio=SimpleNamespace(positions={}, cash=1_000_000, equity=1_000_000),
            instrument_master=master,
        )
        return ShortVolExecutor(rt)

    def test_wing_width_scales_with_price_level(self):
        ex = self._executor()
        # NIFTY-tuned default (1.25%): ~300 on 24000 with a 50 step
        self.assertEqual(ex._wing_width(24000, 50), 300.0)
        # BANKNIFTY ~51000 / step 100 -> ~640 rounded to a 100 grid
        self.assertGreater(ex._wing_width(51000, 100), 500.0)
        # SENSEX ~82000 / step 100 -> ~1000, far wider than NIFTY's 300
        self.assertGreater(ex._wing_width(82000, 100), 900.0)
        # never narrower than two strike steps
        self.assertGreaterEqual(ex._wing_width(1000, 100), 200.0)

    def test_infer_strike_step_from_chain(self):
        from datetime import date as _date
        exp = _date(2026, 7, 23)
        opts = [_option(strike=float(s), ot=OptionType.CE, expiry=exp) for s in range(50000, 51000, 100)]
        master = SimpleNamespace(by_underlying=lambda u, seg: opts)
        ex = self._executor(master)
        self.assertEqual(ex._infer_strike_step("BANKNIFTY", exp), 100)

    def test_decide_uses_passed_step_and_wing(self):
        # A high-priced index (spot 51000) with its own step/wing produces a
        # defined-risk condor whose max loss is capped by the passed wing width.
        s = ShortVolStrategy(sd=1.25, min_vrp=2.0)
        d = s.decide(spot=51000, vix=18.0, closes=[51000 * 1.0004 ** i for i in range(40)],
                     capital=2_000_000, lot_size=15, strike_step=100, wing_width=700)
        if d.enter:
            self.assertLessEqual(d.max_loss, 700)
            # strikes must land on the 100 grid
            for leg in d.legs:
                self.assertEqual(leg.strike % 100, 0)


class CondorExitContractTests(unittest.TestCase):
    """Locks in the invariant: a defined-risk condor leg is held to expiry —
    a premium swing must NOT stop it out (that would unbalance the structure)."""

    def _expiry_only_plan(self, side=Side.SELL, expiry_days=3):
        from datetime import date as _date, timedelta as _td
        from trading_platform.domain.models import Trade
        from trading_platform.exit.exit_plan import ExitPlan
        trade = Trade(
            trade_id="t1", order_id="o1", symbol="NIFTY24000CE", side=side,
            quantity=50, price=100.0, charges=0.0, timestamp=datetime(2026, 7, 13, 10, 0),
            strategy_name="short_vol_condor",
        )
        plan = ExitPlan.from_trade(trade, instrument=_option(ot=OptionType.CE),
                                   expiry_date=_date.today() + _td(days=expiry_days))
        # Same nulling on_fill applies to multi-leg condor legs:
        plan.stop_loss_price = None
        plan.target_price = None
        plan.trailing_pct = None
        plan.partial_exit_enabled = False
        return plan

    def test_premium_swing_does_not_trigger(self):
        from trading_platform.exit.exit_plan import ExitTrigger
        plan = self._expiry_only_plan()
        # Short premium doubling (a big adverse move) must NOT exit the leg.
        self.assertIsNone(plan.check_trigger(200.0, datetime(2026, 7, 14, 11, 0)))
        # Premium collapsing to near zero (a big favourable move) also holds.
        self.assertIsNone(plan.check_trigger(5.0, datetime(2026, 7, 14, 11, 0)))

    def test_exits_at_expiry(self):
        from datetime import date as _date, timedelta as _td
        from trading_platform.exit.exit_plan import ExitTrigger
        plan = self._expiry_only_plan(expiry_days=0)
        trig = plan.check_trigger(120.0, datetime.combine(_date.today() + _td(days=0), datetime.min.time()))
        self.assertEqual(trig, ExitTrigger.EXPIRY)


def _nifty_index_instrument():
    return Instrument(
        symbol="NIFTY", name="NIFTY", exchange=Exchange.NSE,
        segment=Segment.CASH, asset_class=AssetClass.INDEX,
        instrument_type=InstrumentType.INDEX, token="26000",
    )


class CurrentVixTests(unittest.TestCase):
    """2026-08-05 fix: _current_vix() used to go straight to the rate-limited
    candle API, the same disconnected-price bug as _option_last_price had."""

    def _executor(self, price_resolution, get_candles=None):
        rt = SimpleNamespace(
            instrument_master=SimpleNamespace(get=lambda sym: _nifty_index_instrument()),
            live_feed=SimpleNamespace(
                register_instruments=mock.Mock(), add_subscriptions=mock.Mock()
            ),
            price_service=SimpleNamespace(resolve=lambda *a, **k: price_resolution),
            angel_one_history=SimpleNamespace(get_candles=get_candles or (lambda *a: [])),
        )
        return ShortVolExecutor(rt)

    def test_prefers_live_price(self):
        ex = self._executor(SimpleNamespace(price=13.42, source="live", is_stale=False))
        self.assertEqual(ex._current_vix(), 13.42)
        ex._rt.live_feed.register_instruments.assert_called_once()
        ex._rt.live_feed.add_subscriptions.assert_called_once_with(["INDIAVIX"])

    def test_falls_back_to_candle_when_no_live_price(self):
        gc = lambda inst, a, b, tf: [SimpleNamespace(close=14.71)]
        ex = self._executor(SimpleNamespace(price=None, source=None, is_stale=True), gc)
        self.assertEqual(ex._current_vix(), 14.71)

    def test_returns_zero_when_nothing_available(self):
        ex = self._executor(SimpleNamespace(price=None, source=None, is_stale=True), lambda *a: [])
        self.assertEqual(ex._current_vix(), 0.0)


class OptionPriceFetchTests(unittest.TestCase):
    """Rate-limit hardening of the ATM option-price candle fetch."""

    def _inst(self, sym="NIFTY28JUL2624000CE"):
        return SimpleNamespace(symbol=sym, strike=24000.0, lot_size=75)

    def _executor(self, get_candles):
        # price_service resolves to nothing so these tests exercise the
        # candle-fetch fallback specifically, not the live/model tiers ahead
        # of it (covered separately in test_price_resolution_service.py).
        rt = SimpleNamespace(
            angel_one_history=SimpleNamespace(get_candles=get_candles),
            price_service=SimpleNamespace(
                resolve=lambda *a, **k: SimpleNamespace(price=None, source=None, is_stale=True)
            ),
        )
        return ShortVolExecutor(rt)

    def test_success_is_cached_and_fetched_once(self):
        calls = {"n": 0}
        def gc(inst, a, b, tf):
            calls["n"] += 1
            return [SimpleNamespace(close=123.5)]
        ex = self._executor(gc)
        self.assertEqual(ex._option_last_price(self._inst()), 123.5)
        self.assertEqual(ex._option_last_price(self._inst()), 123.5)  # served from cache
        self.assertEqual(calls["n"], 1)

    def test_persistent_rate_limit_negatively_cached(self):
        """2026-08-06: the retry loop here was removed as duplicate of
        AngelOneHistoricalDataProvider's own retry/backoff (tested directly
        in test_angel_one_data.py) — get_candles() is now called once per
        _option_last_price() call and is expected to have already exhausted
        its own retries before raising. What this test covers is what's
        still genuinely this method's own behavior: caching that failure
        negatively so a persistently rate-limited contract isn't re-fetched
        every scan cycle."""
        calls = {"n": 0}
        def gc(inst, a, b, tf):
            calls["n"] += 1
            raise Exception("exceeding access rate")
        ex = self._executor(gc)
        with mock.patch.dict(os.environ, {"SHORTVOL_FETCH_NEG_TTL": "60"}):
            self.assertEqual(ex._option_last_price(self._inst()), 0.0)
            self.assertEqual(calls["n"], 1)
            # Second call within TTL must NOT re-hammer the throttled contract.
            self.assertEqual(ex._option_last_price(self._inst()), 0.0)
        self.assertEqual(calls["n"], 1)


class EnterProductTypeTests(unittest.TestCase):
    """Fix 2026-08-03: short-vol legs are held for days, so they must be
    submitted as CARRYFORWARD, not the default INTRADAY (which the broker
    force-squares-off same day in LIVE, and which our own EOD sweep used to
    close in PAPER)."""

    def test_enter_submits_legs_as_carryforward(self):
        rt = SimpleNamespace(submit_multi_leg=mock.AsyncMock(return_value={"submitted": True}))
        ex = ShortVolExecutor(rt)
        fake_plan = {
            "enter": True, "vrp": 3.0, "expiry": "2100-01-07",
            "legs": [
                {"symbol": "NIFTY24000CE", "side": "SELL", "price": 100.0, "quantity": 1, "is_wing": False},
                {"symbol": "NIFTY24300CE", "side": "BUY", "price": 40.0, "quantity": 1, "is_wing": True},
                {"symbol": "NIFTY23000PE", "side": "SELL", "price": 90.0, "quantity": 1, "is_wing": False},
                {"symbol": "NIFTY22700PE", "side": "BUY", "price": 35.0, "quantity": 1, "is_wing": True},
            ],
        }
        ex.build = lambda underlying, expiry=None, structure="condor": fake_plan
        result = asyncio.run(ex.enter("NIFTY", None, "condor"))
        self.assertTrue(result["submitted"])
        submitted_payload = rt.submit_multi_leg.call_args[0][0]
        self.assertEqual(len(submitted_payload["legs"]), 4)
        for leg in submitted_payload["legs"]:
            self.assertEqual(leg["product_type"], "CARRYFORWARD")


class ActiveExitPolicyTests(unittest.TestCase):
    """Fix 2026-08-03: structure-level profit-target/stop-loss exit — the
    per-leg ExitPlan only fires on expiry; this is the active management on
    top of it."""

    def _condor_positions(self, exp, underlying="NIFTY"):
        # sell call (short), buy wing call (long), sell put (short), buy wing put (long)
        strikes = {"NIFTY": (24000.0, 24300.0, 23000.0, 22700.0),
                   "BANKNIFTY": (55000.0, 55600.0, 53000.0, 52400.0)}[underlying]
        cs, cw, ps, pw = strikes
        legs = {
            (cs, OptionType.CE): (-1, 100.0),
            (cw, OptionType.CE): (1, 40.0),
            (ps, OptionType.PE): (-1, 90.0),
            (pw, OptionType.PE): (1, 35.0),
        }
        out = {}
        for (strike, ot), (qty, avg) in legs.items():
            inst = _option(underlying, strike, ot, exp)
            out[inst.symbol] = Position(instrument=inst, quantity=qty, average_price=avg)
        return out

    def _executor(self, positions, prices):
        def gc(inst, a, b, tf):
            return [SimpleNamespace(close=prices[inst.symbol])]
        rt = SimpleNamespace(
            portfolio=SimpleNamespace(positions=positions),
            angel_one_history=SimpleNamespace(get_candles=gc),
            submit_multi_leg=mock.AsyncMock(return_value={"submitted": True}),
            # price_service resolves to nothing so these tests exercise the
            # candle-fetch fallback via `prices`, matching prior behavior.
            price_service=SimpleNamespace(
                resolve=lambda *a, **k: SimpleNamespace(price=None, source=None, is_stale=True)
            ),
        )
        return ShortVolExecutor(rt)

    def test_find_open_structures_groups_by_underlying_and_expiry(self):
        from datetime import date as _date
        exp1, exp2 = _date(2100, 1, 7), _date(2100, 2, 4)
        flat_inst = _option("NIFTY", 21000.0, OptionType.CE, exp1)
        positions = {
            **self._condor_positions(exp1),
            **{f"bn_{k}": v for k, v in self._condor_positions(exp2, underlying="BANKNIFTY").items()},
            "FLAT": Position(instrument=flat_inst, quantity=0, average_price=50.0),
            "RELIANCE": Position(
                instrument=Instrument(symbol="RELIANCE", name="RELIANCE", exchange=Exchange.NSE,
                                       segment=Segment.CASH, asset_class=AssetClass.EQUITY,
                                       instrument_type=InstrumentType.EQUITY, token="1"),
                quantity=10, average_price=2800.0),
        }
        ex = self._executor(positions, {})
        groups = ex.find_open_structures()
        keys = {(g["underlying"], g["expiry"]) for g in groups}
        self.assertEqual(keys, {("NIFTY", exp1), ("BANKNIFTY", exp2)})
        nifty_group = next(g for g in groups if g["underlying"] == "NIFTY")
        self.assertEqual(len(nifty_group["positions"]), 4)

    def test_find_open_structures_filters_by_underlying(self):
        from datetime import date as _date
        exp = _date(2100, 1, 7)
        ex = self._executor(self._condor_positions(exp), {})
        self.assertEqual(len(ex.find_open_structures("NIFTY")), 1)
        self.assertEqual(len(ex.find_open_structures("BANKNIFTY")), 0)

    def test_evaluate_exit_profit_target(self):
        from datetime import date as _date
        exp = _date(2100, 1, 7)
        positions = self._condor_positions(exp)
        # Both shorts decayed a lot (profit), wings decayed some (small loss) —
        # net well over 50% of the 5750 credit received.
        prices = {"NIFTY24000CE": 20.0, "NIFTY24300CE": 10.0, "NIFTY23000PE": 15.0, "NIFTY22700PE": 5.0}
        ex = self._executor(positions, prices)
        with mock.patch.dict(os.environ, {"SHORTVOL_PROFIT_TARGET_PCT": "0.50"}):
            verdict = ex.evaluate_exit("NIFTY", exp, list(positions.values()))
        self.assertEqual(verdict["action"], "close")
        self.assertIn("profit target", verdict["reason"])
        self.assertGreaterEqual(verdict["captured_pct"], 0.50)

    def test_evaluate_exit_stop_loss(self):
        from datetime import date as _date
        exp = _date(2100, 1, 7)
        positions = self._condor_positions(exp)
        # Short call blows out hard against us, wing only partially offsets —
        # well beyond a 1.5x-credit loss.
        prices = {"NIFTY24000CE": 400.0, "NIFTY24300CE": 50.0, "NIFTY23000PE": 90.0, "NIFTY22700PE": 35.0}
        ex = self._executor(positions, prices)
        with mock.patch.dict(os.environ, {"SHORTVOL_STOP_LOSS_MULTIPLE": "1.5"}):
            verdict = ex.evaluate_exit("NIFTY", exp, list(positions.values()))
        self.assertEqual(verdict["action"], "close")
        self.assertIn("stop loss", verdict["reason"])

    def test_evaluate_exit_holds_when_no_threshold_crossed(self):
        from datetime import date as _date
        exp = _date(2100, 1, 7)
        positions = self._condor_positions(exp)
        # No move at all -> 0% captured, well inside both thresholds.
        prices = {"NIFTY24000CE": 100.0, "NIFTY24300CE": 40.0, "NIFTY23000PE": 90.0, "NIFTY22700PE": 35.0}
        ex = self._executor(positions, prices)
        verdict = ex.evaluate_exit("NIFTY", exp, list(positions.values()))
        self.assertEqual(verdict["action"], "hold")
        self.assertAlmostEqual(verdict["captured_pct"], 0.0, places=6)

    def test_evaluate_exit_holds_when_price_unavailable(self):
        from datetime import date as _date
        exp = _date(2100, 1, 7)
        positions = self._condor_positions(exp)

        def gc(inst, a, b, tf):
            return []   # no candles -> _option_last_price returns 0.0

        rt = SimpleNamespace(
            portfolio=SimpleNamespace(positions=positions),
            angel_one_history=SimpleNamespace(get_candles=gc),
            price_service=SimpleNamespace(
                resolve=lambda *a, **k: SimpleNamespace(price=None, source=None, is_stale=True)
            ),
        )
        ex = ShortVolExecutor(rt)
        verdict = ex.evaluate_exit("NIFTY", exp, list(positions.values()))
        self.assertEqual(verdict["action"], "hold")
        self.assertIn("no current price", verdict["reason"])

    def test_close_structure_submits_opposite_side_and_opens_position_false(self):
        from datetime import date as _date
        exp = _date(2100, 1, 7)
        positions = self._condor_positions(exp)
        prices = {"NIFTY24000CE": 20.0, "NIFTY24300CE": 10.0, "NIFTY23000PE": 15.0, "NIFTY22700PE": 5.0}
        ex = self._executor(positions, prices)
        result = asyncio.run(ex.close_structure("NIFTY", exp, list(positions.values()), "profit target"))
        self.assertTrue(result["submitted"])
        submitted_payload = ex._rt.submit_multi_leg.call_args[0][0]
        by_symbol = {leg["symbol"]: leg for leg in submitted_payload["legs"]}
        # Closing a short (quantity<0) = BUY; closing a long (quantity>0) = SELL.
        self.assertEqual(by_symbol["NIFTY24000CE"]["side"], "BUY")
        self.assertEqual(by_symbol["NIFTY24300CE"]["side"], "SELL")
        self.assertEqual(by_symbol["NIFTY23000PE"]["side"], "BUY")
        self.assertEqual(by_symbol["NIFTY22700PE"]["side"], "SELL")
        for leg in submitted_payload["legs"]:
            self.assertEqual(leg["product_type"], "CARRYFORWARD")
            self.assertFalse(leg["metadata"]["opens_position"])
            self.assertNotEqual(leg["priority"], "ENTRY")


class IvRankGateTests(unittest.TestCase):
    """2026-08-06: IV rank/percentile (derivatives.engine.compute_iv_rank) is
    surfaced as a diagnostic on every build() call, and gated behind
    SHORTVOL_MIN_IV_RANK (default 0 = off) as an opt-in secondary confirm on
    top of the already-validated VRP threshold — never a silent replacement
    for it, per the "models must earn deployment" rule."""

    def _executor(self, *, atm_premium=250.0, vix_history=None, closes=None):
        from datetime import date as _date, timedelta as _td
        expiry = _date.today() + _td(days=7)
        strikes = [float(k) for k in range(22000, 27050, 50)]
        opts = [
            _option("NIFTY", k, ot, expiry)
            for k in strikes for ot in (OptionType.CE, OptionType.PE)
        ]
        nifty_index = Instrument(
            symbol="NIFTY", name="NIFTY", exchange=Exchange.NSE, segment=Segment.CASH,
            asset_class=AssetClass.INDEX, instrument_type=InstrumentType.INDEX, token="26000",
        )
        master = SimpleNamespace(
            by_underlying=lambda u, seg=None: opts,
            nearest_expiry=lambda u, today, segment=None: expiry,
            get=lambda sym: nifty_index,
        )

        def gc(inst, a, b, tf):
            sym = getattr(inst, "symbol", "")
            if sym == "INDIAVIX":
                return [SimpleNamespace(close=v) for v in (vix_history or [])]
            return [SimpleNamespace(close=atm_premium)]

        rt = SimpleNamespace(
            instrument_master=master,
            live_feed=SimpleNamespace(latest_tick=lambda u: SimpleNamespace(last_price=24000.0)),
            price_service=SimpleNamespace(
                resolve=lambda *a, **k: SimpleNamespace(price=None, source=None, is_stale=True)
            ),
            angel_one_history=SimpleNamespace(get_candles=gc),
            decision_pipeline=SimpleNamespace(
                _fetch_bars=lambda u, start, n: [SimpleNamespace(close=c) for c in (closes or _flat_closes())]
            ),
            portfolio=SimpleNamespace(cash=1_000_000, equity=1_000_000),
        )
        return ShortVolExecutor(rt)

    def test_diagnostic_fields_present_and_gate_off_by_default(self):
        ex = self._executor(vix_history=[float(v) for v in range(10, 30)])  # 20 obs, enough
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHORTVOL_MIN_IV_RANK", None)
            plan = ex.build("NIFTY")
        self.assertTrue(plan["enter"], plan.get("reason"))
        self.assertIsNotNone(plan["iv_rank"])
        self.assertIsNotNone(plan["iv_percentile"])
        self.assertEqual(plan["iv_rank_lookback_n"], 20)

    def test_insufficient_history_is_diagnostic_only_when_gate_disabled(self):
        ex = self._executor(vix_history=[])  # no VIX history at all
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHORTVOL_MIN_IV_RANK", None)
            plan = ex.build("NIFTY")
        self.assertTrue(plan["enter"], plan.get("reason"))  # VRP alone still governs
        self.assertIsNone(plan["iv_rank"])
        self.assertEqual(plan["iv_rank_lookback_n"], 0)

    def test_gate_declines_when_history_insufficient_and_gate_enabled(self):
        ex = self._executor(vix_history=[])
        with mock.patch.dict(os.environ, {"SHORTVOL_MIN_IV_RANK": "50"}):
            plan = ex.build("NIFTY")
        self.assertFalse(plan["enter"])
        self.assertIn("insufficient IV history", plan["reason"])

    def test_gate_declines_when_rank_below_threshold(self):
        # Current IV (~18.8 from atm_premium=250, see module math) sits near
        # the LOW end of a history that mostly ranges much higher.
        ex = self._executor(vix_history=[float(v) for v in range(15, 60)])
        with mock.patch.dict(os.environ, {"SHORTVOL_MIN_IV_RANK": "80"}):
            plan = ex.build("NIFTY")
        self.assertFalse(plan["enter"])
        self.assertIn("IV rank", plan["reason"])
        self.assertIn("required 80", plan["reason"])

    def test_gate_allows_when_rank_meets_threshold(self):
        # History capped low so current IV sits at/near the lookback high.
        ex = self._executor(vix_history=[float(v) for v in range(5, 15)] * 2)
        with mock.patch.dict(os.environ, {"SHORTVOL_MIN_IV_RANK": "50"}):
            plan = ex.build("NIFTY")
        self.assertTrue(plan["enter"], plan.get("reason"))
        self.assertGreaterEqual(plan["iv_rank"], 50.0)

    def test_iv_rank_history_uses_vix_for_nifty(self):
        ex = self._executor(vix_history=[12.0, 13.0])
        history = ex._iv_rank_history("NIFTY")
        self.assertEqual(history, [12.0, 13.0])

    def test_iv_rank_history_uses_chain_collector_for_other_underlyings(self):
        rt = SimpleNamespace(
            options_chain_collector=SimpleNamespace(
                atm_iv_history=lambda underlying, lookback_days=365: [11.0, 14.0]
            )
        )
        ex = ShortVolExecutor(rt)
        self.assertEqual(ex._iv_rank_history("BANKNIFTY"), [11.0, 14.0])

    def test_iv_rank_history_empty_when_collector_absent(self):
        ex = ShortVolExecutor(SimpleNamespace())
        self.assertEqual(ex._iv_rank_history("BANKNIFTY"), [])


if __name__ == "__main__":
    unittest.main()
