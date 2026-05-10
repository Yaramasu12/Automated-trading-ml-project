from __future__ import annotations

"""Voting and consensus calculation for the agent council."""

from collections import Counter
from typing import Literal

from trading_platform.agents.schemas import AgentVote


def compute_consensus(votes: list[AgentVote]) -> tuple[str, float, float]:
    """Return (plurality_action, confidence_mean, consensus_score 0-1).

    consensus_score = fraction of votes that agree with plurality action.
    Low consensus_score means high disagreement.
    """
    if not votes:
        return "HOLD", 0.0, 0.0

    action_weights: Counter = Counter()
    for v in votes:
        action_weights[v.action] += v.confidence

    plurality_action = action_weights.most_common(1)[0][0]
    total_weight = sum(action_weights.values())
    consensus_score = action_weights[plurality_action] / total_weight if total_weight > 0 else 0.0
    confidence_mean = sum(v.confidence for v in votes) / len(votes)

    return plurality_action, confidence_mean, consensus_score


def has_veto(votes: list[AgentVote]) -> bool:
    return any(v.action == "HALT" for v in votes)


def aggregate_to_action(
    plurality_action: str,
    consensus_score: float,
    confidence_mean: float,
    consensus_threshold: float = 0.55,
    confidence_threshold: float = 0.45,
) -> Literal["PROCEED", "REDUCE", "HALT", "NO_TRADE"]:
    """Convert council vote summary into a final action."""
    if plurality_action == "HALT":
        return "HALT"
    if consensus_score < consensus_threshold:
        return "NO_TRADE"
    if confidence_mean < confidence_threshold:
        return "NO_TRADE"
    if plurality_action in ("BUY", "SELL"):
        return "PROCEED"
    if plurality_action == "REDUCE":
        return "REDUCE"
    if plurality_action == "HEDGE":
        return "REDUCE"
    return "NO_TRADE"
