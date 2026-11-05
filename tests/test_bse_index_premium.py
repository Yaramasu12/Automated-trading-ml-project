"""Phase-1 BSE F&O bhavcopy ingestion for SENSEX / BANKEX index options.

Mirrors tests/test_angel_one_data.py (unittest) and the SQLite-DB style of the
other DB tests. Covers the whole reused pipeline against a small, deterministic
saved fixture zip (tests/fixtures/bse_fo_sample.zip) whose option settlement
prices were produced with the module's own Black-76 at a KNOWN vol per
(index, expiry), so IV recovery is checkable:

  * parse: SENSEX/BANKEX CE/PE rows, settle>0, non-index / zero-settle noise dropped
  * ATM IV recovery lands in a sane band and returns the seeded vol back
  * iv30 total-variance interpolation between two bracketing SENSEX expiries
  * fetch_bse_zip wraps a bare CSV into a zip and rejects BSE's HTML error page
  * a backfill populating index_vol_premium for SENSEX/BANKEX, latest_index_premium()
    returning them, and runtime._premium_snapshot_for("SENSEX") returning a snapshot
    (so the edge-gate wiring works end-to-end, without touching the order path)

REPORTING ONLY — nothing here promotes SENSEX/BANKEX or exercises any live-order path.
"""
from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from trading_platform.research import bse_index_premium as bse
from trading_platform.research.index_premium import atm_rows, compute_reading, iv30_for_day
from trading_platform.data.persistence import TradingDatabase

FIXTURE = Path(__file__).parent / "fixtures" / "bse_fo_sample.zip"
FIXTURE_DAY = date(2024, 7, 8)


def _fixture_zip_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _fixture_csv_bytes() -> bytes:
    with zipfile.ZipFile(FIXTURE) as zf:
        return zf.read(zf.namelist()[0])


class BseParseTests(unittest.TestCase):
    def setUp(self):
        self.opts = bse.parse_bse_index_options(_fixture_zip_bytes())

    def test_parses_sensex_and_bankex_ce_pe_with_settle_positive(self):
        syms = {o["sym"] for o in self.opts}
        self.assertEqual(syms, {"SENSEX", "BANKEX"})
        self.assertTrue(all(o["ot"] in ("CE", "PE") for o in self.opts))
        self.assertTrue(all(o["px"] > 0 for o in self.opts))
        # both option types present for each index
        for sym in ("SENSEX", "BANKEX"):
            types = {o["ot"] for o in self.opts if o["sym"] == sym}
            self.assertEqual(types, {"CE", "PE"}, sym)
        # lot size carried through from NewBrdLotQty (SENSEX 10, BANKEX 15)
        self.assertEqual({o["lot"] for o in self.opts if o["sym"] == "SENSEX"}, {10})
        self.assertEqual({o["lot"] for o in self.opts if o["sym"] == "BANKEX"}, {15})

    def test_drops_non_index_and_zero_settle_rows(self):
        # the fixture also holds an index FUTURE (IDF), a stock option (STO,
        # RELIANCE) and a zero-settle index option — none must survive.
        self.assertNotIn("RELIANCE", {o["sym"] for o in self.opts})
        self.assertTrue(all(o["sym"] in ("SENSEX", "BANKEX") for o in self.opts))
        # the zero-settle SENSEX 72000 PE must be absent (px>0 filter)
        self.assertFalse(any(o["sym"] == "SENSEX" and o["k"] == 72000.0 for o in self.opts))

    def test_atm_iv_recovery_in_sane_band(self):
        rows = atm_rows(FIXTURE_DAY, self.opts)
        self.assertTrue(rows)
        for r in rows:
            self.assertGreater(r["iv"], 0.08, r)
            self.assertLess(r["iv"], 0.25, r)
        # the seeded vols come back: SENSEX 7-DTE 0.14, 35-DTE 0.18, BANKEX 0.15
        by = {(r["index"], r["dte"]): r["iv"] for r in rows}
        self.assertAlmostEqual(by[("SENSEX", 7)], 0.14, places=2)
        self.assertAlmostEqual(by[("SENSEX", 35)], 0.18, places=2)
        self.assertAlmostEqual(by[("BANKEX", 7)], 0.15, places=2)

    def test_iv30_interpolation_between_bracketing_expiries(self):
        iv30 = iv30_for_day(atm_rows(FIXTURE_DAY, self.opts))
        self.assertIn("SENSEX", iv30)
        self.assertIn("BANKEX", iv30)
        sensex_iv30, sensex_spot = iv30["SENSEX"]
        # SENSEX brackets 30d with a 7d leg (14%) and a 35d leg (18%): the
        # total-variance interpolation must land strictly between the two.
        self.assertGreater(sensex_iv30, 14.0)
        self.assertLess(sensex_iv30, 18.0)
        # recovered parity spot is near the seeded underlying (~79950)
        self.assertAlmostEqual(sensex_spot, 79950.0, delta=200.0)
        # BANKEX has a single expiry -> iv30 is that leg's ~15%
        self.assertAlmostEqual(iv30["BANKEX"][0], 15.0, delta=0.5)


