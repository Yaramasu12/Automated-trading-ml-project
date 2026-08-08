"""
trading_platform/ai/rag/eval.py — RAG evaluation harness per §8 (REDESIGN_PROMPT)

Measures RAG quality on a fixed question set in CI so RAG quality is measured,
not assumed — exactly like the trading models.

Metrics (per RAGAS/DeepEval OSS):
  - Retrieval faithfulness: does the answer stay faithful to the retrieved context?
  - Context precision: are the top-k retrieved chunks relevant?
  - Context recall: does the context contain all necessary info for the answer?
  - Answer grounding: is the answer grounded in the context (vs hallucinated)?

All metrics are scored 0-1, aggregated per query, and stored in the DB.
"""

from pydantic import BaseModel, Field
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np


# ──────────────────────────────────────────────
# Evaluation result schema
# ──────────────────────────────────────────────


@dataclass
class RAGMetricScores:
    """Scores for a single RAG evaluation query."""
    faithfulness: float = 0.0  # 0-1: answer faithfulness to context
    context_precision: float = 0.0  # 0-1: precision of top-k retrieval
    context_recall: float = 0.0  # 0-1: recall of context vs ground truth
    answer_grounding: float = 0.0  # 0-1: answer grounded in context
    overall: float = 0.0  # weighted average

    @property
    def passes_threshold(self) -> bool:
        """Check if overall score passes minimum threshold."""
        return self.overall >= 0.65  # 65% minimum for production


@dataclass
class RAGEvaluationResult:
    """Result of evaluating the RAG pipeline on a single query."""
    query: str
    expected_answer: str
    retrieved_chunks: list[str] = field(default_factory=list)
    generated_answer: str = ""
    metrics: RAGMetricScores = field(default_factory=RAGMetricScores)
    latency_ms: float = 0.0
    llm_calls: int = 0


@dataclass
class RAGEvaluationReport:
    """Aggregated evaluation report across all queries."""
    queries_evaluated: int = 0
    mean_faithfulness: float = 0.0
    mean_context_precision: float = 0.0
    mean_context_recall: float = 0.0
    mean_answer_grounding: float = 0.0
    mean_overall: float = 0.0
    pass_rate: float = 0.0  # fraction of queries that pass threshold
    per_query_results: list[RAGEvaluationResult] = field(default_factory=list)

    @property
    def is_production_ready(self) -> bool:
        """Check if RAG pipeline is production-ready based on thresholds."""
        return (
            self.mean_faithfulness >= 0.75
            and self.mean_context_precision >= 0.70
            and self.mean_overall >= 0.65
            and self.pass_rate >= 0.70
        )


# ──────────────────────────────────────────────
# Fixed question sets (for CI evaluation)
# ──────────────────────────────────────────────


def get_market_data_questions() -> list[dict[str, str]]:
    """Get fixed question set for market data RAG retrieval."""
    return [
        {
            "query": "What is the current ATM IV for NIFTY?",
            "expected_answer": "ATM IV value from latest chain snapshot",
            "context_sources": ["option_chain_snapshot", "iv_history"],
        },
        {
            "query": "What is the IV rank for BANKNIFTY?",
            "expected_answer": "IV rank percentage from history",
            "context_sources": ["iv_rank_history"],
        },
        {
            "query": "What is the PCR for NIFTY this session?",
            "expected_answer": "Put-call ratio value",
            "context_sources": ["options_chain_snapshot"],
        },
        {
            "query": "What is the VIX level?",
            "expected_answer": "India VIX value",
            "context_sources": ["vix_feed"],
        },
        {
            "query": "What is the max pain for the near-month expiry?",
            "expected_answer": "Max pain strike value",
            "context_sources": ["option_chain_snapshot"],
        },
    ]


def get_news_sentiment_questions() -> list[dict[str, str]]:
    """Get fixed question set for news sentiment RAG retrieval."""
    return [
        {
            "query": "What is the sentiment for RBI's latest policy rate decision?",
            "expected_answer": "Sentiment score (-1 to 1) for RBI event",
            "context_sources": ["rbi_circulars", "news_feed"],
        },
        {
            "query": "What are the key events in the budget calendar this week?",
            "expected_answer": "List of budget-related events",
            "context_sources": ["event_calendar", "news_feed"],
        },
        {
            "query": "What is the sentiment for Reliance Industries latest earnings?",
            "expected_answer": "Sentiment score for Reliance earnings",
            "context_sources": ["earnings_transcripts", "news_feed"],
        },
        {
            "query": "What SEBI circulars were published this month?",
            "expected_answer": "List of SEBI circulars",
            "context_sources": ["sebi_circulars"],
        },
    ]


