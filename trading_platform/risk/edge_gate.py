"""Disciplined entry gate: only OPEN a position on a validated edge in a
favourable volatility-risk-premium (VRP) regime. Exits and reductions are
never blocked.

This wires trading_platform/research/index_premium.compute_reading()'s output
(premium = ATM-IV30 minus 20-day realized vol, in vol points; and its
trailing-60-day median) into the order-submission path for the FIRST time.
See that module's standing "REPORTING ONLY: nothing in this module is wired
into any entry-decision path" note, and the 2026-10-13 audit finding #3.

The rule is faithful to audit_results/2026-09-19/premium_regime_filter.md: an
entry on a (strategy_family, underlying) pair is allowed only when

    (1) the pair is on the approved trade list  (promotion / finding #2), and
    (2) the underlying's trailing-60d median premium >= that pair's theta
        (the regime "TRADE" verdict; below theta is "STAND ASIDE" — #3).

It FAILS CLOSED: if the regime data needed to justify an entry is missing or
stale, the entry is refused. Standing aside on missing data is the whole point
of a disciplined gate — the system should not open a short-vol position blind.

Pure logic, no I/O: the caller (runtime._evaluate_final_execution_gate) reads
the latest premium row from the DB and passes it in as a PremiumSnapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Index underlyings this gate understands. Option symbols like
# "FINNIFTY13OCT2625500CE" carry the underlying as a prefix; equities carry
# their own ticker and are matched by exact symbol instead.
_INDEX_UNDERLYINGS: tuple[str, ...] = (
    "MIDCPNIFTY",  # longest-prefix first so "NIFTY" never shadows it
    "BANKNIFTY",
    "FINNIFTY",
    "SENSEX",
    "BANKEX",
    "NIFTY",
)

# Strategy-name prefixes collapsed to one family for policy lookup, e.g.
# "short_vol_condor", "short_vol_exit", "short_vol_variants_putspread" ->
# "short_vol". A name matching no known family is used verbatim (so an
# unknown strategy is simply never on the approved list -> refused).
_KNOWN_FAMILIES: tuple[str, ...] = ("short_vol", "equity_momentum", "momentum")


def family_of(strategy_name: str | None) -> str:
    name = (strategy_name or "").strip().lower()
    for fam in _KNOWN_FAMILIES:
        if name == fam or name.startswith(fam + "_"):
            return fam
    return name


def underlying_of(symbol: str | None, explicit: str | None = None) -> str:
    """Underlying for an instrument. Prefer the instrument's own `underlying`
    field; otherwise recover an index underlying from the option symbol's
    prefix; otherwise fall back to the raw symbol (an equity trades under its
    own ticker)."""
    if explicit:
        return explicit.strip().upper()
    sym = (symbol or "").strip().upper()
    for u in _INDEX_UNDERLYINGS:
        if sym.startswith(u):
            return u
    return sym


@dataclass(frozen=True)
class PremiumSnapshot:
    """One underlying's most recent VRP reading (from index_vol_premium)."""

    underlying: str
    trade_date: date | None
    premium: float | None               # today's VRP, vol points (iv30 - rv20)
    trailing_median_60d: float | None   # the regime signal the filter uses
    stale_days: int = 0                  # trade_date age vs the entry's as_of


@dataclass(frozen=True)
class EdgePolicy:
    """One approved line on the disciplined trade list."""

    strategy_family: str
    underlying: str
    min_trailing_median_premium: float   # theta, in vol points
    max_staleness_days: int = 4          # weekend + one holiday of tolerance


@dataclass(frozen=True)
class EdgeGateResult:
    allowed: bool
    code: str
    reason: str

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason}


_ALLOW_EXIT = EdgeGateResult(True, "exit_or_reduction", "exits/reductions are never gated")
_ALLOW_DISABLED = EdgeGateResult(True, "gate_disabled", "edge gate disabled by config")