class BseFetchTests(unittest.TestCase):
    def test_urls_for_builds_expected_download_path(self):
        urls = bse.bse_urls_for(date(2024, 7, 8))
        self.assertEqual(
            urls,
            ["https://www.bseindia.com/download/Bhavcopy/Derivative/"
             "BhavCopy_BSE_FO_0_0_0_20240708_F_0000.CSV"],
        )

    def test_fetch_bse_zip_wraps_bare_csv_into_parseable_zip(self):
        csv_bytes = _fixture_csv_bytes()
        # BSE serves a bare CSV; fetch_bse_zip must wrap it so the shared
        # (zip-reading) parser works. Patch the HTTP core, no network.
        orig = bse.fetch_urls
        bse.fetch_urls = lambda urls, referer, retries=3: csv_bytes
        try:
            zbytes = bse.fetch_bse_zip(date(2024, 7, 8))
        finally:
            bse.fetch_urls = orig
        self.assertIsNotNone(zbytes)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(zbytes)))
        opts = bse.parse_bse_index_options(zbytes)
        self.assertEqual({o["sym"] for o in opts}, {"SENSEX", "BANKEX"})

    def test_fetch_bse_zip_rejects_html_error_page(self):
        # a holiday / not-yet-published day returns BSE's HTML error page with
        # HTTP 200 (not a 404) — it must be treated as "no file", not persisted.
        orig = bse.fetch_urls
        bse.fetch_urls = lambda urls, referer, retries=3: b"<!DOCTYPE html><html>error</html>"
        try:
            self.assertIsNone(bse.fetch_bse_zip(date(2024, 7, 8)))
        finally:
            bse.fetch_urls = orig

    def test_fetch_bse_zip_none_when_no_file(self):
        orig = bse.fetch_urls
        bse.fetch_urls = lambda urls, referer, retries=3: None
        try:
            self.assertIsNone(bse.fetch_bse_zip(date(2024, 7, 8)))
        finally:
            bse.fetch_urls = orig


class BseBackfillDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmp.name) / "trading.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _seed_from_fixture(self):
        """Seed each index's (iv30, spot) from the real fixture parse."""
        return iv30_for_day(atm_rows(FIXTURE_DAY, bse.parse_bse_index_options(_fixture_zip_bytes())))

    def test_backfill_populates_index_vol_premium_and_latest_returns_them(self):
        seed = self._seed_from_fixture()
        indices = ("SENSEX", "BANKEX")
        prior_spots = {i: [] for i in indices}
        prior_premiums = {i: [] for i in indices}
        # Replay a deterministic ~30-day series per index through the SAME
        # compute_reading + save_index_premium the live job / NSE backfill use,
        # so realized vol and the trailing-60d median actually populate.
        for i in range(30):
            day = FIXTURE_DAY + timedelta(days=i)
            for idx in indices:
                iv30, base_spot = seed[idx]
                spot = base_spot * (1 + 0.0007 * (((i * 7) % 11) - 5))  # bounded deterministic drift
                realized, premium, trailing_median = compute_reading(
                    day, iv30, spot, prior_spots[idx], prior_premiums[idx],
                )
                self.db.save_index_premium(day, idx, spot, iv30, realized, premium, trailing_median)
                prior_spots[idx] = (prior_spots[idx] + [(day, spot)])[-20:]
                if premium is not None:
                    prior_premiums[idx] = (prior_premiums[idx] + [premium])[-59:]

        latest = {r["index_symbol"]: r for r in self.db.latest_index_premium()}
        self.assertIn("SENSEX", latest)
        self.assertIn("BANKEX", latest)
        # once >=15 observations exist, realized vol and premium are populated
        self.assertIsNotNone(latest["SENSEX"]["premium"])
        self.assertIsNotNone(latest["SENSEX"]["trailing_median_60d"])
        self.assertIsNotNone(latest["BANKEX"]["premium"])
        # iv30 round-trips through the DB as the seeded value
        self.assertAlmostEqual(latest["SENSEX"]["atm_iv30"], seed["SENSEX"][0], places=2)


class BseRuntimeSnapshotTests(unittest.TestCase):
    def test_premium_snapshot_for_sensex_returns_snapshot_for_edge_gate(self):
        from trading_platform.api.runtime import TradingRuntime
        from trading_platform.risk.edge_gate import PremiumSnapshot

        runtime = TradingRuntime()
        today = datetime.now(timezone.utc).date()
        # Persist a SENSEX reading the same way the BSE monitor job would.
        runtime.db.save_index_premium(today, "SENSEX", 77200.0, 13.4, 16.1, -2.7, 4.0)

        snap = runtime._premium_snapshot_for("SENSEX", datetime.now(timezone.utc))
        self.assertIsInstance(snap, PremiumSnapshot)
        self.assertEqual(snap.underlying, "SENSEX")
        self.assertEqual(snap.trade_date, today)
        self.assertAlmostEqual(snap.trailing_median_60d, 4.0, places=3)
        self.assertEqual(snap.stale_days, 0)


if __name__ == "__main__":
    unittest.main()
