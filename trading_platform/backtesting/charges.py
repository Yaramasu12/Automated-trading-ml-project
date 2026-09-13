from __future__ import annotations

from datetime import date

from trading_platform.domain.enums import AssetClass, Exchange, Segment, Side
from trading_platform.domain.models import OrderIntent

# audit_results/2026-09-13/AUDIT_REPORT.md finding #3 (high): futures/options
# STT here were understated even against the PRE-hike regime (0.0125%/0.0625%
# coded vs the actual then-current 0.02%/0.10%), and the Budget 2026 hike
# effective 1 April 2026 raised them further. Verified 2026-09-13 against
# NSE Circular Ref. No. 02/2026 (FATAX73524.pdf) via Zerodha's own bulletin
# (nseindia.com's STT page and the circular PDF itself did not respond to a
# direct fetch — re-verify against
# https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax
# and https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf when
# next reachable):
#   Futures STT (sell-side):        0.02%  -> 0.05%  from 1 April 2026
#   Options STT (sell-side premium): 0.10%  -> 0.15%  from 1 April 2026
#   Options STT on exercise (notional): 0.125% -> 0.15% from 1 April 2026
#     (still not modelled here — exercise/assignment isn't simulated by
#     this estimator at all, sell-side premium STT only)
# Equity intraday/delivery STT is unaffected by this circular.
_STT_HIKE_EFFECTIVE_DATE = date(2026, 4, 1)