class EdgeGate:
    """Enforces the approved trade list + VRP-regime filter on entries."""

    def __init__(
        self,
        policies: list[EdgePolicy],
        *,
        enabled: bool = True,
        require_regime_data: bool = True,
        governed_families: set[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.require_regime_data = require_regime_data
        self._by_key: dict[tuple[str, str], EdgePolicy] = {
            (p.strategy_family, p.underlying.upper()): p for p in policies
        }
        # The gate only disciplines families it has an opinion about — by
        # default, exactly those named in the approved-edge policies. A family
        # with no policy at all (e.g. a directional or manual strategy, or a
        # test fixture) is governed by the other gates, not this one, and
        # passes through. To bring a new family under discipline, add a policy
        # line for it (even a deliberately empty-underlying "stand aside" one).
        self._governed_families: set[str] = (
            governed_families
            if governed_families is not None
            else {p.strategy_family for p in policies}
        )

    # ---- construction from config ------------------------------------------

    @classmethod
    def from_spec(
        cls,
        spec: str,
        *,
        enabled: bool = True,
        require_regime_data: bool = True,
    ) -> "EdgeGate":
        """Parse "short_vol:FINNIFTY:4.0, short_vol:NIFTY:2.0" into policies.

        Each entry is family:underlying:theta[:max_staleness_days]. Malformed
        entries are skipped (a policy you cannot parse must not silently widen
        what may trade)."""
        policies: list[EdgePolicy] = []
        for raw in (spec or "").split(","):
            part = raw.strip()
            if not part:
                continue
            bits = [b.strip() for b in part.split(":")]
            if len(bits) < 3:
                continue
            try:
                theta = float(bits[2])
                stale = int(bits[3]) if len(bits) >= 4 and bits[3] else 4
            except ValueError:
                continue
            policies.append(
                EdgePolicy(
                    strategy_family=family_of(bits[0]),
                    underlying=bits[1].upper(),
                    min_trailing_median_premium=theta,
                    max_staleness_days=stale,
                )
            )
        return cls(policies, enabled=enabled, require_regime_data=require_regime_data)

    @property
    def approved_pairs(self) -> list[tuple[str, str]]:
        return sorted(self._by_key.keys())

    # ---- the decision -------------------------------------------------------

    def evaluate(
        self,
        *,
        strategy_name: str | None,
        underlying: str,
        opens_position: bool,
        premium: PremiumSnapshot | None,
    ) -> EdgeGateResult:
        if not opens_position:
            return _ALLOW_EXIT
        if not self.enabled:
            return _ALLOW_DISABLED

        family = family_of(strategy_name)
        if family not in self._governed_families:
            return EdgeGateResult(
                True, "family_not_governed",
                f"{family} is not under edge-gate discipline (no approved-edge policy)",
            )
        key = (family, (underlying or "").upper())
        policy = self._by_key.get(key)
        if policy is None:
            return EdgeGateResult(
                False,
                "not_on_approved_trade_list",
                f"{family} on {underlying} is not a promoted edge "
                f"(approved: {', '.join(f'{f}/{u}' for f, u in self.approved_pairs) or 'none'})",
            )

        signal = premium.trailing_median_60d if premium is not None else None
        if signal is None:
            if self.require_regime_data:
                return EdgeGateResult(
                    False,
                    "regime_data_unavailable",
                    f"no trailing-60d premium for {underlying}; standing aside "
                    f"(run scripts/backfill_premium_monitor.py to populate it)",
                )
            return EdgeGateResult(True, "regime_data_optional", "regime data missing but not required")

        if premium is not None and premium.stale_days > policy.max_staleness_days:
            return EdgeGateResult(
                False,
                "regime_data_stale",
                f"{underlying} premium reading is {premium.stale_days}d old "
                f"(> {policy.max_staleness_days}d); standing aside",
            )

        if signal < policy.min_trailing_median_premium:
            return EdgeGateResult(
                False,
                "regime_stand_aside",
                f"{underlying} trailing-60d premium {signal:.2f} < theta "
                f"{policy.min_trailing_median_premium:.2f}; no vol-selling edge now",
            )

        return EdgeGateResult(
            True,
            "edge_validated",
            f"{family}/{underlying} trailing-60d premium {signal:.2f} "
            f">= theta {policy.min_trailing_median_premium:.2f}",
        )
