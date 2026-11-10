"""Phase 4 — real-traded-leg fidelity upgrade.

Three things, SQLite-DB style like test_market_store_phase2.py, ADDITIVE only
(never touches index_vol_premium or any live-order path):

  1. FULL-CHAIN INGEST — bhavcopy.persist_bhavcopy writes EVERY strike/expiry of
     an index option into eod_option_quote (not just ATM), and the opt-in
     Phase-4 scoping filters (underlyings / strike_band_pct / skip_iv30) keep a
     multi-year backfill tractable without ever mutating a raw column.
  2. TRADED-LEG PRICING — ShortVolBacktester(leg_pricing='traded') prices each
     leg from the real eod_option_quote settle/close for its own
     (trade_date, expiry, strike, CE/PE), and falls back to a Black-Scholes
     model leg — LABELLED — when a quote is absent. Model mode stays untouched.
  3. COVERAGE ACCOUNTING — the traded-vs-modeled leg coverage the validator
     reports is counted correctly over transacted (entry+exit) legs.
"""
from __future__ import annotations

import io
import math
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from trading_platform.data.persistence import TradingDatabase
from trading_platform.data.ingest import nse_bhavcopy
from trading_platform.backtesting.short_vol_backtest import (
    DailyBar,
    ShortVolBacktester,
    TradedQuoteBook,
    black_scholes,
    smile_iv,
)
from trading_platform.strategies.short_vol import CondorLegSpec, ShortVolStrategy
from trading_platform.domain.enums import OptionType, Side

_UDIFF_HDR = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def _opt_row(sym, expiry, strike, ot, close, settle, undrlyg):
    return (f"2024-07-08,2024-07-08,FO,NSE,IDO,1,,{sym},,{expiry},{expiry},"
            f"{strike:.2f},{ot},X,0,0,0,{close},{close},{close},{undrlyg},{settle},"
            f"100,0,100,0,1,F1,65,,,,,")


def _chain_zip():
    """A NIFTY chain (one expiry, five strikes CE+PE, underlying 24000) plus a
    far-OTM strike outside a +/-10% band and a SENSEX contract — enough to test
    full-chain persistence AND every scoping filter."""
    exp = "2024-07-15"
    rows = []
    for k in (23000, 23500, 24000, 24500, 25000):
        rows.append(_opt_row("NIFTY", exp, k, "CE", 100, 101, 24000))
        rows.append(_opt_row("NIFTY", exp, k, "PE", 90, 91, 24000))
    rows.append(_opt_row("NIFTY", exp, 30000, "CE", 2, 2, 24000))   # outside +/-10%
    rows.append(_opt_row("SENSEX", exp, 80000, "CE", 120, 121, 80000))  # other underlying
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_20240708.csv", "\n".join([_UDIFF_HDR] + rows))
    return buf.getvalue()


class FullChainIngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.db"
        self.db = TradingDatabase(db_path=self.path)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _con(self):
        return sqlite3.connect(str(self.path))

    def test_persists_every_strike_not_just_atm(self):
        counts = nse_bhavcopy.ingest_day(self.db, date(2024, 7, 8), raw_zip=_chain_zip())
        # 5 strikes x CE/PE + 1 far NIFTY CE + 1 SENSEX CE = 12 option rows
        self.assertEqual(counts["eod_option_quote"], 12)
        con = self._con()
        try:
            n_nifty_strikes = con.execute(
                "SELECT COUNT(DISTINCT strike) FROM eod_option_quote "
                "WHERE underlying='NIFTY' AND expiry='2024-07-15'").fetchone()[0]
            self.assertEqual(n_nifty_strikes, 6)   # full chain, not one ATM row
            # raw traded prices stored verbatim; modeled IV left NULL
            settle = con.execute(
                "SELECT settle FROM eod_option_quote WHERE underlying='NIFTY' "
                "AND strike=24000 AND option_type='CE'").fetchone()[0]
            self.assertAlmostEqual(settle, 101.0, places=4)
            ivs = [r[0] for r in con.execute("SELECT implied_vol FROM eod_option_quote")]
            self.assertTrue(all(v is None for v in ivs))
        finally:
            con.close()

    def test_scoping_filters_underlyings_band_and_skip_iv30(self):
        counts = nse_bhavcopy.ingest_day(
            self.db, date(2024, 7, 8), raw_zip=_chain_zip(),
            underlyings=["NIFTY"], strike_band_pct=0.10, skip_iv30=True)
        con = self._con()
        try:
            unds = {r[0] for r in con.execute("SELECT DISTINCT underlying FROM eod_option_quote")}
            self.assertEqual(unds, {"NIFTY"})                  # SENSEX filtered out
            strikes = {r[0] for r in con.execute("SELECT DISTINCT strike FROM eod_option_quote")}
            self.assertEqual(strikes, {23000.0, 23500.0, 24000.0, 24500.0, 25000.0})  # 30000 banded out
            # skip_iv30 => no modeled IV rows recovered at ingest
            self.assertEqual(con.execute("SELECT COUNT(*) FROM index_atm_iv30").fetchone()[0], 0)
        finally:
            con.close()
        # the counts returned match what was actually written
        self.assertEqual(counts["eod_option_quote"], 10)      # 5 strikes x CE/PE
        self.assertEqual(counts["index_atm_iv30"], 0)

    def test_default_behaviour_unchanged_recovers_iv30(self):
        # No filters => full chain AND the modeled index_atm_iv30 recovery runs
        # exactly as before Phase 4 (so nothing already deployed changes).
        counts = nse_bhavcopy.ingest_day(self.db, date(2024, 7, 8), raw_zip=_chain_zip())
        self.assertGreaterEqual(counts["index_atm_iv30"], 1)