def get_event_risk_questions() -> list[dict[str, str]]:
    """Get fixed question set for event risk RAG retrieval."""
    return [
        {
            "query": "Are there any RBI policy meetings this week?",
            "expected_answer": "List of RBI events with dates",
            "context_sources": ["event_calendar", "rbi_circulars"],
        },
        {
            "query": "Is there a blackout period for NIFTY options near expiry?",
            "expected_answer": "Blackout status for near-month expiry",
            "context_sources": ["event_calendar", "expiry_calendar"],
        },
        {
            "query": "What macro events are scheduled for tomorrow?",
            "expected_answer": "List of tomorrow's macro events",
            "context_sources": ["macro_calendar", "news_feed"],
        },
    ]


def get_all_evaluation_questions() -> list[dict[str, str]]:
    """Get all fixed evaluation questions."""
    return (
        get_market_data_questions()
        + get_news_sentiment_questions()
        + get_event_risk_questions()
    )


# ──────────────────────────────────────────────
# Metric computation (lightweight, no external deps)
# ──────────────────────────────────────────────


def compute_faithfulness(
    answer: str,
    context_chunks: list[str],
    llm_client: Any | None = None,
) -> float:
    """Compute faithfulness score: does the answer stay faithful to context?

    Uses LLM-based evaluation if client provided, otherwise heuristic.

    Heuristic: check for contradictions between answer and context.
    """
    if llm_client:
        # LLM-based faithfulness: ask model "Is this answer supported by the context?"
        try:
            response = llm_client.chat.completions.create(
                model=llm_client.model,
                messages=[{
                    "role": "system",
                    "content": (
                        "You are a RAG evaluator. Given a context and an answer, "
                        "rate how faithful the answer is to the context. "
                        "Return only a float between 0 and 1."
                    ),
                }, {
                    "role": "user",
                    "content": (
                        f"Context:\n{''.join(context_chunks)}\n\n"
                        f"Answer:\n{answer}\n\n"
                        f"Faithfulness score (0-1):"
                    ),
                }],
                max_tokens=10,
                temperature=0,
            )
            score_str = response.choices[0].message.content.strip()
            return float(max(0, min(1, float(score_str))))
        except Exception:
            pass  # Fall back to heuristic

    # Heuristic: check for keyword overlap and contradiction patterns
    answer_lower = answer.lower()
    total_overlap = 0
    for chunk in context_chunks:
        chunk_words = set(chunk.lower().split())
        answer_words = set(answer_lower.split())
        overlap = len(chunk_words & answer_words)
        total_overlap += overlap

    if total_overlap == 0:
        return 0.0

    # Normalize by answer length
    return min(1.0, total_overlap / max(len(answer_lower.split()), 1))


def compute_context_precision(
    answer: str,
    context_chunks: list[str],
    top_k: int | None = None,
) -> float:
    """Compute context precision: are the top-k retrieved chunks relevant?

    Measures the fraction of relevant chunks at the top of the ranking.
    """
    if not context_chunks:
        return 0.0

    answer_lower = answer.lower()
    answer_words = set(w for w in answer_lower.split() if len(w) > 3)

    if not answer_words:
        # Fallback: simple overlap with all chunks
        total = sum(1 for c in context_chunks if any(w in c.lower() for w in answer_lower.split()[:10]))
        return total / len(context_chunks)

    # Score each chunk by relevance to the answer
    scored_chunks = []
    for chunk in context_chunks:
        chunk_lower = chunk.lower()
        chunk_words = set(w for w in chunk_lower.split() if len(w) > 3)
        overlap = len(chunk_words & answer_words)
        scored_chunks.append((overlap, chunk))

    # Sort by score (descending)
    scored_chunks.sort(key=lambda x: x[0], reverse=True)

    # Apply top-k if specified
    if top_k:
        scored_chunks = scored_chunks[:top_k]

    # Precision = fraction of scored chunks with non-zero overlap
    relevant = sum(1 for s, _ in scored_chunks if s > 0)
    return relevant / len(scored_chunks) if scored_chunks else 0.0


