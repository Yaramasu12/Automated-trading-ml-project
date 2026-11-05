"""Phase-2 unified market-data store: schema, ingestion, checkpoint lease,
the DB-first/CSV-fallback read API, and the historical-CSV backfill.

SQLite-DB style, like the other DB tests (test_learning_runs.py,
test_bse_index_premium.py). ADDITIVE only — these tests never touch
index_vol_premium or any live-order path. The DB-first/CSV-fallback behaviour
is checked both ways so the current validation results are reproducible before
AND after the DB is populated.
"""
from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from trading_platform.data.persistence import TradingDatabase
from trading_platform.data.ingest import nse_bhavcopy
from trading_platform.data.ingest.bhavcopy import parse_udiff_bhavcopy
from trading_platform.data.market_store import MarketDataStore

_UDIFF_HDR = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def _sample_udiff_zip() -> bytes:
    rows = [
        # index option (NIFTY CE) -> eod_option_quote + instrument(OPT)
        "2026-09-18,2026-09-18,FO,NSE,IDO,1,,NIFTY,,2026-09-22,2026-09-22,23350.00,CE,X,"
        "0,0,0,90,90,90,23346.4,91.50,4955600,10,100,0,1,F1,65,,,,,",
        # index option (NIFTY PE) same strike
        "2026-09-18,2026-09-18,FO,NSE,IDO,2,,NIFTY,,2026-09-22,2026-09-22,23350.00,PE,X,"
        "0,0,0,88,88,88,23346.4,88.00,3000000,5,90,0,1,F1,65,,,,,",
        # stock option (RELIANCE PE) -> instrument(OPT) + option quote
        "2026-09-18,2026-09-18,FO,NSE,STO,3,,RELIANCE,,2026-09-29,2026-09-29,1400.00,PE,Y,"
        "0,0,0,64,64,64,1410,69.24,6200,-5,50,0,0,F1,250,,,,,",
        # index future (NIFTY) -> eod_ohlcv + instrument(FUT)
        "2026-09-18,2026-09-18,FO,NSE,IDF,4,,NIFTY,,2026-09-22,2026-09-22,0,,F,"
        "23300,23400,23250,23346,23346,23300,23346,23346,120000,3,5000,0,0,F1,65,,,,,",
        # cash equity (RELIANCE EQ, no expiry) -> eod_ohlcv + instrument(EQ)
        "2026-09-18,2026-09-18,CM,NSE,,5,,RELIANCE,EQ,,,0,,REL,"
        "1400,1420,1395,1410,1410,1398,0,0,0,0,900000,0,0,F1,1,,,,,",
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_20260918.csv", "\n".join([_UDIFF_HDR] + rows))
    return buf.getvalue()


def _write_csv(path: Path, header: str, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header] + lines) + "\n", encoding="utf-8")


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.db"
        self.db = TradingDatabase(db_path=self.path)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _objects(self, kind):
        con = sqlite3.connect(str(self.path))
        try:
            return {r[0] for r in con.execute(
                f"SELECT name FROM sqlite_master WHERE type='{kind}'")}
        finally:
            con.close()

    def test_all_new_tables_exist(self):
        tables = self._objects("table")
        for t in ("instrument_master", "eod_ohlcv", "eod_option_quote",
                  "index_atm_iv30", "index_spot", "ingestion_runs"):
            self.assertIn(t, tables, t)

    def test_existing_tables_untouched(self):
        # index_vol_premium (and the rest of the live schema) must still exist.
        self.assertIn("index_vol_premium", self._objects("table"))

    def test_indexes_exist(self):
        idxs = self._objects("index")
        for i in ("idx_eod_ohlcv_instr", "idx_eod_optq_underlying",
                  "idx_index_spot_sym", "idx_index_iv30_sym"):
            self.assertIn(i, idxs, i)

    def test_idempotent_reapply(self):
        # Re-opening the same file re-runs the IF NOT EXISTS DDL harmlessly.
        TradingDatabase(db_path=self.path).close()
        self.assertIn("eod_ohlcv", self._objects("table"))


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_parse_classifies_rows(self):
        p = parse_udiff_bhavcopy(_sample_udiff_zip(), "NSE")
        self.assertEqual(p.trade_date, date(2026, 9, 18))
        self.assertEqual({o.option_type for o in p.options}, {"CE", "PE"})
        self.assertEqual({o.instrument_type for o in p.ohlcv}, {"FUT", "EQ"})

    def test_ingest_populates_three_tables(self):
        counts = nse_bhavcopy.ingest_day(self.db, date(2026, 9, 18),
                                         raw_zip=_sample_udiff_zip())
        self.assertEqual(counts["eod_option_quote"], 3)   # NIFTY CE/PE + RELIANCE PE
        self.assertEqual(counts["eod_ohlcv"], 2)          # NIFTY FUT + RELIANCE EQ
        self.assertEqual(counts["instruments"], 5)

        con = sqlite3.connect(str(Path(self._tmp.name) / "t.db"))
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM instrument_master").fetchone()[0], 5)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM eod_ohlcv").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM eod_option_quote").fetchone()[0], 3)
            # raw traded prices are stored; modeled IV is NOT written at ingest
            # (never conflate a traded price with a modeled IV).
            ivs = [r[0] for r in con.execute("SELECT implied_vol FROM eod_option_quote")]
            self.assertTrue(all(v is None for v in ivs))
            # the cash equity's close is the raw bhavcopy value
            close = con.execute(
                "SELECT eo.close FROM eod_ohlcv eo JOIN instrument_master im "
                "ON eo.instrument_id=im.instrument_id WHERE im.tckr_symbol='RELIANCE' "
                "AND im.segment='CASH'").fetchone()[0]
            self.assertAlmostEqual(close, 1410.0, places=4)
        finally:
            con.close()

    def test_ingest_is_idempotent(self):
        raw = _sample_udiff_zip()
        nse_bhavcopy.ingest_day(self.db, date(2026, 9, 18), raw_zip=raw)
        nse_bhavcopy.ingest_day(self.db, date(2026, 9, 18), raw_zip=raw)  # replay
        con = sqlite3.connect(str(Path(self._tmp.name) / "t.db"))
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM eod_option_quote").fetchone()[0], 3)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM eod_ohlcv").fetchone()[0], 2)
        finally:
            con.close()


class IngestionRunClaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_claim_or_skip(self):
        first = self.db.claim_ingestion_run("nse_fo", date(2026, 11, 4), lease_owner="p1")
        self.assertIsNotNone(first)
        self.assertEqual(first["state"], "RUNNING")
        # a second worker racing the same (source, day) is refused
        self.assertIsNone(self.db.claim_ingestion_run("nse_fo", date(2026, 11, 4), lease_owner="p2"))

    def test_claim_after_done_is_refused(self):
        run = self.db.claim_ingestion_run("nse_fo", date(2026, 11, 4), lease_owner="p1")
        self.db.update_ingestion_run(run["id"], "DONE", rows=42, detail="ok")
        self.assertIsNone(self.db.claim_ingestion_run("nse_fo", date(2026, 11, 4), lease_owner="p2"))
        rows = self.db.recent_ingestion_runs("nse_fo")
        self.assertEqual(rows[0]["state"], "DONE")
        self.assertEqual(rows[0]["rows"], 42)

    def test_different_source_or_day_do_not_collide(self):
        a = self.db.claim_ingestion_run("nse_fo", date(2026, 11, 4), lease_owner="p1")
        b = self.db.claim_ingestion_run("bse_fo", date(2026, 11, 4), lease_owner="p1")
        c = self.db.claim_ingestion_run("nse_fo", date(2026, 11, 5), lease_owner="p1")
        self.assertEqual(len({a["id"], b["id"], c["id"]}), 3)


class MarketStoreReadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hist = Path(self._tmp.name) / "historical"
        self.hist.mkdir(parents=True)
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")
        self.store = MarketDataStore(db=self.db, hist_dir=self.hist)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_daily_closes_csv_fallback_then_db_first(self):
        _write_csv(
            self.hist / "SENSEX__ONE_DAY_deep.csv",
            "timestamp,open,high,low,close,volume",
            ["2026-01-01T00:00:00+05:30,100,100,100,100,0",
             "2026-01-02T00:00:00+05:30,101,101,101,101,0"],
        )
        # empty DB -> CSV fallback
        csv_bars = self.store.daily_closes("SENSEX")
        self.assertEqual([b.close for b in csv_bars], [100.0, 101.0])

        # populate DB with DIFFERENT values -> DB-first must win
        self.db.upsert_instrument(instrument_id="BSE:INDEX:SENSEX", exchange="BSE",
                                  segment="INDEX", instrument_type="INDEX",
                                  tckr_symbol="SENSEX", seen_date=date(2026, 1, 1))
        self.db.upsert_index_spot(trade_date=date(2026, 1, 1), index_symbol="SENSEX", close=200.0)
        self.db.upsert_index_spot(trade_date=date(2026, 1, 2), index_symbol="SENSEX", close=201.0)
        db_bars = self.store.daily_closes("SENSEX")
        self.assertEqual([b.close for b in db_bars], [200.0, 201.0])

    def test_iv30_csv_fallback_then_db_first(self):
        _write_csv(
            self.hist / "SENSEX__IV30_deep.csv",
            "timestamp,open,high,low,close,volume",
            ["2026-01-01T00:00:00+05:30,13.5,13.5,13.5,13.5,0"],
        )
        self.assertEqual(self.store.iv30("SENSEX"), {date(2026, 1, 1): 13.5})
        self.db.upsert_index_atm_iv30(trade_date=date(2026, 1, 1), index_symbol="SENSEX",
                                      atm_iv30=17.25)
        self.assertEqual(self.store.iv30("SENSEX"), {date(2026, 1, 1): 17.25})

    def test_equity_daily_closes_from_eod_ohlcv(self):
        self.db.upsert_instrument(instrument_id="NSE:CASH:RELIANCE", exchange="NSE",
                                  segment="CASH", instrument_type="EQ",
                                  tckr_symbol="RELIANCE", seen_date=date(2026, 1, 1))
        self.db.upsert_eod_ohlcv(trade_date=date(2026, 1, 1), instrument_id="NSE:CASH:RELIANCE",
                                 close=1400.0, source="t")
        bars = self.store.daily_closes("RELIANCE")
        self.assertEqual([(b.day, b.close) for b in bars], [(date(2026, 1, 1), 1400.0)])

    def test_csv_path_for_matches_old_resolution(self):
        _write_csv(self.hist / "TCS__ONE_DAY.csv",
                   "timestamp,open,high,low,close,volume",
                   ["2026-01-01T00:00:00+05:30,1,1,1,1,0"])
        self.assertEqual(self.store.csv_path_for("TCS"), self.hist / "TCS__ONE_DAY.csv")
        self.assertIsNone(self.store.csv_path_for("NOSUCH"))


