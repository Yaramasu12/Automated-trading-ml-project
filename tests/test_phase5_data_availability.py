"""Phase-5: continuous availability + observability.

ADDITIVE, SQLite-only tests (like the other DB tests) covering the three
Phase-5 pieces, with ZERO network — every bhavcopy fetch is mocked:

  1. the scheduler's post-market EOD ingest jobs run and record ingestion_runs
     rows (DONE for a mocked NSE fetch),
  2. the MCX ingest job no-ops GRACEFULLY on a simulated Akamai WAF block —
     records FAILED, never raises, never fabricates,
  3. /data/health (compute_data_health) computes latest / gap / staleness from
     seeded tables + ingestion_runs and writes a risk_events row ONLY when a
     source is stale beyond tolerance,
  4. the paid-vendor adapter returns "not configured" + empty when the env flag
     is off and NEVER fabricates data.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from trading_platform.data import persistence  # noqa: E402
from trading_platform.data.persistence import TradingDatabase  # noqa: E402
from trading_platform.data.data_health import (  # noqa: E402
    STALENESS_EVENT_TYPE,
    compute_data_health,
)
from trading_platform.data.ingest import nse_bhavcopy, mcx_bhavcopy  # noqa: E402
from trading_platform.data.ingest.mcx_bhavcopy import MCXBlockedError  # noqa: E402
from trading_platform.data.ingest import vendor_options  # noqa: E402

import daily_scheduler  # noqa: E402


_UDIFF_HDR = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)


def _sample_nse_zip() -> bytes:
    """A minimal NSE F&O UDiFF bhavcopy: one NIFTY option pair + one NIFTY
    future (index underlyings the scheduled job scopes to)."""
    rows = [
        "2026-09-18,2026-09-18,FO,NSE,IDO,1,,NIFTY,,2026-09-22,2026-09-22,23350.00,CE,X,"
        "0,0,0,90,90,90,23346.4,91.50,4955600,10,100,0,1,F1,65,,,,,",
        "2026-09-18,2026-09-18,FO,NSE,IDO,2,,NIFTY,,2026-09-22,2026-09-22,23350.00,PE,X,"
        "0,0,0,88,88,88,23346.4,88.00,3000000,5,90,0,1,F1,65,,,,,",
        "2026-09-18,2026-09-18,FO,NSE,IDF,4,,NIFTY,,2026-09-22,2026-09-22,0,,F,"
        "23300,23400,23250,23346,23346,23300,23346,23346,120000,3,5000,0,0,F1,65,,,,,",
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_20260918.csv", "\n".join([_UDIFF_HDR] + rows))
    return buf.getvalue()


class _IsolatedDefaultDbMixin:
    """Force a bare TradingDatabase() (how the scheduler jobs construct one) to
    a private temp SQLite file, regardless of a dev shell's DATABASE_URL."""

    def setUp(self):
        # ignore_cleanup_errors: the scheduler jobs construct a bare
        # TradingDatabase() internally (by design) whose SQLite handle outlives
        # the call, so on Windows the temp file can still be locked at teardown.
        # That's a harmless process-local artifact, not a product bug.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = Path(self._tmp.name) / "trading.db"
        self._orig_default = persistence._DEFAULT_DB_PATH
        persistence._DEFAULT_DB_PATH = self._db_path
        self._env = mock.patch.dict("os.environ", {"DATABASE_URL": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        persistence._DEFAULT_DB_PATH = self._orig_default
        self._tmp.cleanup()


class SchedulerEodIngestJobTests(_IsolatedDefaultDbMixin, unittest.TestCase):
    FIXED_NOW = datetime(2026, 11, 5, 19, 0, tzinfo=daily_scheduler.IST)  # a trading day

    def test_nse_ingest_job_records_done_runs(self):
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=self.FIXED_NOW), \
             mock.patch.object(nse_bhavcopy, "fetch", return_value=_sample_nse_zip()) as fetch:
            daily_scheduler.run_nse_eod_ingest()

        self.assertTrue(fetch.called)  # the mocked fetch stood in for the network
        db = TradingDatabase(db_path=self._db_path)
        try:
            runs = db.recent_ingestion_runs("nse_fo")
            self.assertTrue(runs, "expected ingestion_runs rows for nse_fo")
            done = [r for r in runs if r["state"] == "DONE"]
            self.assertTrue(done, "expected at least one DONE nse_fo day")
            self.assertTrue(all((r["rows"] or 0) > 0 for r in done))
            # the scoped underlying actually landed in the store
            h = db.market_source_health("eod_option_quote", "nse_fo")
            self.assertGreater(h["total_rows"], 0)
        finally:
            db.close()

    def test_nse_ingest_is_idempotent_on_replay(self):
        # Running the job twice must not re-ingest DONE days (claim-or-skip).
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=self.FIXED_NOW), \
             mock.patch.object(nse_bhavcopy, "fetch", return_value=_sample_nse_zip()):
            daily_scheduler.run_nse_eod_ingest()
            daily_scheduler.run_nse_eod_ingest()

        db = TradingDatabase(db_path=self._db_path)
        try:
            runs = db.recent_ingestion_runs("nse_fo", limit=100)
            dates = [r["trade_date"] for r in runs]
            self.assertEqual(len(dates), len(set(dates)), "duplicate (source,day) runs created")
        finally:
            db.close()


