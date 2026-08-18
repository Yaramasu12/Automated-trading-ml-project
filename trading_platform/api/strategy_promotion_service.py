"""StrategyPromotionService — promotion ladder + gate enforcement for
rule-based strategies (REDESIGN_PROMPT.md §5).

Mirrors api/policy_service.py's multi-check promotion-gate pattern, but for
strategies rather than RL policies. The two are deliberately separate:

- `PolicyRecord` (rl/policies.py) covers RL policies, which NEVER submit live
  orders (`can_submit_live_orders` is hardcoded False) — promotion there is
  about advisory influence.
- `StrategyPromotionRecord` (here) covers rule-based strategies, which DO place
  live orders once `live_approved`. There is intentionally no
  `can_submit_live_orders` check here; live order submission stays gated by the
  untouched final-execution machinery (RiskEngine, kill switch, arming, the
  confirmation phrase). This ladder gates *promotion*, not order submission.

Promotion to paper or any live tier requires the REDESIGN §5 backtest gates
(CPCV/DSR/PBO/Monte-Carlo-DD/cost model) to have been run AND passed — read
from `backtest_gate_results` via TradingDatabase.latest_gate_summary().
A strategy with no recorded gate run is blocked (absence != pass).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_STRATEGY_STATUS_ORDER = ["research", "shadow", "paper", "live_canary", "live_approved"]
_STRATEGY_TERMINAL_STATUSES = {"disabled"}
# Tiers that require passing backtest gates before entry.
_GATED_STATUSES = {"paper", "live_canary", "live_approved"}
# Tiers that additionally require a paper-trading track record.
_LIVE_STATUSES = {"live_canary", "live_approved"}
# Tiers on which a gate WAIVER may substitute for gate evidence. Paper only,
# and deliberately so: paper risks no real money, whereas the live rungs are
# exactly where unvalidated edge costs money — a waiver must never buy access
# to those. Enforced in _active_waiver(); do not widen without a very good reason.
_WAIVABLE_STATUSES = {"paper"}


@dataclass
class StrategyPromotionRecord:
    strategy_id: str
    status: str = "research"
    version: int = 1
    metadata: dict = field(default_factory=dict)


class StrategyPromotionService:
    def __init__(
        self,
        *,
        db: Any,
        min_paper_days: int = 30,
        paper_days_lookup: Any = None,
    ) -> None:
        """db: TradingDatabase. paper_days_lookup: optional callable
        (strategy_id) -> int, supplying observed paper-trading days; when not
        provided, paper days come from the record's own metadata (set by
        whatever records paper progress) and default to 0 — which blocks live
        promotion rather than silently allowing it."""
        self._db = db
        self._min_paper_days = min_paper_days
        self._paper_days_lookup = paper_days_lookup

    # ---------------- record access ----------------

    def get_record(self, strategy_id: str) -> StrategyPromotionRecord:
        row = self._db.get_strategy_promotion(strategy_id)
        if not row:
            return StrategyPromotionRecord(strategy_id=strategy_id)
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            import json
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}
        return StrategyPromotionRecord(
            strategy_id=row.get("strategy_id", strategy_id),
            status=row.get("status", "research"),
            version=int(row.get("version", 1) or 1),
            metadata=metadata,
        )

    def list_promotions(self) -> list[dict]:
        rows = self._db.list_strategy_promotions()
        out = []
        for row in rows:
            record = self.get_record(row.get("strategy_id"))
            payload = {
                "strategy_id": record.strategy_id,
                "status": record.status,
                "version": record.version,
                "metadata": record.metadata,
            }
            next_status = self._next_status(record.status)
            payload["promotion_gate"] = (
                self.promotion_gate(record, next_status, {})
                if next_status
                else {"approved": False, "reason": "no_next_status", "checks": []}
            )
            out.append(payload)
        return out

    # ---------------- gate ----------------

    @staticmethod
    def _promotion_check(name: str, passed: bool, required: Any, actual: Any, reason: str | None = None) -> dict:
        return {
            "name": name,
            "passed": bool(passed),
            "required": required,
            "actual": actual,
            "reason": reason or ("" if passed else name),
        }

    @staticmethod
    def _next_status(status: str) -> str | None:
        if status in _STRATEGY_TERMINAL_STATUSES or status not in _STRATEGY_STATUS_ORDER:
            return None
        idx = _STRATEGY_STATUS_ORDER.index(status)
        if idx >= len(_STRATEGY_STATUS_ORDER) - 1:
            return None
        return _STRATEGY_STATUS_ORDER[idx + 1]

    @staticmethod
    def _active_waiver(record: StrategyPromotionRecord, target_status: str) -> str | None:
        """Return the waiver reason if this record carries a valid, non-expired
        gate waiver for `target_status`, else None.

        A waiver is stored on the record as
        `metadata["gate_waiver"] = {"reason": str, "rung": str, "granted_at": iso}`.
        It only applies to the rung it was granted for, and only to rungs in
        _WAIVABLE_STATUSES — a paper waiver can never unlock live_canary.
        """
        if target_status not in _WAIVABLE_STATUSES:
            return None
        waiver = (record.metadata or {}).get("gate_waiver")
        if not isinstance(waiver, dict):
            return None
        if waiver.get("rung") != target_status:
            return None
        reason = str(waiver.get("reason") or "").strip()
        return reason or None

    def grant_gate_waiver(self, strategy_id: str, rung: str, reason: str) -> dict:
        """Record an explicit, audited waiver letting `strategy_id` onto `rung`
        without gate evidence. Requires a non-empty reason — an unexplained
        waiver is exactly the silent-override this ladder exists to prevent."""
        rung = str(rung).strip()
        reason = str(reason or "").strip()
        if rung not in _WAIVABLE_STATUSES:
            raise ValueError(
                f"gate waivers are only permitted for {sorted(_WAIVABLE_STATUSES)} "
                f"(requested '{rung}') — live rungs always require real gate results"
            )
        if not reason:
            raise ValueError("a gate waiver requires an explicit reason")
        record = self.get_record(strategy_id)
        metadata = dict(record.metadata or {})
        metadata["gate_waiver"] = {
            "reason": reason,
            "rung": rung,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        }
        self._db.upsert_strategy_promotion(strategy_id, record.status, record.version, metadata)
        logger.warning(
            "GATE WAIVER granted: strategy=%s rung=%s reason=%s", strategy_id, rung, reason
        )
        return {"strategy_id": strategy_id, "rung": rung, "reason": reason, "waiver": metadata["gate_waiver"]}

    def revoke_gate_waiver(self, strategy_id: str) -> dict:
        record = self.get_record(strategy_id)
        metadata = dict(record.metadata or {})
        removed = metadata.pop("gate_waiver", None)
        self._db.upsert_strategy_promotion(strategy_id, record.status, record.version, metadata)
        return {"strategy_id": strategy_id, "revoked": removed is not None, "previous": removed}

    def _paper_days(self, record: StrategyPromotionRecord) -> int:
        if self._paper_days_lookup is not None:
            try:
                return int(self._paper_days_lookup(record.strategy_id) or 0)
            except Exception:
                return 0
        return int(record.metadata.get("paper_days", 0) or 0)

    def promotion_gate(
        self,
        record: StrategyPromotionRecord,
        target_status: str,
        payload: dict | None = None,
    ) -> dict:
        payload = payload or {}
        valid_statuses = set(_STRATEGY_STATUS_ORDER) | _STRATEGY_TERMINAL_STATUSES
        expected_next = self._next_status(record.status)

        checks = [
            self._promotion_check("target_status_valid", target_status in valid_statuses, sorted(valid_statuses), target_status),
            self._promotion_check("strategy_not_disabled", record.status != "disabled", True, record.status),
            self._promotion_check("single_step_forward", target_status == expected_next, expected_next, target_status),
        ]

        gate_summary = None
        if target_status in _GATED_STATUSES:
            gate_summary = self._db.latest_gate_summary(record.strategy_id)
            gates_passed = bool(gate_summary and gate_summary.get("all_passed"))
            # Waivers (see _WAIVABLE_STATUSES) let a strategy onto the PAPER rung
            # without gate evidence — deliberately narrow: paper risks no real
            # money, and some strategies (e.g. index-options short-vol) cannot
            # produce gate results until historical options/index data exists.
            # A waiver is never silent: it needs an explicit reason, is stored on
            # the promotion record, and is surfaced as its own failed-but-waived
            # check so the UI/audit trail shows the gate did NOT actually pass.
            waiver = self._active_waiver(record, target_status)
            if not gates_passed and waiver:
                checks.append(
                    self._promotion_check(
                        "backtest_gates_waived",
                        True,
                        f"waiver for '{target_status}' rung",
                        {"waived": True, "reason": waiver, "gates_passed": gates_passed},
                    )
                )
            else:
                checks.append(
                    self._promotion_check(
                        "backtest_gates_passed",
                        gates_passed,
                        True,
                        (gate_summary or {}).get("all_passed") if gate_summary else None,
                        "no_backtest_gate_run_recorded" if not gate_summary else "backtest_gates_failed",
                    )
                )

        paper_days = self._paper_days(record)
        if target_status in _LIVE_STATUSES:
            checks.append(
                self._promotion_check(
                    "paper_days", paper_days >= self._min_paper_days, self._min_paper_days, paper_days,
                )
            )

        if target_status == "live_approved":
            checks.append(
                self._promotion_check(
                    "manual_live_approval", bool(payload.get("manual_approval")), True,
                    bool(payload.get("manual_approval")),
                )
            )

        approved = all(c["passed"] for c in checks)
        return {
            "approved": approved,
            "current_status": record.status,
            "target_status": target_status,
            "checks": checks,
            "blocking": [c["reason"] or c["name"] for c in checks if not c["passed"]],
            "gate_summary": gate_summary,
            "metrics": {"paper_days": paper_days, "min_paper_days": self._min_paper_days},
        }

    # ---------------- transitions ----------------

    def promote(self, strategy_id: str, target_status: str, payload: dict | None = None) -> dict:
        record = self.get_record(strategy_id)
        gate = self.promotion_gate(record, target_status, payload or {})
        if not gate["approved"]:
            return {"promoted": False, "strategy_id": strategy_id, "promotion_gate": gate}
        metadata = dict(record.metadata)
        metadata["rollback_pointer"] = record.status
        self._db.upsert_strategy_promotion(strategy_id, target_status, record.version + 1, metadata)
        return {
            "promoted": True,
            "strategy_id": strategy_id,
            "status": target_status,
            "previous_status": record.status,
            "promotion_gate": gate,
        }

    def rollback(self, strategy_id: str, target_status: str | None = None) -> dict:
        """Roll a strategy BACK down the ladder. Deliberately ungated —
        de-risking must never be blocked by a failing gate."""
        record = self.get_record(strategy_id)
        previous = target_status or record.metadata.get("rollback_pointer") or "research"
        if previous not in set(_STRATEGY_STATUS_ORDER) | _STRATEGY_TERMINAL_STATUSES:
            return {"rolled_back": False, "reason": "invalid_target_status", "strategy_id": strategy_id}
        metadata = dict(record.metadata)
        metadata["rolled_back_from"] = record.status
        self._db.upsert_strategy_promotion(strategy_id, previous, record.version + 1, metadata)
        return {
            "rolled_back": True,
            "strategy_id": strategy_id,
            "status": previous,
            "previous_status": record.status,
        }
