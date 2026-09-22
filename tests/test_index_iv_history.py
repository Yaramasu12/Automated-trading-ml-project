"""Tests for trading_platform.research.index_premium (shared by scripts/
build_index_iv_history.py and daily_scheduler.py's premium_monitor_update
job) and the backtester options added alongside it (wing_pct, skew_per_10pct).

Every per-index conclusion drawn from NSE bhavcopy rests on the implied-vol
inversion and ATM extraction being right, so those are tested against chains
with a KNOWN volatility rather than trusting the output.
"""
from __future__ import annotations

import io
import math
import unittest
import zipfile
from datetime import date, timedelta

import trading_platform.research.index_premium as biv
from trading_platform.backtesting.short_vol_backtest import ShortVolBacktester, smile_iv
from trading_platform.domain.enums import OptionType


def _synthetic_chain(sym: str, day: date, expiry: date, fwd: float, sigma: float,
                     step: float = 50.0, span: int = 25) -> list[dict]:
    """A chain priced exactly by Black-76 at a known sigma/forward."""
    t = (expiry - day).days / 365.0
    k0 = round(fwd / step) * step
    out = []
    for n in range(-span, span + 1):
        k = k0 + n * step
        for ot, is_call in (("CE", True), ("PE", False)):
            px = biv.b76(fwd, k, t, sigma, is_call)
            if px > 0.05:
                out.append(dict(sym=sym, exp=expiry, k=k, ot=ot, px=round(px, 2),
                                vol=1000.0, oi=5000.0, lot=65, und=0.0))
    return out


class ImpliedVolTests(unittest.TestCase):
    def test_inversion_round_trips_across_vol_levels_and_moneyness(self):
        t, fwd = 30 / 365.0, 24000.0
        for sigma in (0.08, 0.12, 0.20, 0.35, 0.60):
            for k in (22800.0, 24000.0, 25200.0):
                for is_call in (True, False):
                    px = biv.b76(fwd, k, t, sigma, is_call)
                    if px < 0.5:      # too cheap for settlement-price precision
                        continue
                    got = biv.implied_vol(px, fwd, k, t, is_call)
                    self.assertIsNotNone(got)
                    self.assertAlmostEqual(got, sigma, delta=0.002)

    def test_price_below_intrinsic_is_rejected_not_inverted(self):
        # A settlement price below discounted intrinsic has no implied vol;
        # returning a number here would silently poison the IV series.
        self.assertIsNone(biv.implied_vol(1.0, 24000.0, 23000.0, 30 / 365.0, True))


class AtmExtractionTests(unittest.TestCase):
    def test_recovers_known_sigma_and_forward_from_a_synthetic_chain(self):
        day, expiry = date(2026, 9, 1), date(2026, 9, 29)
        opts = _synthetic_chain("BANKNIFTY", day, expiry, fwd=56000.0, sigma=0.22, step=100.0)
        rows = biv.atm_rows(day, opts)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["index"], "BANKNIFTY")
        self.assertEqual(r["dte"], 28)
        self.assertAlmostEqual(r["iv"], 0.22, delta=0.003)
        self.assertAlmostEqual(r["forward"], 56000.0, delta=60.0)   # settle prices rounded to paise
        self.assertLessEqual(abs(r["strike"] - 56000.0), 200.0)

    def test_expiry_day_and_thin_chains_are_skipped(self):
        day = date(2026, 9, 29)
        self.assertEqual(biv.atm_rows(day, _synthetic_chain("NIFTY", day, day, 24000.0, 0.15)), [])
        thin = _synthetic_chain("NIFTY", date(2026, 9, 1), date(2026, 9, 29), 24000.0, 0.15, span=0)
        self.assertEqual(biv.atm_rows(date(2026, 9, 1), thin), [])

    def test_skew_columns_reflect_a_steeper_put_wing(self):
        # Price the puts richer than the calls; the recorded OTM put IV must
        # exceed the OTM call IV -- this is what lets the smile be calibrated
        # from data instead of assumed.
        day, expiry = date(2026, 9, 1), date(2026, 9, 29)
        t, fwd = 28 / 365.0, 24000.0
        opts = []
        for n in range(-30, 31):
            k = fwd + n * 50
            for ot, is_call in (("CE", True), ("PE", False)):
                sig = 0.12 + (0.20 - 0.12) * max(0.0, (fwd - k) / (fwd * 0.05)) if not is_call and k < fwd else 0.12
                px = biv.b76(fwd, k, t, sig, is_call)
                if px > 0.05:
                    opts.append(dict(sym="NIFTY", exp=expiry, k=k, ot=ot, px=round(px, 2),
                                     vol=1.0, oi=1.0, lot=65, und=0.0))
        r = biv.atm_rows(day, opts)[0]
        self.assertGreater(r["iv_put95"], r["iv_call105"])