class SchedulerMcxBlockedJobTests(_IsolatedDefaultDbMixin, unittest.TestCase):
    FIXED_NOW = datetime(2026, 11, 5, 19, 10, tzinfo=daily_scheduler.IST)

    def test_mcx_job_no_ops_gracefully_on_waf_block(self):
        def _blocked(_d, retries=3):
            raise MCXBlockedError("Akamai WAF block page (simulated)")

        with mock.patch.object(daily_scheduler, "_now_ist", return_value=self.FIXED_NOW), \
             mock.patch.object(mcx_bhavcopy, "fetch", side_effect=_blocked):
            # Must NOT raise — the scheduler loop must survive a blocked source.
            daily_scheduler.run_mcx_eod_ingest()

        db = TradingDatabase(db_path=self._db_path)
        try:
            runs = db.recent_ingestion_runs("mcx_bhavcopy")
            self.assertTrue(runs, "expected ingestion_runs rows for mcx_bhavcopy")
            self.assertTrue(all(r["state"] == "FAILED" for r in runs))
            self.assertTrue(any("WAF" in (r["detail"] or "") for r in runs))
            # NEVER fabricate: no commodity rows were written for a blocked day.
            from trading_platform.data.market_store import MarketDataStore  # noqa: F401
            h = db.market_source_health("eod_ohlcv", "mcx_bhavcopy")
            self.assertEqual(h["total_rows"], 0)
        finally:
            db.close()


class DataHealthTests(unittest.TestCase):
    AS_OF = date(2026, 11, 5)  # a trading day

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_ohlcv(self, source, trade_date, *, rows=1):
        for i in range(rows):
            self.db.upsert_eod_ohlcv(
                trade_date=trade_date, instrument_id=f"{source}:{trade_date}:{i}",
                close=100.0 + i, source=source,
            )

    def _nse_entry(self, report, asset_class="ohlcv"):
        for e in report["entries"]:
            if e["source"] == "nse_fo" and e["asset_class"] == asset_class:
                return e
        self.fail(f"no nse_fo/{asset_class} entry in report")

    def test_fresh_source_is_ok_and_writes_no_risk_event(self):
        self._seed_ohlcv("nse_fo", date(2026, 11, 4), rows=3)  # 1 trading day old
        report = compute_data_health(self.db, as_of=self.AS_OF, max_stale_days=4)

        entry = self._nse_entry(report)
        self.assertEqual(entry["status"], "ok")
        self.assertFalse(entry["is_stale"])
        self.assertEqual(entry["latest_trade_date"], "2026-11-04")
        self.assertEqual(entry["rows_on_latest_date"], 3)
        self.assertEqual(entry["stale_trading_days"], 1)
        self.assertFalse(report["stale"])
        self.assertEqual(report["alerts_written"], [])
        self.assertEqual(self.db.recent_risk_events(), [])

    def test_stale_source_writes_exactly_one_risk_event(self):
        self._seed_ohlcv("nse_fo", date(2026, 10, 1))  # ~24 trading days old
        report = compute_data_health(self.db, as_of=self.AS_OF, max_stale_days=4)

        entry = self._nse_entry(report)
        self.assertEqual(entry["status"], "stale")
        self.assertTrue(entry["is_stale"])
        self.assertGreater(entry["stale_trading_days"], 4)
        self.assertTrue(report["stale"])
        self.assertIn("nse_fo", report["stale_sources"])
        self.assertEqual(report["alerts_written"], ["nse_fo"])

        events = self.db.recent_risk_events()
        staleness = [e for e in events if e["event_type"] == STALENESS_EVENT_TYPE]
        self.assertEqual(len(staleness), 1)
        self.assertEqual(staleness[0]["symbol"], "nse_fo")

    def test_staleness_alert_is_deduped_per_day(self):
        self._seed_ohlcv("nse_fo", date(2026, 10, 1))
        compute_data_health(self.db, as_of=self.AS_OF, max_stale_days=4)
        compute_data_health(self.db, as_of=self.AS_OF, max_stale_days=4)  # second poll
        staleness = [e for e in self.db.recent_risk_events()
                     if e["event_type"] == STALENESS_EVENT_TYPE]
        self.assertEqual(len(staleness), 1, "a second poll must not duplicate the alert")

    def test_empty_source_is_no_data_not_stale(self):
        # Nothing seeded at all: every source is 'no_data', nothing is 'stale',
        # and NO alert is written (don't spam about never-configured sources).
        report = compute_data_health(self.db, as_of=self.AS_OF, max_stale_days=4)
        self.assertTrue(report["entries"])
        self.assertTrue(all(e["status"] == "no_data" for e in report["entries"]))
        self.assertFalse(report["stale"])
        self.assertEqual(self.db.recent_risk_events(), [])

    def test_gap_count_vs_trading_calendar(self):
        # Present: 2026-11-03 (Tue) and 2026-11-05 (Thu). 2026-11-04 (Wed) is a
        # trading day with no data -> exactly one gap.
        self._seed_ohlcv("nse_fo", date(2026, 11, 3))
        self._seed_ohlcv("nse_fo", date(2026, 11, 5))
        report = compute_data_health(self.db, as_of=self.AS_OF, max_stale_days=4)
        entry = self._nse_entry(report)
        self.assertEqual(entry["gap_count"], 1)
        self.assertIn("2026-11-04", entry["gap_dates"])
        self.assertEqual(entry["status"], "ok")  # latest is as_of -> fresh

    def test_last_successful_and_failed_runs_surface(self):
        self._seed_ohlcv("nse_fo", date(2026, 11, 4))
        run = self.db.claim_ingestion_run("nse_fo", date(2026, 11, 4), lease_owner="t")
        self.db.update_ingestion_run(run["id"], "DONE", rows=10, detail="ok")
        run2 = self.db.claim_ingestion_run("nse_fo", date(2026, 11, 3), lease_owner="t")
        self.db.update_ingestion_run(run2["id"], "FAILED", rows=0, detail="network error")

        report = compute_data_health(self.db, as_of=self.AS_OF, max_stale_days=4)
        entry = self._nse_entry(report)
        self.assertIsNotNone(entry["last_successful_run"])
        self.assertEqual(entry["last_successful_run"]["state"], "DONE")
        self.assertIsNotNone(entry["last_failed_run"])
        self.assertEqual(entry["last_failed_run"]["state"], "FAILED")


