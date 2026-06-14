"""PolicyService — RL/MARL policy promotion gate + live-canary readiness.

Phase 2 of the runtime decomposition (the most entangled cluster). Holds the
policy listing, promotion/rollback, the multi-check promotion gate, and the
live-canary (M6) readiness computation that the final execution gate consults.

Dependencies are injected under the SAME attribute names they had on the
runtime, so the method bodies are a verbatim move with zero substitution. This
includes `trace_replay` and `_float_or_none` as injected callables (they live on
the runtime and are also used elsewhere there).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable

from trading_platform.rl.policies import PolicyRecord

# Promotion ladder + terminal states (moved with the policy methods from runtime).
_POLICY_STATUS_ORDER = ["research", "shadow", "paper", "live_canary", "live_approved"]
_POLICY_TERMINAL_STATUSES = {"disabled"}


class PolicyService:
    def __init__(
        self,
        *,
        policy_registry: Any,
        policy_promotion_requirements: dict,
        paper_learning_journal: Any,
        trace_replay: Callable[[str], dict | None],
        float_or_none: Callable[[object], float | None],
    ) -> None:
        self._policy_registry = policy_registry
        self._policy_promotion_requirements = policy_promotion_requirements
        self.paper_learning_journal = paper_learning_journal
        self.trace_replay = trace_replay
        self._float_or_none = float_or_none

    def live_canary_readiness_payload(self) -> dict:
        """Return M6 readiness: whether live-canary can be considered.

        This is stricter than broker readiness. Broker readiness says whether
        live plumbing can be armed; M6 says whether the paper/shadow evidence is
        clean enough to even consider a live-canary policy.
        """
        trace_ids, journal_days = self._live_canary_evidence_trace_ids()
        metrics = self._policy_promotion_metrics({"trace_ids": trace_ids})
        metrics["paper_trading_days"] = max(metrics["paper_trading_days"], len(journal_days))
        req = self._policy_promotion_requirements

        paper_checks = self._paper_readiness_checks(metrics)
        policy_candidates = self._live_canary_policy_candidates()
        ready_candidates = [
            item for item in policy_candidates
            if item["status"] in {"live_canary", "live_approved"} or item["gate"].get("approved")
        ]
        paper_days = metrics["paper_trading_days"]
        target_max_paper_days = 20
        checks = [
            *paper_checks,
            self._promotion_check(
                "paper_review_window",
                paper_days <= target_max_paper_days,
                f"<={target_max_paper_days}",
                paper_days,
                "paper_review_overdue",
            ),
            self._promotion_check(
                "policy_gate_ready",
                len(ready_candidates) > 0,
                ">0 paper policy with approved live_canary gate",
                len(ready_candidates),
            ),
        ]
        approved = all(check["passed"] for check in checks)
        blocking = [check["reason"] or check["name"] for check in checks if not check["passed"]]
        return {
            "can_consider_live_canary": approved,
            "status": "READY" if approved else "NOT_READY",
            "blocking_reasons": blocking,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "paper_window": {
                "min_days": req["min_paper_trading_days"],
                "target_max_days": target_max_paper_days,
                "actual_days": paper_days,
                "minimum_met": paper_days >= req["min_paper_trading_days"],
                "within_target_review_window": req["min_paper_trading_days"] <= paper_days <= target_max_paper_days,
                "review_overdue": paper_days > target_max_paper_days,
            },
            "metrics": metrics,
            "checks": checks,
            "policy_candidates": policy_candidates,
            "requirements": {**req, "target_max_paper_days": target_max_paper_days},
            "evidence": {
                "trace_count": len(trace_ids),
                "trace_ids": trace_ids[:100],
                "paper_days": sorted(journal_days),
            },
        }

    def list_policies(self) -> dict:
        return {"policies": [self._policy_payload(rec) for rec in self._policy_registry._records.values()]}

    def promote_policy(self, payload: dict) -> dict:
        policy_id = str(payload.get("policy_id", ""))
        new_status = str(payload.get("status") or payload.get("target_status") or "")
        record = self._policy_registry.get_record(policy_id)
        gate = self.policy_promotion_gate(policy_id, new_status, payload)
        if not record or not gate["approved"]:
            return {"policy_id": policy_id, "new_status": new_status, "ok": False, "gate": gate}

        evidence = self._policy_promotion_evidence(record, payload)
        if evidence:
            record.metadata["promotion_evidence"] = {
                **dict(record.metadata.get("promotion_evidence") or {}),
                **evidence,
            }

        if not self._policy_registry.promote(policy_id, new_status):
            gate = {**gate, "approved": False, "reason": "registry_promotion_failed"}
            return {"policy_id": policy_id, "new_status": new_status, "ok": False, "gate": gate}

        now = datetime.now(timezone.utc).isoformat()
        history = list(record.metadata.get("promotion_history") or [])
        history.append({
            "ts": now,
            "from_status": gate["current_status"],
            "to_status": new_status,
            "gate": gate,
        })
        record.metadata["promotion_history"] = history[-50:]
        record.metadata["promoted_at"] = now
        record.metadata["last_promotion_gate"] = gate
        return {
            "policy_id": policy_id,
            "new_status": new_status,
            "ok": True,
            "gate": gate,
            "policy": self._policy_payload(record),
        }

    def rollback_policy(self, payload: dict) -> dict:
        policy_id = str(payload.get("policy_id", ""))
        record = self._policy_registry.get_record(policy_id)
        if record is None:
            return {"policy_id": policy_id, "status": "disabled", "ok": False, "reason": "policy_not_found"}
        previous = record.status
        if not self._policy_registry.promote(policy_id, "disabled"):
            return {"policy_id": policy_id, "status": "disabled", "ok": False, "reason": "rollback_failed"}
        now = datetime.now(timezone.utc).isoformat()
        history = list(record.metadata.get("promotion_history") or [])
        history.append({"ts": now, "from_status": previous, "to_status": "disabled", "reason": "rollback"})
        record.metadata["promotion_history"] = history[-50:]
        record.metadata["disabled_at"] = now
        return {"policy_id": policy_id, "status": "disabled", "ok": True, "previous_status": previous}

    def _live_canary_evidence_trace_ids(self) -> tuple[list[str], set[str]]:
        trace_ids: list[str] = []
        paper_days: set[str] = set()
        try:
            events = self.paper_learning_journal.recent_events(limit=5000, execution_mode="PAPER")
        except Exception:
            events = []
        for event in reversed(events):
            trace_id = str(event.get("trace_id") or "")
            if trace_id:
                trace_ids.append(trace_id)
            occurred_at = str(event.get("occurred_at") or "")
            if occurred_at:
                paper_days.add(occurred_at[:10])
        return list(dict.fromkeys(trace_ids)), paper_days

    def _live_canary_policy_candidates(self) -> list[dict]:
        candidates = []
        for record in self._policy_registry._records.values():
            if record.status not in {"paper", "live_canary", "live_approved"}:
                continue
            gate = (
                {"approved": True, "reason": "already_in_live_canary_stage"}
                if record.status in {"live_canary", "live_approved"}
                else self._policy_promotion_gate_for_record(record, "live_canary", {})
            )
            candidates.append({
                "policy_id": record.policy_id,
                "role": record.role,
                "status": record.status,
                "gate": gate,
            })
        return candidates

    def policy_promotion_gate(self, policy_id: str, target_status: str, payload: dict | None = None) -> dict:
        record = self._policy_registry.get_record(policy_id)
        if record is None:
            return {
                "approved": False,
                "reason": "policy_not_found",
                "policy_id": policy_id,
                "current_status": None,
                "target_status": target_status,
                "checks": [self._promotion_check("policy_exists", False, True, False)],
                "metrics": {},
                "requirements": self._policy_promotion_requirements,
            }
        return self._policy_promotion_gate_for_record(record, target_status, payload or {})

    def _policy_payload(self, record: PolicyRecord) -> dict:
        next_status = self._next_policy_status(record.status)
        payload = {
            "policy_id": record.policy_id,
            "role": record.role,
            "status": record.status,
            "version": record.version,
            "metadata": record.metadata,
            "can_submit_live_orders": record.can_submit_live_orders,
            "promoted_at": record.metadata.get("promoted_at"),
            "rollback_pointer": record.metadata.get("rollback_pointer"),
        }
        if next_status:
            payload["promotion_gate"] = self._policy_promotion_gate_for_record(record, next_status, {})
        else:
            payload["promotion_gate"] = {
                "approved": False,
                "reason": "no_next_status",
                "current_status": record.status,
                "target_status": None,
                "checks": [],
                "metrics": {},
                "requirements": self._policy_promotion_requirements,
            }
        return payload

    @staticmethod
    def _next_policy_status(status: str) -> str | None:
        if status in _POLICY_TERMINAL_STATUSES:
            return None
        if status not in _POLICY_STATUS_ORDER:
            return None
        idx = _POLICY_STATUS_ORDER.index(status)
        if idx >= len(_POLICY_STATUS_ORDER) - 1:
            return None
        return _POLICY_STATUS_ORDER[idx + 1]

    @staticmethod
    def _promotion_check(name: str, passed: bool, required, actual, reason: str | None = None) -> dict:
        return {
            "name": name,
            "passed": bool(passed),
            "required": required,
            "actual": actual,
            "reason": reason or ("" if passed else name),
        }

    def _policy_promotion_gate_for_record(
        self,
        record: PolicyRecord,
        target_status: str,
        payload: dict,
    ) -> dict:
        valid_statuses = set(_POLICY_STATUS_ORDER) | _POLICY_TERMINAL_STATUSES
        expected_next = self._next_policy_status(record.status)
        checks = [
            self._promotion_check("policy_exists", True, True, True),
            self._promotion_check("target_status_valid", target_status in valid_statuses, sorted(valid_statuses), target_status),
            self._promotion_check("policy_not_disabled", record.status != "disabled", True, record.status),
            self._promotion_check("single_step_forward", target_status == expected_next, expected_next, target_status),
        ]

        policy = self._policy_registry.get(record.policy_id)
        policy_can_live = bool(getattr(policy, "can_submit_live_orders", False))
        checks.append(
            self._promotion_check(
                "advisory_only_policy",
                not policy_can_live and not record.can_submit_live_orders,
                "policies must not submit live orders directly",
                {"policy": policy_can_live, "record": record.can_submit_live_orders},
            )
        )

        evidence = self._policy_promotion_evidence(record, payload)
        metrics = self._policy_promotion_metrics(evidence)
        if target_status in {"live_canary", "live_approved"}:
            checks.extend(self._paper_readiness_checks(metrics))
        if target_status == "live_approved":
            manual_approval = bool(
                payload.get("manual_approval")
                or evidence.get("manual_approval")
                or evidence.get("live_approval_acknowledged")
            )
            checks.append(
                self._promotion_check(
                    "manual_live_approval",
                    manual_approval,
                    True,
                    manual_approval,
                    "manual_live_approval_required",
                )
            )

        approved = all(check["passed"] for check in checks)
        failed = [check["reason"] or check["name"] for check in checks if not check["passed"]]
        return {
            "approved": approved,
            "reason": "approved" if approved else failed[0],
            "policy_id": record.policy_id,
            "current_status": record.status,
            "target_status": target_status,
            "checks": checks,
            "metrics": metrics,
            "requirements": self._policy_promotion_requirements,
        }

    def _paper_readiness_checks(self, metrics: dict) -> list[dict]:
        req = self._policy_promotion_requirements
        return [
            self._promotion_check("trace_replay_evidence_present", metrics["trace_count"] > 0, ">0", metrics["trace_count"]),
            self._promotion_check(
                "trace_replay_complete",
                metrics["complete_replay_count"] >= req["min_complete_replays"],
                req["min_complete_replays"],
                metrics["complete_replay_count"],
            ),
            self._promotion_check(
                "paper_trading_days",
                metrics["paper_trading_days"] >= req["min_paper_trading_days"],
                req["min_paper_trading_days"],
                metrics["paper_trading_days"],
            ),
            self._promotion_check(
                "paper_fill_count",
                metrics["fill_count"] >= req["min_paper_fills"],
                req["min_paper_fills"],
                metrics["fill_count"],
            ),
            self._promotion_check(
                "paper_label_count",
                metrics["label_count"] >= req["min_paper_labels"],
                req["min_paper_labels"],
                metrics["label_count"],
            ),
            self._promotion_check(
                "slippage_record_count",
                metrics["slippage_count"] >= req["min_slippage_records"],
                req["min_slippage_records"],
                metrics["slippage_count"],
            ),
            self._promotion_check(
                "learning_update_count",
                metrics["learning_update_count"] >= req["min_learning_updates"],
                req["min_learning_updates"],
                metrics["learning_update_count"],
            ),
            self._promotion_check(
                "profit_factor",
                metrics["profit_factor"] >= req["min_profit_factor"],
                req["min_profit_factor"],
                metrics["profit_factor"],
            ),
            self._promotion_check(
                "sharpe",
                metrics["sharpe"] >= req["min_sharpe"],
                req["min_sharpe"],
                metrics["sharpe"],
            ),
            self._promotion_check(
                "max_drawdown",
                metrics["max_drawdown_pct"] <= req["max_drawdown_pct"],
                req["max_drawdown_pct"],
                metrics["max_drawdown_pct"],
            ),
            self._promotion_check(
                "avg_slippage_surprise",
                metrics["avg_slippage_surprise"] <= req["max_avg_slippage_surprise"],
                req["max_avg_slippage_surprise"],
                metrics["avg_slippage_surprise"],
            ),
        ]

    @staticmethod
    def _policy_promotion_evidence(record: PolicyRecord, payload: dict) -> dict:
        evidence = dict(record.metadata.get("promotion_evidence") or {})
        payload_evidence = payload.get("promotion_evidence") or payload.get("evidence") or {}
        if isinstance(payload_evidence, dict):
            evidence.update(payload_evidence)
        trace_ids = []
        for raw in (
            evidence.get("trace_ids"),
            evidence.get("evidence_trace_ids"),
            payload.get("trace_ids"),
            payload.get("evidence_trace_ids"),
        ):
            if isinstance(raw, str):
                trace_ids.append(raw)
            elif isinstance(raw, list):
                trace_ids.extend(str(item) for item in raw if item)
        if payload.get("trace_id"):
            trace_ids.append(str(payload["trace_id"]))
        if trace_ids:
            evidence["trace_ids"] = list(dict.fromkeys(trace_ids))
        return evidence

    def _policy_promotion_metrics(self, evidence: dict) -> dict:
        trace_ids = [str(item) for item in evidence.get("trace_ids", []) if item]
        labels: list[dict] = []
        slippage_rows: list[dict] = []
        journal_days: set[str] = set()
        fill_count = 0
        slippage_count = 0
        learning_update_count = 0
        complete_replays = 0
        missing_traces: list[str] = []
        for trace_id in trace_ids:
            replay = self.trace_replay(trace_id)
            if replay is None:
                missing_traces.append(trace_id)
                continue
            summary = replay.get("summary") or {}
            fill_count += int(summary.get("fill_count") or summary.get("trade_count") or 0)
            slippage_count += int(summary.get("slippage_count") or 0)
            learning_update_count += int(summary.get("learning_update_count") or 0)
            labels.extend(replay.get("labels") or [])
            slippage_rows.extend(replay.get("slippage") or [])
            if summary.get("lifecycle_complete"):
                complete_replays += 1
            for event in (replay.get("paper_journal") or {}).get("events", []):
                occurred_at = str(event.get("occurred_at") or "")
                if occurred_at:
                    journal_days.add(occurred_at[:10])

        returns = [self._float_or_none(label.get("forward_return_pct")) for label in labels]
        returns = [value for value in returns if value is not None]
        gross_profit = sum(value for value in returns if value > 0)
        gross_loss = abs(sum(value for value in returns if value < 0))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = 999.0
        else:
            profit_factor = 0.0

        if len(returns) > 1:
            mean_return = sum(returns) / len(returns)
            variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
            stddev = math.sqrt(max(0.0, variance))
            sharpe = mean_return / stddev * math.sqrt(min(252, len(returns))) if stddev > 0 else (999.0 if mean_return > 0 else 0.0)
        elif len(returns) == 1:
            sharpe = 999.0 if returns[0] > 0 else 0.0
        else:
            sharpe = 0.0

        drawdowns = [abs(self._float_or_none(label.get("max_drawdown_pct")) or 0.0) for label in labels]
        max_drawdown = max(drawdowns) if drawdowns else 0.0
        slip_surprises = []
        for row in slippage_rows:
            value = self._float_or_none(row.get("slippage_surprise"))
            if value is not None:
                slip_surprises.append(value)
        if not slip_surprises:
            for label in labels:
                value = self._float_or_none(label.get("slippage_surprise"))
                if value is not None:
                    slip_surprises.append(value)
        avg_slippage_surprise = sum(slip_surprises) / len(slip_surprises) if slip_surprises else 0.0

        metrics = {
            "trace_ids": trace_ids,
            "missing_trace_ids": missing_traces,
            "trace_count": len(trace_ids) - len(missing_traces),
            "complete_replay_count": complete_replays,
            "paper_trading_days": len(journal_days),
            "fill_count": fill_count,
            "label_count": len(labels),
            "slippage_count": slippage_count,
            "learning_update_count": learning_update_count,
            "profit_factor": profit_factor,
            "sharpe": sharpe,
            "max_drawdown_pct": max_drawdown,
            "avg_slippage_surprise": avg_slippage_surprise,
        }
        overrides = {
            "paper_trading_days": ["paper_trading_days", "paper_days"],
            "fill_count": ["paper_fill_count", "fill_count"],
            "label_count": ["paper_label_count", "label_count"],
            "slippage_count": ["slippage_count", "paper_slippage_count"],
            "learning_update_count": ["learning_update_count", "paper_learning_update_count"],
            "profit_factor": ["profit_factor", "paper_profit_factor"],
            "sharpe": ["sharpe", "paper_sharpe"],
            "max_drawdown_pct": ["max_drawdown_pct", "paper_max_drawdown_pct"],
            "avg_slippage_surprise": ["avg_slippage_surprise", "paper_avg_slippage_surprise"],
        }
        for metric, keys in overrides.items():
            for key in keys:
                if key not in evidence:
                    continue
                override = self._float_or_none(evidence.get(key))
                if override is None:
                    continue
                metrics[metric] = max(metrics[metric], int(override)) if metric.endswith("_count") or metric == "paper_trading_days" else override
                break
        return metrics