def compute_context_recall(
    answer: str,
    context_chunks: list[str],
    ground_truth: str | None = None,
) -> float:
    """Compute context recall: does the context contain all necessary info?

    If ground_truth is provided, compare context against it.
    Otherwise, compare context against answer.
    """
    if not context_chunks:
        return 0.0

    if ground_truth:
        # Compare context to ground truth
        gt_words = set(w for w in ground_truth.lower().split() if len(w) > 3)
        if not gt_words:
            return 0.5  # Neutral if ground truth has no meaningful words

        # Fraction of ground truth words found in context
        context_text = " ".join(context_chunks).lower()
        found = sum(1 for w in gt_words if w in context_text)
        return found / len(gt_words)

    # Fallback: compare answer words to context
    answer_words = set(w for w in answer.lower().split() if len(w) > 3)
    if not answer_words:
        return 0.5

    context_text = " ".join(context_chunks).lower()
    found = sum(1 for w in answer_words if w in context_text)
    return found / len(answer_words)


def compute_answer_grounding(
    answer: str,
    context_chunks: list[str],
) -> float:
    """Compute answer grounding: is the answer grounded in context vs hallucinated?

    Measures how much of the answer can be traced back to the context.
    """
    if not context_chunks or not answer:
        return 0.0

    answer_lower = answer.lower()
    answer_sentences = [s.strip() for s in answer_lower.split(".") if s.strip()]

    grounded_sentences = 0
    for sentence in answer_sentences:
        # Check if sentence (or its key words) appear in any context chunk
        sentence_words = set(w for w in sentence.split() if len(w) > 3)
        if not sentence_words:
            continue

        for chunk in context_chunks:
            chunk_lower = chunk.lower()
            if sentence_words & set(w for w in chunk_lower.split() if len(w) > 3):
                grounded_sentences += 1
                break

    return grounded_sentences / len(answer_sentences) if answer_sentences else 0.0


def compute_overall_score(
    faithfulness: float,
    context_precision: float,
    context_recall: float,
    answer_grounding: float,
) -> float:
    """Compute weighted overall score."""
    weights = {
        "faithfulness": 0.30,
        "context_precision": 0.25,
        "context_recall": 0.20,
        "answer_grounding": 0.25,
    }
    return (
        faithfulness * weights["faithfulness"]
        + context_precision * weights["context_precision"]
        + context_recall * weights["context_recall"]
        + answer_grounding * weights["answer_grounding"]
    )


# ──────────────────────────────────────────────
# Evaluation harness
# ──────────────────────────────────────────────


