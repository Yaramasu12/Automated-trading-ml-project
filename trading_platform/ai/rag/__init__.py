"""
trading_platform/ai/rag/__init__.py — RAG pipeline package

Per §8 (REDESIGN_PROMPT): Real RAG + LLM agents for the platform.

Components:
- router.py: Adaptive RAG router (simple vs multi-hop queries)
- graph_rag.py: GraphRAG knowledge graph for entity-relationship queries
- ingestion.py: Document ingestion pipeline (contextual retrieval, chunking, embedding)
- agents/: LangGraph LLM agents (veto-only, reflection, tool use)
"""

from .router import AdaptiveRouter, AdaptiveRAGPipeline, QueryComplexity
from .graph_rag import KnowledgeGraph, KGNode, KGEdge
from .ingestion import (
    RAGIngestionPipeline,
    EmbeddingProvider,
    QdrantStore,
    NewsSourceFetcher,
    SEBICircularFetcher,
    RBICircularFetcher,
    RAGEvaluationHarness,
    RAGASMetrics,
    build_ingestion_pipeline,
)
from .eval import RAGEvaluator, RAGEvaluationReport

__all__ = [
    "AdaptiveRouter",
    "AdaptiveRAGPipeline",
    "QueryComplexity",
    "KnowledgeGraph",
    "KGNode",
    "KGEdge",
    "RAGIngestionPipeline",
    "EmbeddingProvider",
    "QdrantStore",
    "NewsSourceFetcher",
    "SEBICircularFetcher",
    "RBICircularFetcher",
    "RAGEvaluationHarness",
    "RAGASMetrics",
    "build_ingestion_pipeline",
    "RAGEvaluator",
    "RAGEvaluationReport",
]