class ParseTests(unittest.TestCase):
    @staticmethod
    def _zip(name: str, text: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name, text)
        return buf.getvalue()

    def test_parses_the_new_udiff_format_and_ignores_stock_options(self):
        hdr = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
               "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
               "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
               "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")
        idx = ("2026-09-18,2026-09-18,FO,NSE,IDO,1,,NIFTY,,2026-09-22,2026-09-22,23350.00,CE,X,"
               "0,0,0,90,90,90,23346.4,91.50,4955600,0,100,0,1,F1,65,,,,,")
        stk = ("2026-09-18,2026-09-18,FO,NSE,STO,2,,ABCAPITAL,,2026-09-29,2026-09-29,480.00,PE,Y,"
               "0,0,0,64,64,64,410.04,69.24,6200,0,0,0,0,F1,3100,,,,,")
        out = biv.parse_index_options(self._zip("a.csv", "\n".join([hdr, idx, stk])))
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["sym"], out[0]["k"], out[0]["ot"], out[0]["px"], out[0]["lot"]),
                         ("NIFTY", 23350.0, "CE", 91.5, 65))

    def test_parses_the_old_format(self):
        hdr = ("INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,"
               "CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,")
        row = "OPTIDX,BANKNIFTY,29-Jun-2023,44000,PE,10,11,9,10.5,10.7,500,1.0,1000,10,15-JUN-2023,"
        fut = "FUTIDX,BANKNIFTY,29-Jun-2023,0,XX,1,1,1,1,1,1,1,1,1,15-JUN-2023,"
        out = biv.parse_index_options(self._zip("b.csv", "\n".join([hdr, row, fut])))
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["sym"], out[0]["exp"], out[0]["ot"], out[0]["px"]),
                         ("BANKNIFTY", date(2023, 6, 29), "PE", 10.7))

    def test_url_format_switches_at_the_udiff_cutover(self):
        self.assertIn("BhavCopy_NSE_FO_0_0_0_20260918", biv.urls_for(date(2026, 9, 18))[0])
        self.assertIn("fo15JUN2023bhav.csv.zip", biv.urls_for(date(2023, 6, 15))[0])