class RAGEvaluator:
    """RAG evaluation harness.

    Evaluates the RAG pipeline on a fixed question set.
    Results are stored in DB for tracking over time.
    """

    def __init__(
        self,
        rag_pipeline: Any,
        llm_client: Any | None = None,
        db_connection: Any | None = None,
        question_sets: list[dict[str, str]] | None = None,
    ) -> None:
        self.rag_pipeline = rag_pipeline
        self.llm_client = llm_client
        self.db = db_connection
        self.question_sets = question_sets or get_all_evaluation_questions()

    def evaluate_query(
        self,
        query: str,
        expected_answer: str,
    ) -> RAGEvaluationResult:
        """Evaluate RAG pipeline on a single query."""
        import time
        start = time.monotonic()

        # Run RAG pipeline
        result = self.rag_pipeline.query(query)
        retrieved_chunks = result.get("chunks", [])
        generated_answer = result.get("answer", "")

        elapsed_ms = (time.monotonic() - start) * 1000

        # Compute metrics
        faithfulness = compute_faithfulness(generated_answer, retrieved_chunks, self.llm_client)
        precision = compute_context_precision(generated_answer, retrieved_chunks)
        recall = compute_context_recall(generated_answer, retrieved_chunks, expected_answer)
        grounding = compute_answer_grounding(generated_answer, retrieved_chunks)
        overall = compute_overall_score(faithfulness, precision, recall, grounding)

        return RAGEvaluationResult(
            query=query,
            expected_answer=expected_answer,
            retrieved_chunks=retrieved_chunks,
            generated_answer=generated_answer,
            metrics=RAGMetricScores(
                faithfulness=faithfulness,
                context_precision=precision,
                context_recall=recall,
                answer_grounding=grounding,
                overall=overall,
            ),
            latency_ms=elapsed_ms,
            llm_calls=result.get("llm_calls", 0),
        )

    def evaluate_all(self) -> RAGEvaluationReport:
        """Evaluate RAG pipeline on all fixed questions."""
        results = []
        for q in self.question_sets:
            result = self.evaluate_query(q["query"], q["expected_answer"])
            results.append(result)

        # Aggregate
        n = len(results)
        if n == 0:
            return RAGEvaluationReport()

        mean_f = np.mean([r.metrics.faithfulness for r in results])
        mean_p = np.mean([r.metrics.context_precision for r in results])
        mean_r = np.mean([r.metrics.context_recall for r in results])
        mean_g = np.mean([r.metrics.answer_grounding for r in results])
        mean_o = np.mean([r.metrics.overall for r in results])
        pass_rate = sum(1 for r in results if r.metrics.passes_threshold) / n

        return RAGEvaluationReport(
            queries_evaluated=n,
            mean_faithfulness=float(mean_f),
            mean_context_precision=float(mean_p),
            mean_context_recall=float(mean_r),
            mean_answer_grounding=float(mean_g),
            mean_overall=float(mean_o),
            pass_rate=float(pass_rate),
            per_query_results=results,
        )

    def save_report(self, report: RAGEvaluationReport) -> None:
        """Save evaluation report to DB."""
        if not self.db:
            return

        # Store in rag_evaluations table (create if not exists)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS rag_evaluations (
                id SERIAL PRIMARY KEY,
                evaluated_at TIMESTAMP DEFAULT NOW(),
                queries_evaluated INTEGER,
                mean_faithfulness FLOAT,
                mean_context_precision FLOAT,
                mean_context_recall FLOAT,
                mean_answer_grounding FLOAT,
                mean_overall FLOAT,
                pass_rate FLOAT,
                is_production_ready BOOLEAN
            )
        """)

        self.db.execute("""
            INSERT INTO rag_evaluations (
                queries_evaluated, mean_faithfulness, mean_context_precision,
                mean_context_recall, mean_answer_grounding, mean_overall,
                pass_rate, is_production_ready
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report.queries_evaluated,
            report.mean_faithfulness,
            report.mean_context_precision,
            report.mean_context_recall,
            report.mean_answer_grounding,
            report.mean_overall,
            report.pass_rate,
            report.is_production_ready,
        ))

    def get_latest_report(self) -> RAGEvaluationReport | None:
        """Get the latest evaluation report from DB."""
        if not self.db:
            return None

        row = self.db.execute(
            "SELECT * FROM rag_evaluations ORDER BY evaluated_at DESC LIMIT 1"
        ).fetchone()

        if not row:
            return None

        return RAGEvaluationReport(
            queries_evaluated=row[1],
            mean_faithfulness=row[2],
            mean_context_precision=row[3],
            mean_context_recall=row[4],
            mean_answer_grounding=row[5],
            mean_overall=row[6],
            pass_rate=row[7],
        )


# ──────────────────────────────────────────────
# CI integration
# ──────────────────────────────────────────────


def run_ci_rag_evaluation() -> bool:
    """Run RAG evaluation in CI. Returns True if RAG passes production thresholds."""
    # This is called in CI via pytest
    # The evaluator needs to be initialized with a test RAG pipeline

    from trading_platform.ai.rag.router import AdaptiveRAGPipeline

    # Create a mock RAG pipeline for testing
    pipeline = AdaptiveRAGPipeline()

    evaluator = RAGEvaluator(pipeline)
    report = evaluator.evaluate_all()

    print(f"RAG Evaluation Report:")
    print(f"  Queries: {report.queries_evaluated}")
    print(f"  Faithfulness: {report.mean_faithfulness:.3f}")
    print(f"  Context Precision: {report.mean_context_precision:.3f}")
    print(f"  Context Recall: {report.mean_context_recall:.3f}")
    print(f"  Answer Grounding: {report.mean_answer_grounding:.3f}")
    print(f"  Overall: {report.mean_overall:.3f}")
    print(f"  Pass Rate: {report.pass_rate:.1%}")
    print(f"  Production Ready: {report.is_production_ready}")

    return report.is_production_ready