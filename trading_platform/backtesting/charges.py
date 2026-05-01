from __future__ import annotations

from trading_platform.domain.models import OrderIntent


class ChargesModel:
    """Approximate Indian market trading costs for local backtesting."""

    def estimate(self, intent: OrderIntent, price: float) -> float:
        turnover = abs(intent.quantity * intent.instrument.lot_size * price)
        brokerage = min(20.0, turnover * 0.0003)
        exchange_txn = turnover * 0.0000325
        sebi = turnover * 0.000001
        stamp_or_stt = turnover * 0.0001
        gst = 0.18 * (brokerage + exchange_txn)
        return round(brokerage + exchange_txn + sebi + stamp_or_stt + gst, 2)