class ChargesModel:
    """Differentiated Indian market trading costs for backtesting.

    Charge structure per instrument family (current, i.e. on/after
    2026-04-01 — see _STT_HIKE_EFFECTIVE_DATE above for the pre-hike rates
    `estimate(as_of=...)` uses for an earlier date):

    Equity intraday (MIS):
      - STT: 0.025% on sell-side turnover
      - Brokerage: min(₹20, 0.03% of turnover) per side
      - Exchange txn: 0.00325% NSE / 0.00375% BSE
      - SEBI: 0.0001%
      - Stamp duty: 0.003% on buy-side (NSE; state-level, approximated)

    Equity delivery (CNC):
      - STT: 0.1% both sides
      - Brokerage: min(₹20, 0.3% of turnover) per side (flat ₹0 some brokers; using cap)
      - Exchange txn: 0.00325%
      - SEBI: 0.0001%
      - Stamp duty: 0.015% on buy-side

    Futures (NFO/BFO):
      - STT: 0.05% on sell-side (0.02% before 2026-04-01)
      - Brokerage: min(₹20, 0.03% of turnover) per side
      - Exchange txn: 0.002% NSE / 0.002% BFO
      - SEBI: 0.0001%
      - Stamp duty: 0.002% on buy-side

    Options (NFO/BFO) — on premium turnover:
      - STT: 0.15% on sell-side premium turnover (0.10% before 2026-04-01;
        exercised: 0.15% on notional, was 0.125% — not modelled here)
      - Brokerage: flat ₹20 per executed order (most discount brokers)
      - Exchange txn: 0.053% on premium turnover (NSE)
      - SEBI: 0.0001%
      - Stamp duty: 0.003% on buy-side premium

    MCX commodities:
      - CTT: 0.01% on sell-side (non-agri futures, SEBI rate)
      - Brokerage: min(₹20, 0.03% of turnover)
      - Exchange txn: 0.0026% (MCX)
      - SEBI: 0.0001%
      - Stamp duty: 0.002% on buy-side
    """

    def estimate(self, intent: OrderIntent, price: float, as_of: date | None = None) -> float:
        """`as_of`: the trade date this fill actually happened on, so a
        REPLAY/backtest over history spanning the 2026-04-01 STT hike uses
        the rate that actually applied on that date rather than today's
        rate applied retroactively (audit finding #3's "historical
        evaluation needs date-effective schedules" note). None (the
        default, used by every live/PAPER call site) means "now" — always
        the current (post-hike) rate."""
        instrument = intent.instrument
        turnover = abs(intent.quantity * instrument.lot_size * price)
        if turnover <= 0:
            return 0.0

        from trading_platform.domain.enums import ProductType

        is_intraday = intent.product_type == ProductType.INTRADAY
        is_buy = intent.signal.side == Side.BUY
        is_sell = not is_buy
        pre_hike = as_of is not None and as_of < _STT_HIKE_EFFECTIVE_DATE

        segment = instrument.segment
        exchange = instrument.exchange
        asset_class = instrument.asset_class

        if asset_class == AssetClass.COMMODITY and exchange == Exchange.MCX:
            return self._commodity_charges(turnover, is_buy)

        if segment == Segment.OPTIONS:
            return self._options_charges(turnover, is_buy, pre_hike)

        if segment == Segment.FUTURES:
            return self._futures_charges(turnover, is_buy, exchange, pre_hike)

        # Cash equity
        if is_intraday:
            return self._equity_intraday_charges(turnover, is_buy, exchange)
        return self._equity_delivery_charges(turnover, is_buy, exchange)

    # ── Equity intraday ────────────────────────────────────────────────────────

    @staticmethod
    def _equity_intraday_charges(turnover: float, is_buy: bool, exchange: Exchange) -> float:
        brokerage = min(20.0, turnover * 0.0003)
        stt = turnover * 0.00025 if not is_buy else 0.0   # sell-side only, 0.025%
        exchange_txn = turnover * (0.0000375 if exchange == Exchange.BSE else 0.0000325)
        sebi = turnover * 0.000001
        stamp = turnover * 0.00003 if is_buy else 0.0     # 0.003% buy-side stamp duty
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        return round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)

    # ── Equity delivery ────────────────────────────────────────────────────────

    @staticmethod
    def _equity_delivery_charges(turnover: float, is_buy: bool, exchange: Exchange) -> float:
        brokerage = min(20.0, turnover * 0.003)            # 0.3% capped at ₹20
        stt = turnover * 0.001                             # 0.1% both sides
        exchange_txn = turnover * (0.0000375 if exchange == Exchange.BSE else 0.0000325)
        sebi = turnover * 0.000001
        stamp = turnover * 0.00015 if is_buy else 0.0     # 0.015% buy-side stamp duty
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        return round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)

    # ── Futures ────────────────────────────────────────────────────────────────

    @staticmethod
    def _futures_charges(turnover: float, is_buy: bool, exchange: Exchange, pre_hike: bool = False) -> float:
        brokerage = min(20.0, turnover * 0.0003)
        # 0.05% sell-side from 2026-04-01 (was 0.02%) — see
        # _STT_HIKE_EFFECTIVE_DATE's citation above this class.
        stt_rate = 0.0002 if pre_hike else 0.0005
        stt = turnover * stt_rate if not is_buy else 0.0
        exchange_txn = turnover * 0.00002                  # 0.002% NSE/BFO futures
        sebi = turnover * 0.000001
        stamp = turnover * 0.00002 if is_buy else 0.0     # 0.002% buy-side
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        return round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)

    # ── Options ────────────────────────────────────────────────────────────────

    @staticmethod
    def _options_charges(premium_turnover: float, is_buy: bool, pre_hike: bool = False) -> float:
        # Flat ₹20 brokerage regardless of premium size (discount broker standard)
        brokerage = 20.0
        # 0.15% sell-side premium from 2026-04-01 (was 0.10%) — see
        # _STT_HIKE_EFFECTIVE_DATE's citation above this class. Exercise/
        # assignment STT (0.15% of notional, was 0.125%) is still not
        # modelled — this estimator only ever prices a sell-side premium fill.
        stt_rate = 0.001 if pre_hike else 0.0015
        stt = premium_turnover * stt_rate if not is_buy else 0.0
        exchange_txn = premium_turnover * 0.00053                   # 0.053% NSE options
        sebi = premium_turnover * 0.000001
        stamp = premium_turnover * 0.00003 if is_buy else 0.0      # 0.003% buy-side
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        return round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)

    # ── MCX commodities ────────────────────────────────────────────────────────

    @staticmethod
    def _commodity_charges(turnover: float, is_buy: bool) -> float:
        brokerage = min(20.0, turnover * 0.0003)
        ctt = turnover * 0.0001 if not is_buy else 0.0    # CTT 0.01% sell-side (non-agri)
        exchange_txn = turnover * 0.000026                 # 0.0026% MCX
        sebi = turnover * 0.000001
        stamp = turnover * 0.00002 if is_buy else 0.0     # 0.002% buy-side
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        return round(brokerage + ctt + exchange_txn + sebi + stamp + gst, 2)
