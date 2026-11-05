"""Phase-3 market-data: MCX commodity-futures ingest, continuous back-adjusted
futures, point-in-time corporate-action adjustment, and the research screens
rewired off Angel One onto MarketDataStore.

SQLite-DB style, like test_market_store_phase2.py. ADDITIVE only — nothing here
touches index_vol_premium or any live-order path, and raw eod_ohlcv is never
mutated (adjustments are computed in the store). Fixtures are synthetic test
data built to the CONFIRMED real MCX / NSE schemas; no exchange data is
fabricated and presented as real.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from trading_platform.data.persistence import TradingDatabase
from trading_platform.data.ingest import mcx_bhavcopy, corporate_actions as ca
from trading_platform.data.ingest.bhavcopy import make_instrument_id
from trading_platform.data.market_store import MarketDataStore


def _ms(d: date) -> str:
    """An MCX-style /Date(ms)/ stamp for calendar date d (UTC midnight)."""
    ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    return f"/Date({ts})/"


def _bdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ──────────────────────────────────────────────────────────────────────────
# MCX bhavcopy parse + ingest
# ──────────────────────────────────────────────────────────────────────────

def _mcx_fixture() -> dict:
    """A small MCX GetDateWiseBhavCopy payload in the confirmed real schema:
    {"d": {"Data": [ {row}, ... ]}}. FUTCOM rows for the seven target
    commodities (Close>0) plus one OPTCOM and one FUTIDX row that must be
    skipped."""
    td, exp = "2026-11-04", _ms(date(2026, 11, 27))
    futcom = []
    for i, sym in enumerate(("GOLD", "SILVER", "CRUDEOIL", "NATURALGAS",
                             "COPPER", "ZINC", "NICKEL")):
        base = 1000.0 + 100 * i
        futcom.append({
            "__type": "x", "Date": _ms(date(2026, 11, 4)), "Symbol": sym,
            "InstrumentName": "FUTCOM", "ExpiryDate": exp, "StrikePrice": 0.0,
            "OptionType": "", "Open": base, "High": base + 5, "Low": base - 5,
            "Close": base + 2, "PreviousClose": base, "Volume": 1234 + i,
            "Value": 99.0, "OpenInterest": 500 + i,
        })
    skip = [
        {"__type": "x", "Date": _ms(date(2026, 11, 4)), "Symbol": "GOLD",
         "InstrumentName": "OPTCOM", "ExpiryDate": exp, "StrikePrice": 100000.0,
         "OptionType": "CE", "Open": 1, "High": 1, "Low": 1, "Close": 1,
         "PreviousClose": 1, "Volume": 1, "Value": 1, "OpenInterest": 1},
        {"__type": "x", "Date": _ms(date(2026, 11, 4)), "Symbol": "BULLDEX",
         "InstrumentName": "FUTIDX", "ExpiryDate": exp, "StrikePrice": 0.0,
         "OptionType": "", "Open": 2, "High": 2, "Low": 2, "Close": 2,
         "PreviousClose": 2, "Volume": 2, "Value": 2, "OpenInterest": 2},
    ]
    return {"d": {"Data": futcom + skip}}


class MCXParseTests(unittest.TestCase):
    def test_parse_keeps_only_futcom_with_positive_settle(self):
        parsed = mcx_bhavcopy.parse_mcx_bhavcopy(_mcx_fixture())
        self.assertEqual(parsed.trade_date, date(2026, 11, 4))
        self.assertEqual(len(parsed.ohlcv), 7)   # 7 FUTCOM, OPTCOM+FUTIDX dropped
        self.assertTrue(all(r.instrument_type == "COMMODITY_FUT" for r in parsed.ohlcv))
        self.assertTrue(all(r.segment == "FUT" for r in parsed.ohlcv))
        self.assertTrue(all(r.settle and r.settle > 0 for r in parsed.ohlcv))
        self.assertTrue(all(r.close == r.settle for r in parsed.ohlcv))  # settle := close
        self.assertEqual({r.expiry for r in parsed.ohlcv}, {date(2026, 11, 27)})
        self.assertEqual({r.tckr_symbol for r in parsed.ohlcv},
                         {"GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "NICKEL"})

    def test_parse_accepts_bare_data_list_and_iso_dates(self):
        rows = [{"Symbol": "GOLD", "InstrumentName": "FUTCOM",
                 "Date": "2026-11-04", "ExpiryDate": "2026-11-27",
                 "Open": 1, "High": 2, "Low": 1, "Close": 1500.5, "OpenInterest": 9}]
        parsed = mcx_bhavcopy.parse_mcx_bhavcopy(rows)
        self.assertEqual(len(parsed.ohlcv), 1)
        self.assertEqual(parsed.ohlcv[0].expiry, date(2026, 11, 27))
        self.assertEqual(parsed.ohlcv[0].settle, 1500.5)


class MCXIngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_ingest_populates_eod_ohlcv_commodity_fut(self):
        counts = mcx_bhavcopy.ingest_day(self.db, date(2026, 11, 4), raw_json=_mcx_fixture())
        self.assertEqual(counts["eod_ohlcv"], 7)
        self.assertEqual(counts["eod_option_quote"], 0)
        store = MarketDataStore(db=self.db, hist_dir=Path(self._tmp.name) / "none")
        curve = store.futures_curve("GOLD")
        self.assertIn(date(2026, 11, 27), curve)
        self.assertGreater(curve[date(2026, 11, 27)][date(2026, 11, 4)]["close"], 0)

    def test_ingest_is_idempotent_and_labels_source(self):
        raw = _mcx_fixture()
        mcx_bhavcopy.ingest_day(self.db, date(2026, 11, 4), raw_json=raw)
        mcx_bhavcopy.ingest_day(self.db, date(2026, 11, 4), raw_json=raw)  # replay
        rows = self.db.eod_ohlcv_futures("SILVER")
        self.assertEqual(len(rows), 1)  # one contract-day, not doubled
        # source is labeled mcx_bhavcopy, instrument_type COMMODITY_FUT
        with self.db._cursor() as cur:
            cur.execute("SELECT DISTINCT source FROM eod_ohlcv")
            self.assertEqual({r[0] for r in cur.fetchall()}, {"mcx_bhavcopy"})
            cur.execute("SELECT DISTINCT instrument_type FROM instrument_master")
            self.assertEqual({r[0] for r in cur.fetchall()}, {"COMMODITY_FUT"})


# ──────────────────────────────────────────────────────────────────────────
# Continuous back-adjusted futures
# ──────────────────────────────────────────────────────────────────────────

def _seed_future(db, underlying, expiry, dates, close_fn, oi_fn,
                 itype="COMMODITY_FUT", seg="FUT", exch="MCX"):
    iid = make_instrument_id(exch, "FUT", underlying, expiry)
    db.upsert_instrument(instrument_id=iid, exchange=exch, segment=seg,
                         instrument_type=itype, tckr_symbol=underlying,
                         underlying=underlying, expiry=expiry, seen_date=dates[0])
    for i, d in enumerate(dates):
        db.upsert_eod_ohlcv(trade_date=d, instrument_id=iid, close=close_fn(i),
                            settle=close_fn(i), oi=oi_fn(i), volume=100, source="test")


class ContinuousFutureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")
        self.store = MarketDataStore(db=self.db, hist_dir=Path(self._tmp.name) / "none")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_oi_roll_back_adjusts_and_never_returns_across_roll(self):
        dates = _bdays(date(2026, 1, 1), 60)
        e1, e2 = date(2026, 3, 31), date(2026, 4, 30)
        # front E1 close = 1000+i ; far E2 = E1 + 50 (constant contango -> a 50 gap)
        _seed_future(self.db, "GOLD", e1, dates, lambda i: 1000.0 + i,
                     oi_fn=lambda i: 1000.0)
        _seed_future(self.db, "GOLD", e2, dates, lambda i: 1050.0 + i,
                     oi_fn=lambda i: 500.0 if i < 40 else 2000.0)  # E2 dominates OI at i>=40

        cf = self.store.continuous_future("GOLD", roll="oi")
        self.assertEqual(len(cf.rolls), 1)
        roll = cf.rolls[0]
        self.assertEqual(roll.from_expiry, e1)
        self.assertEqual(roll.to_expiry, e2)
        self.assertEqual(roll.roll_date, dates[40])
        self.assertTrue(roll.has_overlap)
        self.assertAlmostEqual(roll.gap, 50.0, places=6)  # E2 - E1 at the roll date
        self.assertIn(dates[40], cf.roll_dates)

        # Back-adjustment makes the whole series a clean +1/day ramp: the change
        # across the roll is the in-contract daily move (+1), NOT the raw 51-point
        # cross-contract jump. So no return is ever computed across the roll.
        by_day = {b.day: b.close for b in cf.bars}
        self.assertEqual(len(cf.bars), len(dates))
        diffs = [round(by_day[dates[i]] - by_day[dates[i - 1]], 6) for i in range(1, len(dates))]
        self.assertTrue(all(abs(x - 1.0) < 1e-6 for x in diffs), diffs[:45])
        # explicitly: the day crossing the roll moved +1, not +51
        self.assertAlmostEqual(by_day[dates[40]] - by_day[dates[39]], 1.0, places=6)

    def test_empty_when_no_futures(self):
        cf = self.store.continuous_future("NOSUCH")
        self.assertEqual(cf.bars, [])
        self.assertEqual(cf.rolls, [])


# ──────────────────────────────────────────────────────────────────────────
# Corporate actions: table + point-in-time back-adjustment
# ──────────────────────────────────────────────────────────────────────────

class CorporateActionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")
        self.store = MarketDataStore(db=self.db, hist_dir=Path(self._tmp.name) / "none")
        iid = make_instrument_id("NSE", "CASH", "ACME")
        self.db.upsert_instrument(instrument_id=iid, exchange="NSE", segment="CASH",
                                  instrument_type="EQ", tckr_symbol="ACME",
                                  seen_date=date(2026, 1, 5))
        for d, c in [(date(2026, 1, 5), 1000.0), (date(2026, 1, 6), 1010.0),
                     (date(2026, 1, 7), 505.0), (date(2026, 1, 8), 510.0)]:
            self.db.upsert_eod_ohlcv(trade_date=d, instrument_id=iid, close=c, source="test")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_table_exists_and_upsert_idempotent(self):
        self.db.upsert_corporate_action(symbol="ACME", ex_date=date(2026, 1, 7),
                                        action_type="SPLIT", ratio_num=2, ratio_den=1,
                                        raw_text="2:1 split", source="test")
        self.db.upsert_corporate_action(symbol="ACME", ex_date=date(2026, 1, 7),
                                        action_type="SPLIT", ratio_num=2, ratio_den=1,
                                        raw_text="2:1 split", source="test")  # replay
        rows = self.db.corporate_actions("ACME")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action_type"], "SPLIT")
        self.assertEqual(rows[0]["ex_date"], date(2026, 1, 7))

    def test_point_in_time_adjustment(self):
        # 2:1 split, ex-date 2026-01-07 (price halves). num=2, den=1 -> factor 0.5.
        self.db.upsert_corporate_action(symbol="ACME", ex_date=date(2026, 1, 7),
                                        action_type="SPLIT", ratio_num=2, ratio_den=1,
                                        raw_text="2:1 split", source="test")

        # as_of AFTER the ex-date: pre-ex bars scaled onto the post-split scale.
        adj = {b.day: b.close for b in self.store.adjusted_closes("ACME", as_of=date(2026, 1, 10))}
        self.assertAlmostEqual(adj[date(2026, 1, 5)], 500.0, places=6)   # 1000 * 0.5
        self.assertAlmostEqual(adj[date(2026, 1, 6)], 505.0, places=6)   # 1010 * 0.5
        self.assertAlmostEqual(adj[date(2026, 1, 7)], 505.0, places=6)   # on/after ex-date: raw
        self.assertAlmostEqual(adj[date(2026, 1, 8)], 510.0, places=6)

        # as_of BEFORE the ex-date: the split "has not happened" -> raw, unadjusted.
        raw = {b.day: b.close for b in self.store.adjusted_closes("ACME", as_of=date(2026, 1, 6))}
        self.assertAlmostEqual(raw[date(2026, 1, 5)], 1000.0, places=6)
        self.assertAlmostEqual(raw[date(2026, 1, 6)], 1010.0, places=6)

        # raw eod_ohlcv is never mutated by the adjustment
        plain = {b.day: b.close for b in self.store.daily_closes("ACME")}
        self.assertAlmostEqual(plain[date(2026, 1, 5)], 1000.0, places=6)

    def test_parser_classifies_nse_subjects(self):
        self.assertEqual(ca.classify_subject(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"),
            ("SPLIT", 10.0, 2.0))
        self.assertEqual(ca.classify_subject("Bonus 1:1"), ("BONUS", 2.0, 1.0))
        self.assertEqual(ca.classify_subject("Dividend - Rs 5 Per Share")[0], "DIV")
        self.assertEqual(ca.classify_subject("Annual General Meeting")[0], "OTHER")

    def test_parser_to_store_end_to_end(self):
        rows = [{"symbol": "ACME", "exDate": "07-Jan-2026",
                 "subject": "Face Value Split (Sub-Division) - From Rs 10/- To Rs 5/-"}]
        n = ca.ingest(self.db, rows=rows, source="test")
        self.assertEqual(n, 1)
        # 10->5 face-value split: factor den/num = 5/10 = 0.5 applied pre-ex.
        adj = {b.day: b.close for b in self.store.adjusted_closes("ACME", as_of=date(2026, 1, 10))}
        self.assertAlmostEqual(adj[date(2026, 1, 5)], 500.0, places=6)

    def test_empty_adapter_is_a_noop(self):
        self.assertEqual(ca.ingest(self.db), 0)  # no rows -> no fabricated actions


# ──────────────────────────────────────────────────────────────────────────
# Research screens rewired off Angel One -> MarketDataStore
# ──────────────────────────────────────────────────────────────────────────

class ResearchRewireTests(unittest.TestCase):
    """Each screen runs end-to-end against a seeded store (no Angel One) and
    returns its verdict shape: an int exit code in its defined set {0,1,2}."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")
        self.store = MarketDataStore(db=self.db, hist_dir=Path(self._tmp.name) / "none")
        self._seed()

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed(self):
        import math
        dates = _bdays(date(2026, 1, 1), 200)
        # Commodity multi-maturity curves (carry) + front months (pairs).
        for ci, sym in enumerate(("GOLD", "SILVER", "CRUDEOIL")):
            for k, exp in enumerate((date(2026, 9, 30), date(2026, 10, 30), date(2026, 11, 30))):
                base = 1000.0 + 500 * ci + 20 * k
                _seed_future(self.db, sym, exp, dates,
                             close_fn=lambda i, b=base: b + math.sin(i / 7.0) * 5 + i * 0.1,
                             oi_fn=lambda i, k=k: 1000.0 + k * 10 + i)
        # Index spot + modeled iv30 (vol_premium) and index near-month futures (basis).
        for sym in ("NIFTY", "BANKNIFTY"):
            self.db.upsert_instrument(instrument_id=f"NSE:INDEX:{sym}", exchange="NSE",
                                      segment="INDEX", instrument_type="INDEX",
                                      tckr_symbol=sym, seen_date=dates[0])
            for i, d in enumerate(dates):
                spot = 23000.0 + 50 * math.sin(i / 9.0) + i
                self.db.upsert_index_spot(trade_date=d, index_symbol=sym, close=spot, source="test")
                self.db.upsert_index_atm_iv30(trade_date=d, index_symbol=sym,
                                              atm_iv30=14.0 + 2 * math.sin(i / 11.0), source="test")
            _seed_future(self.db, sym, date(2026, 11, 27), dates,
                         close_fn=lambda i: 23010.0 + 50 * math.sin(i / 9.0) + i,
                         oi_fn=lambda i: 5000.0, itype="FUT", seg="FO", exch="NSE")

    def _run(self, module_name, argv):
        import importlib
        mod = importlib.import_module(module_name)
        import trading_platform.data.market_store as ms_mod
        with mock.patch.object(ms_mod, "MarketDataStore", lambda *a, **k: self.store), \
             mock.patch.object(sys, "argv", argv):
            rc = mod.main()
        self.assertIsInstance(rc, int)
        self.assertIn(rc, (0, 1, 2))

    def test_commodity_carry(self):
        self._run("scripts.research_commodity_carry", ["research_commodity_carry"])

    def test_commodity_pairs(self):
        self._run("scripts.research_commodity_pairs", ["research_commodity_pairs"])

    def test_index_basis(self):
        self._run("scripts.research_index_basis", ["research_index_basis"])

    def test_vol_premium(self):
        self._run("scripts.research_vol_premium", ["research_vol_premium"])

    def test_intraday_commodity_trend(self):
        # EOD store has no intraday series -> insufficient-data verdict (2),
        # but crucially runs with ZERO Angel One.
        import importlib
        mod = importlib.import_module("scripts.research_intraday_commodity_trend")
        import trading_platform.data.market_store as ms_mod
        with mock.patch.object(ms_mod, "MarketDataStore", lambda *a, **k: self.store):
            rc = mod.main()
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