class TradedQuoteBookTests(unittest.TestCase):
    def test_settle_preferred_then_close_and_lookup(self):
        rows = [
            {"trade_date": date(2024, 7, 8), "underlying": "NIFTY",
             "expiry": date(2024, 7, 15), "strike": 24000.0, "option_type": "CE",
             "settle": 101.0, "close": 100.0},
            {"trade_date": date(2024, 7, 8), "underlying": "NIFTY",
             "expiry": date(2024, 7, 15), "strike": 24050.0, "option_type": "PE",
             "settle": 0.0, "close": 88.0},   # settle<=0 -> use close
            {"trade_date": date(2024, 7, 8), "underlying": "NIFTY",
             "expiry": date(2024, 7, 15), "strike": 24100.0, "option_type": "PE",
             "settle": None, "close": None},  # unusable -> dropped
        ]
        book = TradedQuoteBook.from_rows(rows)
        self.assertEqual(book.price(date(2024, 7, 8), "NIFTY", date(2024, 7, 15), 24000, "CE"), 101.0)
        self.assertEqual(book.price(date(2024, 7, 8), "nifty", date(2024, 7, 15), 24050.0, "PE"), 88.0)
        self.assertIsNone(book.price(date(2024, 7, 8), "NIFTY", date(2024, 7, 15), 24100, "PE"))
        self.assertIsNone(book.price(date(2024, 7, 8), "NIFTY", date(2024, 7, 15), 99999, "CE"))
        self.assertEqual(book.expiries_on("NIFTY", date(2024, 7, 8)), (date(2024, 7, 15),))
        # no lookahead: asking a different trade_date returns nothing
        self.assertIsNone(book.price(date(2024, 7, 9), "NIFTY", date(2024, 7, 15), 24000, "CE"))


class TradedLegPricingTests(unittest.TestCase):
    def _bt(self, book):
        bt = ShortVolBacktester(underlying="NIFTY", leg_pricing="traded", quote_book=book)
        bt._cur_expiry = date(2024, 7, 15)
        return bt

    def test_price_leg_uses_quote_when_present(self):
        book = TradedQuoteBook.from_rows([
            {"trade_date": date(2024, 7, 8), "underlying": "NIFTY",
             "expiry": date(2024, 7, 15), "strike": 24000.0, "option_type": "CE",
             "settle": 77.0, "close": 70.0},
        ])
        bt = self._bt(book)
        leg = CondorLegSpec(OptionType.CE, 24000.0, Side.SELL, False)
        px, traded = bt._price_leg(date(2024, 7, 8), leg, 24000.0, 0.14, 7 / 252.0)
        self.assertTrue(traded)
        self.assertEqual(px, 77.0)   # settle, not a model number

    def test_price_leg_falls_back_to_model_when_absent(self):
        bt = self._bt(TradedQuoteBook())   # empty book
        leg = CondorLegSpec(OptionType.PE, 23500.0, Side.SELL, False)
        px, traded = bt._price_leg(date(2024, 7, 8), leg, 24000.0, 0.14, 7 / 252.0)
        self.assertFalse(traded)
        expected = black_scholes(24000.0, 23500.0,
                                 7 / 252.0,
                                 smile_iv(0.14, 24000.0, 23500.0, OptionType.PE, bt.skew_per_10pct),
                                 OptionType.PE)
        self.assertAlmostEqual(px, expected, places=6)

    def test_price_structure_signed_value_and_counts(self):
        # short CE 24000 @100 (SELL) + long CE 24300 @ model (absent) : signed =
        # +100 - model_wing ; counts = 1 traded, 1 modeled.
        book = TradedQuoteBook.from_rows([
            {"trade_date": date(2024, 7, 8), "underlying": "NIFTY",
             "expiry": date(2024, 7, 15), "strike": 24000.0, "option_type": "CE",
             "settle": 100.0, "close": 100.0},
        ])
        bt = self._bt(book)
        legs = [CondorLegSpec(OptionType.CE, 24000.0, Side.SELL, False),
                CondorLegSpec(OptionType.CE, 24300.0, Side.BUY, True)]
        signed, nt, nm = bt._price_structure(date(2024, 7, 8), legs, 24000.0, 0.14, 7 / 252.0)
        self.assertEqual((nt, nm), (1, 1))
        wing = black_scholes(24000.0, 24300.0, 7 / 252.0,
                             smile_iv(0.14, 24000.0, 24300.0, OptionType.CE, bt.skew_per_10pct),
                             OptionType.CE)
        self.assertAlmostEqual(signed, 100.0 - wing, places=6)


