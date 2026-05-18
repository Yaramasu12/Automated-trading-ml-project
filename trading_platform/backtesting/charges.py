from __future__ import annotations

from trading_platform.domain.enums import AssetClass, Exchange, Segment, Side
from trading_platform.domain.models import OrderIntent


class ChargesModel:
    """Differentiated Indian market trading costs for backtesting.

    Charge structure per instrument family:

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
      - STT: 0.0125% on sell-side (SEBI rate as of FY2024)
      - Brokerage: min(₹20, 0.03% of turnover) per side
      - Exchange txn: 0.002% NSE / 0.002% BFO
      - SEBI: 0.0001%
      - Stamp duty: 0.002% on buy-side

    Options (NFO/BFO) — on premium turnover:
      - STT: 0.0625% on sell-side premium turnover (exercised: 0.125% on notional — not modelled here)
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

    def estimate(self, intent: OrderIntent, price: float) -> float:
        instrument = intent.instrument
        turnover = abs(intent.quantity * instrument.lot_size * price)
        if turnover <= 0:
            return 0.0

        from trading_platform.domain.enums import ProductType

        is_intraday = intent.product_type == ProductType.INTRADAY
        is_buy = intent.signal.side == Side.BUY
        is_sell = not is_buy

        segment = instrument.segment
        exchange = instrument.exchange
        asset_class = instrument.asset_class

        if asset_class == AssetClass.COMMODITY and exchange == Exchange.MCX:
            return self._commodity_charges(turnover, is_buy)

        if segment == Segment.OPTIONS:
            return self._options_charges(turnover, is_buy)

        if segment == Segment.FUTURES:
            return self._futures_charges(turnover, is_buy, exchange)

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
    def _futures_charges(turnover: float, is_buy: bool, exchange: Exchange) -> float:
        brokerage = min(20.0, turnover * 0.0003)
        stt = turnover * 0.000125 if not is_buy else 0.0  # 0.0125% sell-side (SEBI FY2024 rate)
        exchange_txn = turnover * 0.00002                  # 0.002% NSE/BFO futures
        sebi = turnover * 0.000001
        stamp = turnover * 0.00002 if is_buy else 0.0     # 0.002% buy-side
        gst = 0.18 * (brokerage + exchange_txn + sebi)
        return round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)

    # ── Options ────────────────────────────────────────────────────────────────

    @staticmethod
    def _options_charges(premium_turnover: float, is_buy: bool) -> float:
        # Flat ₹20 brokerage regardless of premium size (discount broker standard)
        brokerage = 20.0
        stt = premium_turnover * 0.000625 if not is_buy else 0.0   # 0.0625% sell-side on premium
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