class VendorOptionsAdapterTests(unittest.TestCase):
    def test_disabled_by_default_returns_not_configured_and_empty(self):
        adapter = vendor_options.get_vendor_adapter(env={})
        self.assertIsInstance(adapter, vendor_options.DisabledVendorAdapter)
        self.assertFalse(adapter.is_configured())

        res = adapter.intraday_quotes("NIFTY", date(2026, 11, 5))
        self.assertFalse(res.configured)
        self.assertEqual(res.status, "not_configured")
        self.assertEqual(res.quotes, [])  # never fabricated

        hist = adapter.historical_chain("NIFTY", date(2026, 1, 1), date(2026, 11, 5))
        self.assertFalse(hist.configured)
        self.assertEqual(hist.quotes, [])

    def test_flag_on_but_no_credentials_stays_disabled(self):
        adapter = vendor_options.get_vendor_adapter(
            env={"VENDOR_OPTIONS_ENABLED": "true", "VENDOR_OPTIONS_VENDOR": "acme"})
        self.assertFalse(adapter.is_configured())
        self.assertEqual(adapter.intraday_quotes("NIFTY", date(2026, 11, 5)).status,
                         "not_configured")

    def test_flag_on_with_creds_but_no_registered_adapter_stays_disabled(self):
        adapter = vendor_options.get_vendor_adapter(env={
            "VENDOR_OPTIONS_ENABLED": "1",
            "VENDOR_OPTIONS_VENDOR": "nonexistent_vendor",
            "VENDOR_OPTIONS_API_KEY": "k",
        })
        self.assertFalse(adapter.is_configured())

    def test_registered_vendor_is_selected_only_when_fully_configured(self):
        name = "dummy_test_vendor"

        class _Dummy(vendor_options.VendorOptionAdapter):
            name = "dummy_test_vendor"

            def __init__(self, cfg):
                self._cfg = cfg

            def is_configured(self):
                return True

            def _fetch_intraday(self, underlying, trade_date, expiry):
                return [vendor_options.VendorOptionQuote(
                    trade_date=trade_date, underlying=underlying, expiry=None,
                    strike=23350.0, option_type="CE", price=91.5, source=self.name)]

            def _fetch_historical_chain(self, underlying, start, end, expiry):
                return []

        vendor_options.register_vendor(name, lambda cfg: _Dummy(cfg))
        try:
            adapter = vendor_options.get_vendor_adapter(env={
                "VENDOR_OPTIONS_ENABLED": "yes",
                "VENDOR_OPTIONS_VENDOR": name,
                "VENDOR_OPTIONS_API_KEY": "secret",
            })
            self.assertIsInstance(adapter, _Dummy)
            res = adapter.intraday_quotes("NIFTY", date(2026, 11, 5))
            self.assertTrue(res.configured)
            self.assertEqual(res.status, "ok")
            self.assertEqual(len(res.quotes), 1)
            self.assertEqual(res.quotes[0].source, name)
        finally:
            vendor_options._REGISTRY.pop(name, None)


if __name__ == "__main__":
    unittest.main()