class BackfillTests(unittest.TestCase):
    """End-to-end: historical CSVs -> DB tables, idempotently, with iv30 and
    daily_closes readable back DB-first afterward."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hist = Path(self._tmp.name) / "historical"
        self.hist.mkdir(parents=True)
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_hist(self):
        _write_csv(self.hist / "NIFTY__ONE_DAY_deep.csv",
                   "timestamp,open,high,low,close,volume",
                   ["2024-07-08T00:00:00+05:30,24000,24100,23900,24010,0"])
        _write_csv(self.hist / "RELIANCE__ONE_DAY.csv",
                   "timestamp,open,high,low,close,volume",
                   ["2024-07-08T00:00:00+05:30,2900,2920,2890,2910,100"])
        fields = ("date,index,expiry,dte,strike,forward,spot_parity,call,put,iv,"
                  "iv_put95,iv_call105,vol_ce,vol_pe,oi_ce,oi_pe,lot,undrlyg")
        rows = [
            "2024-07-08,NIFTY,2024-07-15,7,24000,24010,24000,120,110,0.14,0.16,0.13,10,9,100,90,65,24010",
            "2024-07-08,NIFTY,2024-08-12,35,24000,24020,24005,260,250,0.18,0.20,0.17,5,4,50,40,65,24010",
        ]
        _write_csv(self.hist / "iv" / "atm_by_expiry.csv", fields, rows)

    def test_backfill_populates_all_tables_and_reads_back(self):
        import scripts.backfill_market_store as bf
        self._seed_hist()
        orig_hist = bf.HIST
        bf.HIST = self.hist
        try:
            bar_counts, index_close = bf.backfill_bars(self.db)
            opt_counts = bf.backfill_options(self.db, index_close)
        finally:
            bf.HIST = orig_hist

        self.assertEqual(bar_counts["index_spot"], 1)     # NIFTY
        self.assertEqual(bar_counts["eod_ohlcv"], 1)      # RELIANCE
        self.assertEqual(opt_counts["eod_option_quote"], 4)   # 2 expiries x CE/PE
        self.assertGreaterEqual(opt_counts["index_atm_iv30"], 1)

        store = MarketDataStore(db=self.db, hist_dir=self.hist)
        # index spot read back DB-first
        self.assertEqual([b.close for b in store.daily_closes("NIFTY")], [24010.0])
        # equity read back DB-first
        self.assertEqual([b.close for b in store.daily_closes("RELIANCE")], [2910.0])
        # modeled iv30 interpolated strictly between the 14% and 18% legs
        iv = store.iv30("NIFTY")
        self.assertIn(date(2024, 7, 8), iv)
        self.assertTrue(14.0 < iv[date(2024, 7, 8)] < 18.0)
        # option quotes carry the raw traded price AND the labeled modeled IV
        quotes = store.option_quotes("NIFTY", expiry=date(2024, 7, 15))
        self.assertEqual(len(quotes), 2)
        self.assertEqual({q["iv_method"] for q in quotes}, {"bhavcopy_b76_atm"})

    def test_backfill_is_idempotent(self):
        import scripts.backfill_market_store as bf
        self._seed_hist()
        orig_hist = bf.HIST
        bf.HIST = self.hist
        try:
            bf.backfill_bars(self.db)
            _, index_close = bf.backfill_bars(self.db)   # replay
            bf.backfill_options(self.db, index_close)
            bf.backfill_options(self.db, index_close)    # replay
        finally:
            bf.HIST = orig_hist
        con = sqlite3.connect(str(Path(self._tmp.name) / "t.db"))
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM index_spot").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM eod_option_quote").fetchone()[0], 4)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
