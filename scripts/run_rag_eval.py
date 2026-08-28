"""Make ai/rag/eval.py's RAGEvaluator actually runnable.

RAGEvaluator was fully implemented (faithfulness/context-precision/
context-recall/answer-grounding metrics, a CI-gate function) but had ZERO
callers anywhere in the codebase — it expects a `rag_pipeline` object with
`.query(q) -> {"chunks": [...], "answer": "...", "llm_calls": N}`, which
doesn't exist: the real, wired RAG system (agents/vector_memory.py's
RAGRetriever) only retrieves evidence (`.retrieve(query) -> list[EvidenceRef]`)
— nothing in this codebase generates a grounded ANSWER from that evidence as
a standalone callable. RAGPipelineAdapter below bridges the two: retrieve via
the real VectorMemoryStore, then generate a real grounded answer via the
local LLM (same thinking-model reasoning_content fallback fixed in
llm_researcher.py this session).

NOTE ON SCOPE: this evaluates a freshly-constructed VectorMemoryStore seeded
with only its default docs (seed_defaults()), the same as a fresh app
boot — it does NOT reach into the already-running trading-api container's
live, accumulated RAG state (there's no API endpoint exposing that; wiring
one is a reasonable follow-up, not done here). This still verifies the eval
framework itself actually works end-to-end, which it never had before.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.agents.vector_memory import VectorMemoryStore, RAGRetriever
from trading_platform.ai.rag.eval import (
    RAGEvaluator,
    get_all_evaluation_questions,
    get_market_data_questions,
    get_news_sentiment_questions,
    get_event_risk_questions,
)
from trading_platform.research.llm_researcher import LocalLLMClient


class RAGPipelineAdapter:
    """Bridges RAGRetriever (retrieval-only) to what RAGEvaluator expects
    (a full retrieve-then-generate pipeline)."""

    def __init__(self, retriever: RAGRetriever, llm: LocalLLMClient) -> None:
        self._retriever = retriever
        self._llm = llm

    def query(self, query: str) -> dict:
        refs = self._retriever.retrieve(query, top_k=4)
        chunks = [r.excerpt for r in refs]
        if not chunks:
            return {"chunks": [], "answer": "", "llm_calls": 0}
        context = "\n".join(f"- {c}" for c in chunks)
        system = (
            "Answer the user's question using ONLY the provided context. "
            "If the context doesn't contain the answer, say so plainly — "
            "never invent a number or fact not present in the context."
        )
        user = f"Context:\n{context}\n\nQuestion: {query}\n\nAnswer concisely (1-2 sentences)."
        answer = self._llm.complete(system, user, temperature=0.2)
        return {"chunks": chunks, "answer": answer.strip(), "llm_calls": 1}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "sample"
    if which == "all":
        questions = get_all_evaluation_questions()
    elif which == "sample":
        # One from each category — fast smoke test of the framework itself,
        # not a full production RAG quality audit (that's `all`, ~12 slow
        # local-model calls).
        questions = [
            get_market_data_questions()[0],
            get_news_sentiment_questions()[0],
            get_event_risk_questions()[0],
        ]
    else:
        raise SystemExit(f"usage: {sys.argv[0]} [sample|all]")

    store = VectorMemoryStore()
    store.seed_defaults()
    retriever = RAGRetriever(store)
    llm = LocalLLMClient(model="qwen/qwen3.6-35b-a3b", max_tokens=16000, timeout=900)
    adapter = RAGPipelineAdapter(retriever, llm)

    evaluator = RAGEvaluator(rag_pipeline=adapter, question_sets=questions)
    report = evaluator.evaluate_all()

    print(f"Evaluated {report.queries_evaluated} queries (mode={which}, "
          f"store seeded with {len(store._docs) if hasattr(store, '_docs') else '?'} docs)")
    print(f"mean_faithfulness      : {report.mean_faithfulness:.3f}")
    print(f"mean_context_precision : {report.mean_context_precision:.3f}")
    print(f"mean_context_recall    : {report.mean_context_recall:.3f}")
    print(f"mean_answer_grounding  : {report.mean_answer_grounding:.3f}")
    print(f"mean_overall           : {report.mean_overall:.3f}")
    print(f"pass_rate              : {report.pass_rate:.1%}")
    print()
    for r in report.per_query_results:
        print(f"[{'PASS' if r.metrics.passes_threshold else 'fail'}] {r.query}")
        print(f"    chunks retrieved: {len(r.retrieved_chunks)}")
        print(f"    answer: {r.generated_answer[:200]!r}")
        print(f"    overall={r.metrics.overall:.2f} faithfulness={r.metrics.faithfulness:.2f} "
              f"precision={r.metrics.context_precision:.2f} recall={r.metrics.context_recall:.2f} "
              f"grounding={r.metrics.answer_grounding:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
