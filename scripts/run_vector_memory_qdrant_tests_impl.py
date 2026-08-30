"""Delegate writing tests/test_vector_memory_qdrant.py to the local LLM,
acting as a professional developer, per the same propose-then-verify
discipline as scripts/run_mcp_implementation.py: the model's output gets
written to a file, then reviewed, run, and fixed here before being trusted
— it is not applied blindly.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_platform.research.llm_researcher import LocalLLMClient

SYSTEM_PROMPT = """You are a professional Python developer who writes precise, minimal
unit tests using unittest and unittest.mock. You NEVER let a test make a real network
call — every external dependency is mocked. You always output a complete, single,
runnable Python file inside one ```python code fence and nothing else outside that fence."""

TASK_PROMPT = '''Write `tests/test_vector_memory_qdrant.py` — unit tests for the Qdrant
persistence methods just added to `trading_platform/agents/vector_memory.py`'s
`VectorMemoryStore` class. Use `unittest.TestCase`, matching this codebase's style
(see the class/method shapes below — copy this style, don't invent a different one).

## The exact code under test (copy this API exactly — do not guess at qdrant_client's shapes)

```python
class VectorMemoryStore:
    def __init__(self, qdrant_url: str | None = None, qdrant_collection: str = "agent_vector_memory") -> None:
        self._docs: dict[str, VectorDocument] = {}
        self._tokens: dict[str, set[str]] = {}
        self._qdrant_collection = qdrant_collection
        self._qdrant = self._init_qdrant(qdrant_url) if qdrant_url else None
        # self._qdrant is None if qdrant_url was None, OR if connecting failed for any reason

    def add(self, doc: VectorDocument) -> None:
        # ... stores doc in self._docs ...
        if doc.embedding is not None:
            self._qdrant_upsert(doc)   # no-ops silently if self._qdrant is None

    def remove(self, doc_id: str) -> None:
        # ... removes from self._docs ...
        if self._qdrant is not None:
            self._qdrant.delete(collection_name=self._qdrant_collection, points_selector=[self._qdrant_point_id(doc_id)])

    @staticmethod
    def _qdrant_point_id(doc_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vector_memory:{doc_id}"))

    def _qdrant_upsert(self, doc: VectorDocument) -> None:
        # no-ops if self._qdrant is None; otherwise calls:
        # self._qdrant.upsert(collection_name=self._qdrant_collection, points=[PointStruct(
        #     id=self._qdrant_point_id(doc.doc_id), vector=doc.embedding,
        #     payload={"doc_id": doc.doc_id, "content": doc.content, "category": doc.category,
        #              "tags": doc.tags, "ts": doc.ts.isoformat(), "metadata": doc.metadata})])

    def load_from_qdrant(self) -> int:
        # Returns 0 immediately if self._qdrant is None.
        # Otherwise calls self._qdrant.scroll(collection_name=..., limit=256, offset=next_offset,
        # with_payload=True, with_vectors=True) in a loop.
        # qdrant_client's real scroll() returns a tuple: (list_of_records, next_offset).
        # next_offset is None when there are no more pages — the loop must stop then.
        # Each record has .payload (a dict with the keys shown in _qdrant_upsert above)
        # and .vector (a list[float] or None).
        # For each record, reconstructs a VectorDocument from record.payload + record.vector
        # and adds it to self._docs (keyed by payload["doc_id"]), returns the total count loaded.
        # Any exception during the whole scroll is caught and returns whatever count was
        # loaded so far (via note_swallowed(), which you can mock as a no-op or just
        # patch trading_platform.agents.vector_memory.note_swallowed).

class VectorDocument:
    # dataclass fields: doc_id: str, content: str, category: str, tags: list[str] = [],
    # ts: datetime = now, metadata: dict = {}, embedding: list[float] | None = None
```

## What to test

1. `VectorMemoryStore(qdrant_url=None)` never attempts a Qdrant connection —
   `store._qdrant is None`, and `add()` on a doc with an embedding doesn't raise even
   though there's no Qdrant client.
2. `add()` on a doc WITH an embedding, when `store._qdrant` is a Mock, calls
   `store._qdrant.upsert(...)` exactly once with `collection_name` matching the
   store's collection and a `points` list of length 1.
3. `add()` on a doc WITHOUT an embedding (embedding=None) does NOT call
   `store._qdrant.upsert()` at all, even when `store._qdrant` is a Mock (only docs
   with a real embedding get persisted — no point vector to store otherwise).
4. `remove()` calls `store._qdrant.delete(...)` with a `points_selector` list
   containing exactly one ID, when `store._qdrant` is a Mock.
5. `_qdrant_point_id(doc_id)` is DETERMINISTIC — calling it twice with the same
   doc_id returns the identical string both times — and is a valid UUID string
   (parseable by `uuid.UUID(...)`  without raising).
6. `_qdrant_point_id` returns DIFFERENT ids for two different doc_ids.
7. `load_from_qdrant()` returns 0 immediately when `store._qdrant is None`, without
   any mock ever being called.
8. `load_from_qdrant()`, given a Mock `store._qdrant` whose `.scroll()` is configured
   to return a single page — `([fake_record], None)` where `fake_record` is a
   `SimpleNamespace` (or Mock) with `.payload = {"doc_id": "x", "content": "y",
   "category": "z", "tags": [], "metadata": {}}` and `.vector = [0.1, 0.2, 0.3]` —
   correctly populates `store._docs["x"]` with a `VectorDocument` whose `.content == "y"`
   and `.embedding == [0.1, 0.2, 0.3]`, and returns `1`.
9. `load_from_qdrant()` handles PAGINATION correctly: `.scroll()` configured with
   `side_effect` to return two pages — first call returns `([record_a], "page2_token")`,
   second call returns `([record_b], None)` — must load BOTH records (returns `2`) and
   must call `.scroll()` exactly twice, with the second call's `offset` kwarg equal to
   `"page2_token"` (proves the pagination offset is actually threaded through, not
   ignored).
10. `load_from_qdrant()` never raises even if `.scroll()` raises an exception — returns
    an int (0, since nothing was loaded before the failure), not None, and not a
    propagated exception.

Mock `qdrant_client`-related behavior by directly setting `store._qdrant = Mock()` (or
`MagicMock()`) AFTER constructing the store with `qdrant_url=None` — do NOT try to mock
the `qdrant_client` import machinery or `_init_qdrant()` itself, that's unnecessary
complexity for testing these specific methods. Import real `VectorMemoryStore` and
`VectorDocument` from `trading_platform.agents.vector_memory`.

Output ONLY the complete file content in a single ```python fence.'''


def extract_code(reply: str) -> str | None:
    m = re.search(r"```python\s*(.*?)```", reply, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```python\s*(.*)", reply, re.DOTALL)
    return m.group(1).strip() if m else None


def main() -> int:
    client = LocalLLMClient(
        model="qwen/qwen3.6-35b-a3b", base_url="http://localhost:1234/v1",
        max_tokens=10000, timeout=900,
    )
    print("Calling local model to write tests/test_vector_memory_qdrant.py ...")
    reply = client.complete(SYSTEM_PROMPT, TASK_PROMPT, temperature=0.2)
    print(f"Got reply, {len(reply)} chars")

    code = extract_code(reply)
    if code is None:
        print("=== COULD NOT EXTRACT CODE — raw reply below ===")
        print(reply)
        return 1

    out_path = Path(__file__).resolve().parents[1] / "tests" / "test_vector_memory_qdrant.py"
    out_path.write_text(code, encoding="utf-8")
    print(f"Wrote {out_path} ({len(code)} chars) — NOT reviewed or run yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