def _bars(n=160, start=date(2024, 7, 1)):
    out, d, i = [], start, 0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(DailyBar(d, 24000 + 200 * math.sin(i / 7.0)))
            i += 1
        d += timedelta(days=1)
    return out


def _full_book(bars, expiry, lo=22000, hi=26000, step=50):
    """A book quoting ONE fixed expiry on EVERY bar day, over a wide strike band
    (ATM-peaked premium so a condor earns real credit). Every condor leg finds a
    real quote => 100% traded coverage."""
    rows = []
    for b in bars:
        for k in range(lo, hi + 1, step):
            for ot in ("CE", "PE"):
                intr = max(0.0, (b.close - k) if ot == "CE" else (k - b.close))
                tv = max(2.0, 120.0 * math.exp(-((k - b.close) / (b.close * 0.025)) ** 2 / 2))
                px = round(intr + tv, 2)
                rows.append({"trade_date": b.day, "underlying": "NIFTY", "expiry": expiry,
                             "strike": float(k), "option_type": ot, "settle": px, "close": px})
    return TradedQuoteBook.from_rows(rows)


class TradedCoverageAccountingTests(unittest.TestCase):
    def setUp(self):
        self.bars = _bars()
        self.vix = {b.day: 14.0 for b in self.bars}
        self.expiry = self.bars[-1].day + timedelta(days=7)
        self.strat = lambda: ShortVolStrategy(sd=1.0, min_vrp=0.0, kelly_fraction=0.30,
                                              risk_budget=0.05)

    def _run(self, book, leg_pricing="traded"):
        bt = ShortVolBacktester(underlying="NIFTY", hold_days=5, structure="condor",
                                strategy=self.strat(), wing_pct=0.0125,
                                leg_pricing=leg_pricing, quote_book=book)
        return bt.run(self.bars, self.vix)

    def test_full_coverage_all_legs_traded(self):
        res = self._run(_full_book(self.bars, self.expiry))
        self.assertGreater(len(res.trades), 0)
        self.assertEqual(res.leg_pricing, "traded")
        self.assertGreater(res.traded_legs, 0)
        self.assertEqual(res.modeled_legs, 0)
        self.assertEqual(res.traded_coverage, 1.0)
        self.assertTrue(all(not t.entry_modeled and not t.exit_modeled for t in res.trades))
        self.assertTrue(all(t.expiry == self.expiry for t in res.trades))
        # coverage == traded / (traded + modeled)
        self.assertAlmostEqual(
            res.traded_coverage,
            res.traded_legs / (res.traded_legs + res.modeled_legs), places=9)

    def test_partial_coverage_labels_model_fallback(self):
        # Only a narrow ATM band is quoted -> the condor wings fall back to model.
        book = _full_book(self.bars, self.expiry, lo=23600, hi=24400)
        res = self._run(book)
        self.assertGreater(len(res.trades), 0)
        self.assertGreater(res.modeled_legs, 0)
        self.assertGreater(res.traded_legs, 0)
        self.assertTrue(0.0 < res.traded_coverage < 1.0)
        self.assertTrue(any(t.entry_modeled or t.exit_modeled for t in res.trades))

    def test_empty_book_all_modeled(self):
        res = self._run(TradedQuoteBook())
        self.assertGreater(len(res.trades), 0)
        self.assertEqual(res.traded_legs, 0)
        self.assertGreater(res.modeled_legs, 0)
        self.assertEqual(res.traded_coverage, 0.0)
        self.assertTrue(all(t.entry_modeled for t in res.trades))

    def test_model_mode_has_no_coverage_and_differs_from_traded_credit(self):
        model = self._run(TradedQuoteBook(), leg_pricing="model")
        self.assertEqual(model.leg_pricing, "model")
        self.assertEqual(model.traded_legs, 0)
        self.assertEqual(model.modeled_legs, 0)
        self.assertEqual(model.traded_coverage, 0.0)
        # traded prices drive a DIFFERENT entry credit than the model legs would.
        # The entry DECISION is identical (model-driven in both modes), so trades
        # share entry days; only the credit each leg is worth differs. Trade
        # counts can still diverge because a different credit shifts profit/stop
        # timing, so compare credits on a day BOTH modes opened a trade on.
        traded = self._run(_full_book(self.bars, self.expiry))
        model_by_day = {t.entry_day: t.entry_credit_points for t in model.trades}
        traded_by_day = {t.entry_day: t.entry_credit_points for t in traded.trades}
        shared = set(model_by_day) & set(traded_by_day)
        self.assertTrue(shared, "expected at least one common entry day")
        self.assertTrue(
            any(abs(model_by_day[d] - traded_by_day[d]) > 1e-3 for d in shared),
            "traded-leg credit should differ from the model-leg credit")


if __name__ == "__main__":
    unittest.main()
