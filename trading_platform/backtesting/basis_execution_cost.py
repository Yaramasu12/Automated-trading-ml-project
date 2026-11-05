"""Realistic India execution-cost model for an index CASH-FUTURES BASIS trade
(2026-11-04). Sibling of execution_cost.py (which models the intraday-equity
gap trade); this one models the two-leg, multi-day basis trade honestly so the
index_basis candidate (research_index_basis.py) faces the SAME realistic-cost
go/no-go that killed the gap signal.

The basis trade holds the near-month index FUTURE against the index CASH for a
few days and captures basis convergence. Its screen subtracted a flat round-trip
%. The real frictions, which that hides, are dominated by the CASH leg:

  * Multi-day hold -> the cash leg is DELIVERY, not intraday. Delivery STT on an
    equity ETF is charged on the SELL at 0.1% (0.001). (Classic single-stock
    delivery is 0.1% on BOTH sides; we use the more favourable ETF sell-only
    rate so the model does not overstate the cost.)
  * TWO legs (future + cash), each paying its own charges on entry AND exit —
    four fills per round trip, not two.
  * The cash leg trades via a liquid index ETF (e.g. NIFTYBEES); it carries an
    ETF half-spread and a small index tracking error on top of statutory costs.

Separately (enforced by the caller, not here): retail cannot hold a SHORT cash
/ ETF position overnight in India, so the 'basis cheap -> long future / short
cash' side of the signal is not executable for a multi-day hold. Only the
'basis rich -> short future / long cash' side is tradeable.

Every rate below is an explicit, current (2026) statutory or conservative
market assumption; change them in one place and re-run the gate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BasisCostModel:
    # ---- futures leg (F&O) -------------------------------------------------
    fut_brokerage_per_order: float = 20.0   # Rs flat, discount broker, per order
    fut_stt_sell: float = 0.0002            # 0.02% on the sell-side turnover
    fut_exch_txn: float = 0.0000190         # ~0.0019% per side, NSE futures
    fut_stamp_buy: float = 0.00002          # 0.002% on the buy-side turnover
    fut_half_spread: float = 0.0001         # per side; index futures are deep
    # ---- cash leg, traded via a liquid index ETF (delivery) ---------------
    etf_stt_sell: float = 0.001             # 0.1% on the sell side (ETF delivery)
    etf_brokerage_per_order: float = 20.0   # Rs flat (many brokers: 0 on delivery)
    etf_exch_txn: float = 0.0000297         # per side
    etf_stamp_buy: float = 0.00015          # 0.015% on the buy-side turnover
    etf_half_spread: float = 0.0005         # per side; index ETF, wider than the future
    etf_tracking_error: float = 0.0003      # round-trip ETF-vs-index drift/creation friction
    etf_dp_charge: float = 15.0             # Rs demat debit on the ETF sell
    # ---- shared -----------------------------------------------------------
    sebi: float = 0.000001                  # Rs 10 / crore, per side
    gst_rate: float = 0.18                  # on brokerage + exchange txn + sebi

    def futures_leg_fraction(self, notional: float) -> float:
        """Round-trip (buy+sell) cost of the futures leg, as a fraction of its
        notional."""
        notional = max(float(notional), 1.0)
        brokerage = 2.0 * self.fut_brokerage_per_order
        exch = self.fut_exch_txn * 2.0 * notional
        sebi = self.sebi * 2.0 * notional
        statutory = (
            self.fut_stt_sell * notional
            + exch
            + sebi
            + self.fut_stamp_buy * notional
            + self.gst_rate * (brokerage + exch + sebi)
        )
        spread = 2.0 * self.fut_half_spread
        return (brokerage + statutory) / notional + spread

    def cash_leg_fraction(self, notional: float) -> float:
        """Round-trip (buy+sell) cost of the cash/ETF leg, as a fraction of its
        notional — the dominant cost, driven by delivery STT + ETF spread."""
        notional = max(float(notional), 1.0)
        brokerage = 2.0 * self.etf_brokerage_per_order
        exch = self.etf_exch_txn * 2.0 * notional
        sebi = self.sebi * 2.0 * notional
        statutory = (
            self.etf_stt_sell * notional
            + exch
            + sebi
            + self.etf_stamp_buy * notional
            + self.gst_rate * (brokerage + exch + sebi)
        )
        spread = 2.0 * self.etf_half_spread
        return (brokerage + self.etf_dp_charge + statutory) / notional + spread + self.etf_tracking_error

    def roundtrip_cost_fraction(self, leg_notional: float) -> float:
        """Total round-trip cost of the two-leg basis trade, as a fraction of one
        leg's notional (both legs are sized ~equally to be basis-neutral). This
        is directly comparable to the signal's per-trade basis-change P&L, which
        is also a fraction of that notional."""
        return self.futures_leg_fraction(leg_notional) + self.cash_leg_fraction(leg_notional)
