"""Qdrant persistence added to VectorMemoryStore 2026-08-29 — RAG memory
previously lived entirely in-process, wiped on every container restart.
These tests exercise the persistence methods against a mocked qdrant_client
(never a real network call — see tests/conftest.py's
_no_real_qdrant_connections for why unit tests must not depend on a real
Qdrant server being up)."""
import unittest
import uuid
import types
from unittest.mock import MagicMock
from trading_platform.agents.vector_memory import VectorMemoryStore, VectorDocument


class TestVectorMemoryStoreQdrant(unittest.TestCase):
    def test_init_no_qdrant_url(self):
        store = VectorMemoryStore(qdrant_url=None)
        self.assertIsNone(store._qdrant)
        doc = VectorDocument(doc_id="test1", content="hello", category="cat", embedding=[0.1])
        # Should not raise even though there's no Qdrant client
        store.add(doc)

    def test_add_with_embedding_calls_upsert(self):
        store = VectorMemoryStore(qdrant_url=None)
        store._qdrant = MagicMock()
        doc = VectorDocument(doc_id="test2", content="hello", category="cat", embedding=[0.1])
        store.add(doc)
        store._qdrant.upsert.assert_called_once()
        call_kwargs = store._qdrant.upsert.call_args[1]
        self.assertEqual(call_kwargs['collection_name'], store._qdrant_collection)
        self.assertEqual(len(call_kwargs['points']), 1)

    def test_add_without_embedding_no_upsert(self):
        store = VectorMemoryStore(qdrant_url=None)
        store._qdrant = MagicMock()
        doc = VectorDocument(doc_id="test3", content="hello", category="cat", embedding=None)
        store.add(doc)
        store._qdrant.upsert.assert_not_called()

    def test_remove_calls_delete(self):
        store = VectorMemoryStore(qdrant_url=None)
        store._qdrant = MagicMock()
        store.remove("test4")
        store._qdrant.delete.assert_called_once()
        call_kwargs = store._qdrant.delete.call_args[1]
        self.assertEqual(len(call_kwargs['points_selector']), 1)

    def test_qdrant_point_id_deterministic(self):
        store = VectorMemoryStore(qdrant_url=None)
        id1 = store._qdrant_point_id("same_id")
        id2 = store._qdrant_point_id("same_id")
        self.assertEqual(id1, id2)

    def test_qdrant_point_id_is_a_valid_uuid(self):
        """Qdrant point IDs must be an unsigned int or a UUID — a doc_id like
        "strat-futures-carry-001" is neither, so _qdrant_point_id must
        produce something Qdrant will actually accept, not just some
        deterministic string."""
        store = VectorMemoryStore(qdrant_url=None)
        point_id = store._qdrant_point_id("strat-futures-carry-001")
        uuid.UUID(point_id)  # raises ValueError if not a valid UUID string

    def test_qdrant_point_id_different_for_different_ids(self):
        store = VectorMemoryStore(qdrant_url=None)
        id1 = store._qdrant_point_id("id_a")
        id2 = store._qdrant_point_id("id_b")
        self.assertNotEqual(id1, id2)

    def test_load_from_qdrant_returns_0_when_no_client(self):
        store = VectorMemoryStore(qdrant_url=None)
        count = store.load_from_qdrant()
        self.assertEqual(count, 0)

    def test_load_from_qdrant_single_page(self):
        store = VectorMemoryStore(qdrant_url=None)
        store._qdrant = MagicMock()
        fake_record = types.SimpleNamespace(
            payload={"doc_id": "x", "content": "y", "category": "z", "tags": [], "ts": "2023-01-01T00:00:00", "metadata": {}},
            vector=[0.1, 0.2, 0.3]
        )
        store._qdrant.scroll.return_value = ([fake_record], None)
        count = store.load_from_qdrant()
        self.assertEqual(count, 1)
        self.assertIn("x", store._docs)
        self.assertEqual(store._docs["x"].content, "y")
        self.assertEqual(store._docs["x"].embedding, [0.1, 0.2, 0.3])

    def test_load_from_qdrant_pagination(self):
        store = VectorMemoryStore(qdrant_url=None)
        store._qdrant = MagicMock()
        rec_a = types.SimpleNamespace(payload={"doc_id": "a", "content": "c1", "category": "cat", "tags": [], "ts": "2023-01-01T00:00:00", "metadata": {}}, vector=[0.1])
        rec_b = types.SimpleNamespace(payload={"doc_id": "b", "content": "c2", "category": "cat", "tags": [], "ts": "2023-01-01T00:00:00", "metadata": {}}, vector=[0.2])
        
        def scroll_side_effect(collection_name, limit, offset=None, with_payload=True, with_vectors=True):
            if offset is None:
                return ([rec_a], "page2_token")
            else:
                return ([rec_b], None)
                
        store._qdrant.scroll.side_effect = scroll_side_effect
        count = store.load_from_qdrant()
        self.assertEqual(count, 2)
        self.assertEqual(store._qdrant.scroll.call_count, 2)
        second_call_kwargs = store._qdrant.scroll.call_args_list[1][1]
        self.assertEqual(second_call_kwargs['offset'], "page2_token")

    def test_load_from_qdrant_handles_scroll_exception(self):
        store = VectorMemoryStore(qdrant_url=None)
        store._qdrant = MagicMock()
        store._qdrant.scroll.side_effect = Exception("connection lost")
        count = store.load_from_qdrant()
        self.assertIsInstance(count, int)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()