class BacktesterOptionTests(unittest.TestCase):
    def test_wing_pct_scales_with_spot_and_defaults_to_the_fixed_wing(self):
        fixed = ShortVolBacktester(underlying="BANKNIFTY")
        self.assertEqual(fixed._wing_at(28000.0), fixed.wing)        # default unchanged
        scaled = ShortVolBacktester(underlying="BANKNIFTY", wing_pct=0.0125)
        self.assertEqual(scaled._wing_at(56000.0), 700.0)
        self.assertEqual(scaled._wing_at(28000.0), 400.0)             # 350 -> nearest 100 step
        self.assertEqual(scaled._wing_at(1000.0), 200.0)              # never narrower than 2 steps

    def test_steeper_skew_prices_otm_puts_richer_and_reaches_the_marks(self):
        self.assertGreater(smile_iv(0.15, 24000, 22800, OptionType.PE, 0.6),
                           smile_iv(0.15, 24000, 22800, OptionType.PE, 0.25))
        self.assertEqual(ShortVolBacktester(skew_per_10pct=0.5).skew_per_10pct, 0.5)
        self.assertEqual(ShortVolBacktester().skew_per_10pct, 0.25)


    def test_entry_filter_blocks_new_entries_only(self):
        # A filter that always says "no" must produce zero trades; the
        # default (None) must be unchanged behaviour. Uses a flat synthetic
        # series with a rich IV so an unfiltered run does enter.
        from trading_platform.backtesting.short_vol_backtest import DailyBar
        import math
        px, bars, d = 20000.0, [], date(2020, 1, 1)
        for i in range(160):
            px *= math.exp(0.0002 * math.sin(i))
            d += timedelta(days=1)
            if d.weekday() < 5:
                bars.append(DailyBar(d, px))
        iv = {b.day: 22.0 for b in bars}
        open_run = ShortVolBacktester(underlying="NIFTY").run(bars, iv)
        blocked = ShortVolBacktester(underlying="NIFTY", entry_filter=lambda day: False).run(bars, iv)
        self.assertGreater(len(open_run.trades), 0)
        self.assertEqual(len(blocked.trades), 0)


class ComputeReadingTests(unittest.TestCase):
    """compute_reading() is shared by the daily scheduler job and the DB
    backfill script -- one implementation, so a live day and a backfilled
    day are computed identically."""

    def test_reports_nothing_until_enough_history_exists(self):
        # Fewer than 15 prior spots/premiums -> honestly "not enough data
        # yet", not a noisy number from 2 observations.
        realized, premium, trailing = biv.compute_reading(date(2026, 1, 1), 15.0, 24000.0, [], [])
        self.assertIsNone(realized)
        self.assertIsNone(premium)
        self.assertIsNone(trailing)

    def test_realized_vol_and_premium_from_a_flat_spot_series(self):
        # A perfectly flat spot series has zero realized vol, so the whole
        # implied level is the premium.
        spots = [(date(2026, 1, 1) + timedelta(days=i), 24000.0) for i in range(20)]
        realized, premium, trailing = biv.compute_reading(date(2026, 1, 21), 15.0, 24000.0, spots, [])
        self.assertAlmostEqual(realized, 0.0, places=6)
        self.assertAlmostEqual(premium, 15.0, places=6)
        self.assertIsNone(trailing)   # premium history still empty

    def test_trailing_median_uses_prior_premiums_plus_todays(self):
        spots = [(date(2026, 1, 1) + timedelta(days=i), 24000.0) for i in range(20)]
        prior_premiums = [float(x) for x in range(1, 16)]  # 1..15, median 8
        _, premium, trailing = biv.compute_reading(date(2026, 1, 21), 15.0, 24000.0, spots, prior_premiums)
        self.assertAlmostEqual(premium, 15.0, places=6)
        self.assertAlmostEqual(trailing, statistics_median(prior_premiums + [15.0]))

    def test_history_windows_are_capped_not_unbounded(self):
        # 200 days of spot history and 200 premiums must not silently grow
        # the window past 20/60 -- a 90-row DB fetch upstream should still
        # behave identically to a capped one.
        spots = [(date(2020, 1, 1) + timedelta(days=i), 24000.0 + i) for i in range(200)]
        prior_premiums = [float(i) for i in range(200)]
        realized, premium, trailing = biv.compute_reading(
            date(2020, 7, 19), 15.0, 24224.0, spots, prior_premiums)
        # Only the last 20 spots (19 returns) and last 59 premiums + today's
        # feed the result -- sanity bound, not an exact hand-derived value.
        self.assertIsNotNone(realized)
        self.assertGreater(trailing, 140)  # median of a near-top-60 slice of 0..199 + 15.0


def statistics_median(xs):
    import statistics
    return statistics.median(xs)


if __name__ == "__main__":
    unittest.main()
