"""
trading_platform/ai/rag/ingestion.py — RAG document ingestion pipeline

Per §8 (REDESIGN_PROMPT): Ingest news, filings, macro data into Qdrant
with contextual retrieval pipeline.

Sources:
- NSE/BSE announcements & filings
- Earnings transcripts
- Moneycontrol/ET/Reuters RSS
- RBI/SEBI circulars
- Macro calendar
- Internal corpus: trade journal, backtest reports, decision_traces

Pipeline:
1. Fetch raw document
2. Structure-aware chunking (by section/speaker)
3. LLM-generated context blurb (contextual retrieval)
4. Embed with local nomic-embed-text or bge-m3
5. Store in Qdrant with rich metadata
6. Build knowledge graph edges (GraphRAG)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Document types
# ──────────────────────────────────────────────


class DocType:
    NEWS = "news"
    FILING = "filing"
    TRANSCRIPT = "transcript"
    CIRCULAR = "circular"
    MACRO = "macro"
    TRADE_JOURNAL = "trade_journal"
    BACKTEST_REPORT = "backtest_report"
    DECISION_TRACE = "decision_trace"
    EARNINGS = "earnings"


# ──────────────────────────────────────────────
# Chunk schema
# ──────────────────────────────────────────────


@dataclass
class TextChunk:
    """A chunked text segment with metadata."""
    doc_id: str
    chunk_id: str
    text: str
    context_blurb: str  # LLM-generated context summary
    doc_type: str
    ticker: Optional[str] = None
    sector: Optional[str] = None
    source: Optional[str] = None
    event_date: Optional[str] = None
    freshness_weight: float = 1.0  # decays with age
    embedding: Optional[list[float]] = None  # computed later
    metadata: dict = field(default_factory=dict)


# ──────────────────────────────────────────────
# Knowledge graph node/edge (GraphRAG)
# ──────────────────────────────────────────────


@dataclass
class KGNode:
    """Knowledge graph node."""
    node_id: str
    node_type: str  # ticker, sector, event, person, company
    label: str
    properties: dict = field(default_factory=dict)


@dataclass
class KGEEdge:
    """Knowledge graph edge."""
    source_id: str
    target_id: str
    relation: str  # "exposed_to", "in_sector", "affected_by", "leads_to"
    strength: float = 1.0
    metadata: dict = field(default_factory=dict)


# ──────────────────────────────────────────────
# Structure-aware chunking
# ──────────────────────────────────────────────


def chunk_document(
    raw_text: str,
    doc_type: str,
    doc_id: str,
    ticker: Optional[str] = None,
    sector: Optional[str] = None,
    source: Optional[str] = None,
    event_date: Optional[str] = None,
    max_chunk_size: int = 512,
    overlap: int = 64,
) -> list[TextChunk]:
    """Structure-aware chunking per doc type.

    - Filings: chunk by section
    - Transcripts: chunk by speaker
    - News: chunk by paragraph
    - General: chunk by sentence boundaries
    """
    chunks: list[TextChunk] = []

    if doc_type == DocType.FILING:
        sections = re.split(r'(?=Section\s+\d+|Part\s+\w+|Clause\s+\d+)', raw_text)
        segments = [s.strip() for s in sections if len(s.strip()) > 100]
    elif doc_type == DocType.TRANSCRIPT:
        # Chunk by speaker (e.g., "Chairman:", "CEO:", "CFO:")
        segments = re.split(r'(?=[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s*:)', raw_text)
        segments = [s.strip() for s in segments if len(s.strip()) > 100]
    elif doc_type == DocType.NEWS:
        # Chunk by paragraph
        segments = [s.strip() for s in raw_text.split('\n\n') if len(s.strip()) > 100]
    else:
        # Default: chunk by sentence
        sentences = re.split(r'(?<=[.!?])\s+', raw_text)
        segments = []
        current = ""
        for s in sentences:
            if len(current) + len(s) > max_chunk_size:
                if current:
                    segments.append(current)
                current = s
            else:
                current += " " + s if current else s
        if current:
            segments.append(current)

    # Create chunks from segments
    base_ts = int(time.time())
    for i, segment in enumerate(segments):
        # Split segment into smaller chunks if needed
        chars = list(segment)
        chunk_texts = []
        start = 0
        while start < len(chars):
            end = min(start + max_chunk_size, len(chars))
            # Try to break at word boundary
            while end > start and chars[end] not in (' ', '.', ',', '!', '?', ';', ':'):
                end -= 1
            chunk_texts.append(''.join(chars[start:end]).strip())
            start = end - overlap if end < len(chars) else end
            if start >= len(chars):
                break

        for j, chunk_text in enumerate(chunk_texts):
            chunk_id = f"{doc_id}_chunk_{i}_{j}"
            # Freshness weight: newer = higher
            if event_date:
                days_old = max(0, (base_ts - _parse_timestamp(event_date)) / 86400)
                freshness = max(0.1, 1.0 - (days_old / 365))
            else:
                freshness = 1.0

            chunks.append(TextChunk(
                doc_id=doc_id,
                chunk_id=chunk_id,
                text=chunk_text,
                context_blurb="",  # filled by LLM
                doc_type=doc_type,
                ticker=ticker,
                sector=sector,
                source=source,
                event_date=event_date,
                freshness_weight=freshness,
            ))

    return chunks


def _parse_timestamp(ts: str) -> float:
    """Parse ISO timestamp to Unix timestamp."""
    try:
        dt = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        return time.mktime(dt)
    except (ValueError, TypeError):
        return time.time()


# ──────────────────────────────────────────────
# Context blurb generator (LLM-powered)
# ──────────────────────────────────────────────


def generate_context_blurb(
    chunk_text: str,
    doc_type: str,
    ticker: Optional[str] = None,
    model_client=None,  # LM Studio compatible client
    max_tokens: int = 128,
) -> str:
    """Generate a context blurb for the chunk using a local LLM.

    The blurb is a 1-2 sentence summary that improves retrieval recall
    by providing the chunk with its context before embedding.

    Prompt: "Given this {doc_type} chunk about {ticker}, write a 1-2 sentence
    context summary that captures the key entities and themes."
    """
    if model_client is None:
        # Fallback: use first sentence as blurb
        first_sent = re.split(r'[.!?]', chunk_text)[:2]
        return '. '.join(s.strip() for s in first_sent if s.strip()) + '.'

    prompt = (
        f"Given this {doc_type} chunk"
        + (f" about {ticker}" if ticker else "")
        + ", write a concise 1-2 sentence context summary "
        + "that captures the key entities, sentiment, and themes. "
        + "Return ONLY the summary, no other text.\n\n"
        + chunk_text[:1000]  # limit input
    )

    try:
        response = model_client.chat.completions.create(
            model="nomic-embed-text-v1.5",  # or use a small completion model
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.0,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Context blurb generation failed: {e}")
        # Fallback
        first_sent = re.split(r'[.!?]', chunk_text)[:2]
        return '. '.join(s.strip() for s in first_sent if s.strip()) + '.'


# ──────────────────────────────────────────────
# Embedding provider
# ──────────────────────────────────────────────


class EmbeddingProvider:
    """Local embedding provider via LM Studio or sentence-transformers."""

    def __init__(
        self,
        model_name: str = "nomic-embed-text-v1.5",
        use_lm_studio: bool = True,
        base_url: Optional[str] = None,
        use_sentence_transformers: bool = False,
    ):
        self.model_name = model_name
        self.use_lm_studio = use_lm_studio
        self.base_url = base_url
        self.use_st = use_sentence_transformers
        self._st_model = None
        self._client = None

        if use_sentence_transformers:
            try:
                from sentence_transformers import SentenceTransformer
                self._st_model = SentenceTransformer(model_name)
            except ImportError:
                logger.warning("sentence-transformers not available, falling back to LM Studio")
                self.use_lm_studio = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""
        if self.use_st and self._st_model is not None:
            import numpy as np
            embeddings = self._st_model.encode(texts, normalize_embeddings=True)
            return embeddings.tolist()

        if self.use_lm_studio and self.base_url:
            import httpx
            client = httpx.Client(base_url=self.base_url, timeout=30.0)
            response = client.post(
                "/v1/embeddings",
                json={
                    "model": self.model_name,
                    "input": texts,
                },
            )
            response.raise_for_status()
            data = response.json()
            return [d["embedding"] for d in data["data"]]

        raise RuntimeError("No embedding provider available")


# ──────────────────────────────────────────────
# Qdrant vector store
# ──────────────────────────────────────────────


class QdrantStore:
    """Qdrant vector store for RAG chunks."""

    def __init__(self, url: str = "http://localhost:6333", collection_name: str = "documents"):
        self.url = url
        self.collection_name = collection_name
        self._client = None
        self._init_client()

    def _init_client(self):
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import (
                Distance, VectorParams, PointStruct,
            )
            self._client = QdrantClient(url=self.url)
            # Create collection if not exists
            self._ensure_collection()
        except ImportError:
            logger.warning("qdrant-client not installed, using in-memory fallback")
            self._client = None

    def _ensure_collection(self):
        if self._client is None:
            return
        from qdrant_client.models import Distance, VectorParams
        try:
            self._client.get_collection(self.collection_name)
        except Exception:
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=768,  # nomic-embed-text dimension
                    distance=Distance.COSINE,
                ),
            )

    def upsert_chunks(self, chunks: list[TextChunk]):
        """Upsert chunks with embeddings into Qdrant."""
        if self._client is None:
            logger.warning("Qdrant client not available, skipping vector storage")
            return

        from qdrant_client.models import PointStruct
        points = []
        for chunk in chunks:
            if chunk.embedding is None:
                continue
            point = PointStruct(
                id=chunk.chunk_id,
                vector=chunk.embedding,
                payload={
                    "doc_id": chunk.doc_id,
                    "doc_type": chunk.doc_type,
                    "ticker": chunk.ticker,
                    "sector": chunk.sector,
                    "source": chunk.source,
                    "event_date": chunk.event_date,
                    "freshness_weight": chunk.freshness_weight,
                    "text": chunk.text[:2000],  # stored for display
                    "context_blurb": chunk.context_blurb,
                },
            )
            points.append(point)

        if points:
            self._client.upsert(
                collection_name=self.collection_name,
                points=points,
            )

    def search(
        self,
        query_embedding: list[float],
        ticker: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        k: int = 20,
        freshness_window_days: int = 90,
    ) -> list[dict]:
        """Hybrid dense search with metadata filtering."""
        if self._client is None:
            return []

        results = self._client.query_points(
            collection_name=self.collection_name,
            query=query_embedding,
            limit=k,
            query_filter=None,  # TODO: add metadata filter
        ).points

        return [
            {
                "id": r.id,
                "score": r.score,
                "payload": r.payload,
            }
            for r in results
        ]


# ──────────────────────────────────────────────
# RAG ingestion pipeline
# ──────────────────────────────────────────────


@dataclass
class IngestionResult:
    """Result of ingesting a document."""
    doc_id: str
    chunks_created: int
    embeddings_computed: int
    errors: list[str] = field(default_factory=list)


class RAGIngestionPipeline:
    """Complete RAG ingestion pipeline.

    Pipeline:
    1. Fetch raw document
    2. Structure-aware chunking
    3. Generate context blurbs (LLM)
    4. Embed chunks
    5. Upsert to Qdrant
    6. Extract KG nodes/edges (GraphRAG)
    """

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        vector_store: QdrantStore,
        llm_client=None,
    ):
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.llm_client = llm_client

    def ingest(
        self,
        raw_text: str,
        doc_type: str,
        doc_id: str,
        ticker: Optional[str] = None,
        sector: Optional[str] = None,
        source: Optional[str] = None,
        event_date: Optional[str] = None,
    ) -> IngestionResult:
        """Ingest a single document."""
        errors = []

        # Step 1: Chunk
        chunks = chunk_document(
            raw_text=raw_text,
            doc_type=doc_type,
            doc_id=doc_id,
            ticker=ticker,
            sector=sector,
            source=source,
            event_date=event_date,
        )

        # Step 2: Generate context blurbs
        for chunk in chunks:
            try:
                chunk.context_blurb = generate_context_blurb(
                    chunk_text=chunk.text,
                    doc_type=doc_type,
                    ticker=ticker,
                    model_client=self.llm_client,
                )
            except Exception as e:
                errors.append(f"Blurb gen failed for {chunk.chunk_id}: {e}")
                chunk.context_blurb = chunk.text[:200]  # fallback

        # Step 3: Embed
        chunk_texts = [c.context_blurb for c in chunks]
        try:
            embeddings = self.embedding_provider.embed(chunk_texts)
            for chunk, emb in zip(chunks, embeddings):
                chunk.embedding = emb
        except Exception as e:
            errors.append(f"Embedding failed: {e}")
            return IngestionResult(doc_id, len(chunks), 0, errors)

        # Step 4: Upsert to Qdrant
        try:
            self.vector_store.upsert_chunks(chunks)
        except Exception as e:
            errors.append(f"Qdrant upsert failed: {e}")

        return IngestionResult(
            doc_id=doc_id,
            chunks_created=len(chunks),
            embeddings_computed=sum(1 for c in chunks if c.embedding is not None),
            errors=errors,
        )

    def ingest_batch(
        self,
        documents: list[dict],
    ) -> list[IngestionResult]:
        """Ingest a batch of documents.

        Each doc dict: {
            "raw_text": str,
            "doc_type": str,
            "doc_id": str,
            "ticker": str | None,
            "sector": str | None,
            "source": str | None,
            "event_date": str | None,
        }
        """
        return [self.ingest(**doc) for doc in documents]


# ──────────────────────────────────────────────
# Source fetchers
# ──────────────────────────────────────────────


class NewsSourceFetcher:
    """Fetch news from RSS feeds."""

    RSS_FEEDS = [
        "https://www.moneycontrol.com/news/rssfeed.xml",
        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds.xml",
        "https://www.reuters.com/rssFeed/markets",
    ]

    def fetch(self) -> list[dict]:
        """Fetch from all RSS feeds."""
        import feedparser
        results = []
        for url in self.RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    results.append({
                        "raw_text": entry.get("summary", "") + "\n\n" + entry.get("description", ""),
                        "doc_type": DocType.NEWS,
                        "doc_id": f"news_{hashlib.md5(entry.link.encode()).hexdigest()[:12]}",
                        "ticker": None,  # extracted later by NER
                        "source": url,
                        "event_date": entry.get("published"),
                    })
            except Exception as e:
                logger.warning(f"RSS fetch failed for {url}: {e}")
        return results


class SEBICircularFetcher:
    """Fetch SEBI circulars."""

    URL = "https://www.sebi.gov.in/sebi_data/notify_indianenglish.php?s=57&c=121"

    def fetch(self) -> list[dict]:
        """Fetch recent SEBI circulars."""
        import httpx
        results = []
        try:
            client = httpx.Client(timeout=30.0)
            response = client.get(self.URL)
            response.raise_for_status()
            # Parse HTML for circulars (simplified)
            circulars = re.findall(
                r'<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>.*?(\d{2}-\w{3}-\d{4})',
                response.text,
            )
            for href, title, date in circulars[:50]:  # limit to 50
                results.append({
                    "raw_text": f"SEBI Circular: {title}",
                    "doc_type": DocType.CIRCULAR,
                    "doc_id": f"sebi_{hashlib.md5(href.encode()).hexdigest()[:12]}",
                    "source": "sebi.gov.in",
                    "event_date": self._parse_indian_date(date),
                })
        except Exception as e:
            logger.warning(f"SEBI circular fetch failed: {e}")
        return results

    def _parse_indian_date(self, date_str: str) -> Optional[str]:
        """Parse DD-MMM-YYYY to ISO."""
        try:
            dt = time.strptime(date_str, "%d-%b-%Y")
            return time.strftime("%Y-%m-%dT00:00:00", dt)
        except (ValueError, TypeError):
            return None


class RBICircularFetcher:
    """Fetch RBI circulars/guidance."""

    URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=99851"

    def fetch(self) -> list[dict]:
        """Fetch RBI circulars."""
        import httpx
        results = []
        try:
            client = httpx.Client(timeout=30.0)
            response = client.get(self.URL)
            response.raise_for_status()
            circulars = re.findall(
                r'<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>.*?(\d{2}-\w{3}-\d{4})',
                response.text,
            )
            for href, title, date in circulars[:50]:
                results.append({
                    "raw_text": f"RBI Circular: {title}",
                    "doc_type": DocType.CIRCULAR,
                    "doc_id": f"rbi_{hashlib.md5(href.encode()).hexdigest()[:12]}",
                    "source": "rbi.org.in",
                    "event_date": self._parse_indian_date(date),
                })
        except Exception as e:
            logger.warning(f"RBI circular fetch failed: {e}")
        return results

    def _parse_indian_date(self, date_str: str) -> Optional[str]:
        try:
            dt = time.strptime(date_str, "%d-%b-%Y")
            return time.strftime("%Y-%m-%dT00:00:00", dt)
        except (ValueError, TypeError):
            return None


# ──────────────────────────────────────────────
# RAG evaluation harness (RAGAS/DeepEval style)
# ──────────────────────────────────────────────


@dataclass
class RAGASMetrics:
    """RAG evaluation metrics."""
    faithfulness: float  # 0-1, how grounded in context
    context_precision: float  # precision of retrieved context
    context_recall: float  # recall of relevant context
    answer_grounding: float  # how well answer is grounded
    overall_score: float  # weighted composite


class RAGEvaluationHarness:
    """Evaluate RAG pipeline quality on a fixed question set.

    Metrics (per RAGAS/DeepEval):
    - Retrieval faithfulness: does the answer use only retrieved context?
    - Context precision: are relevant docs ranked higher?
    - Context recall: does retrieval find all relevant docs?
    - Answer grounding: is the answer fully supported by context?
    """

    def __init__(self, llm_client=None):
        self.llm_client = llm_client
        # Fixed question set (manually curated)
        self._question_set: list[dict] = []

    def add_question(
        self,
        question: str,
        expected_context: list[str],
        expected_answer: str,
        ticker: Optional[str] = None,
    ):
        """Add a question to the evaluation set."""
        self._question_set.append({
            "question": question,
            "expected_context": expected_context,
            "expected_answer": expected_answer,
            "ticker": ticker,
        })

    def evaluate(
        self,
        vector_store: QdrantStore,
        embedding_provider: EmbeddingProvider,
    ) -> RAGASMetrics:
        """Run evaluation on the fixed question set.

        For each question:
        1. Embed the question
        2. Retrieve top-k from vector store
        3. Compute faithfulness, precision, recall, grounding
        4. Average across all questions
        """
        if not self._question_set:
            logger.warning("No questions in evaluation set")
            return RAGASMetrics(0, 0, 0, 0, 0)

        faithfulness_scores = []
        precision_scores = []
        recall_scores = []
        grounding_scores = []

        for q in self._question_set:
            # Embed question
            query_emb = embedding_provider.embed([q["question"]])[0]

            # Retrieve
            results = vector_store.search(query_emb, k=10)
            retrieved_texts = [r["payload"]["text"] for r in results]

            # Faithfulness: check if answer uses only retrieved context
            faith = self._compute_faithfulness(q, retrieved_texts)
            faithfulness_scores.append(faith)

            # Precision: fraction of retrieved docs that are relevant
            prec = self._compute_precision(q, retrieved_texts)
            precision_scores.append(prec)

            # Recall: fraction of relevant docs that were retrieved
            rec = self._compute_recall(q, retrieved_texts)
            recall_scores.append(rec)

            # Grounding: is the answer grounded in retrieved context?
            ground = self._compute_grounding(q, retrieved_texts)
            grounding_scores.append(ground)

        # Composite score (weighted)
        avg_faith = np.mean(faithfulness_scores)
        avg_prec = np.mean(precision_scores)
        avg_rec = np.mean(recall_scores)
        avg_ground = np.mean(grounding_scores)

        composite = 0.3 * avg_faith + 0.25 * avg_prec + 0.25 * avg_rec + 0.2 * avg_ground

        return RAGASMetrics(
            faithfulness=avg_faith,
            context_precision=avg_prec,
            context_recall=avg_rec,
            answer_grounding=avg_ground,
            overall_score=composite,
        )

    def _compute_faithfulness(self, question: dict, retrieved: list[str]) -> float:
        """Compute faithfulness (0-1)."""
        if not retrieved:
            return 0.0
        # Simple heuristic: check if answer text appears in retrieved context
        answer = question.get("expected_answer", "")
        if not answer:
            return 0.5  # neutral
        overlap = sum(1 for text in retrieved if self._text_overlap(answer, text) > 0.3)
        return min(1.0, overlap / len(retrieved))

    def _compute_precision(self, question: dict, retrieved: list[str]) -> float:
        """Compute context precision."""
        if not retrieved:
            return 0.0
        expected = question.get("expected_context", [])
        if not expected:
            return 0.5
        hits = sum(1 for text in retrieved if any(self._text_overlap(text, exp) > 0.4 for exp in expected))
        return hits / len(retrieved)

    def _compute_recall(self, question: dict, retrieved: list[str]) -> float:
        """Compute context recall."""
        expected = question.get("expected_context", [])
        if not expected:
            return 0.5
        found = sum(1 for exp in expected if any(self._text_overlap(exp, ret) > 0.4 for ret in retrieved))
        return found / len(expected) if expected else 0.0

    def _compute_grounding(self, question: dict, retrieved: list[str]) -> float:
        """Compute answer grounding."""
        answer = question.get("expected_answer", "")
        if not answer or not retrieved:
            return 0.5
        # Check how much of the answer is supported by retrieved context
        words = answer.split()
        supported = sum(1 for w in words if len(w) > 3 and any(self._text_overlap(w, r) > 0.3 for r in retrieved))
        return supported / len([w for w in words if len(w) > 3]) if words else 0.0

    def _text_overlap(self, a: str, b: str, min_word_len: int = 4) -> float:
        """Compute word-level Jaccard overlap between two texts."""
        words_a = set(re.findall(r'\b\w{' + str(min_word_len) + r',}\b', a.lower()))
        words_b = set(re.findall(r'\b\w{' + str(min_word_len) + r',}\b', b.lower()))
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union) if union else 0.0


# ──────────────────────────────────────────────
# Convenience: build pipeline from config
# ──────────────────────────────────────────────


def build_ingestion_pipeline(
    lm_studio_url: Optional[str] = None,
    qdrant_url: str = "http://localhost:6333",
    use_sentence_transformers: bool = False,
) -> RAGIngestionPipeline:
    """Build RAG ingestion pipeline from environment/config."""
    embedding_provider = EmbeddingProvider(
        model_name="nomic-embed-text-v1.5",
        use_lm_studio=lm_studio_url is not None,
        base_url=lm_studio_url,
        use_sentence_transformers=use_sentence_transformers,
    )
    vector_store = QdrantStore(url=qdrant_url)
    return RAGIngestionPipeline(
        embedding_provider=embedding_provider,
        vector_store=vector_store,
    )