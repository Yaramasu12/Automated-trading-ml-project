"""
trading_platform/ai/rag/graph_rag.py — GraphRAG knowledge graph

Per §8 (REDESIGN_PROMPT): Entity-reasoning over tickers, sectors, events,
suppliers, peers for contagion and lead-lag queries that flat vector search misses.
Uses networkx (free, OSS) as the graph engine.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import networkx (lightweight dependency)
try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False
    nx = None  # type: ignore


# ──────────────────────────────────────────────
# Knowledge graph data model
# ──────────────────────────────────────────────


@dataclass
class KGNode:
    """A node in the knowledge graph."""
    id: str
    label: str  # "ticker", "sector", "event", "company", "person"
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class KGEdge:
    """An edge in the knowledge graph."""
    source: str
    target: str
    label: str  # "owns", "supplies", "peer_of", "exposed_to", "leads"
    weight: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────
# Knowledge graph operations
# ──────────────────────────────────────────────


class KnowledgeGraph:
    """Lightweight knowledge graph for GraphRAG reasoning."""

    def __init__(self) -> None:
        self._nodes: dict[str, KGNode] = {}
        self._edges: list[KGEdge] = []
        self._graph: Any = None  # networkx graph (lazy init)
        self._dirty = True

    def add_node(self, node: KGNode) -> None:
        """Add a node to the knowledge graph."""
        self._nodes[node.id] = node
        self._dirty = True

    def add_edge(self, edge: KGEdge) -> None:
        """Add an edge to the knowledge graph."""
        self._edges.append(edge)
        self._dirty = True

    def _build_networkx(self) -> Any:
        """Build/refresh the networkx graph (lazy)."""
        if not NETWORKX_AVAILABLE:
            raise RuntimeError("networkx is required for GraphRAG (pip install networkx)")

        if self._graph is not None and not self._dirty:
            return self._graph

        G = nx.DiGraph()  # type: ignore

        for node in self._nodes.values():
            G.add_node(node.id, label=node.label, **node.properties)

        for edge in self._edges:
            G.add_edge(edge.source, edge.target, label=edge.label, weight=edge.weight)

        self._graph = G
        self._dirty = False
        return G

    def find_contagion_paths(self, ticker: str, depth: int = 2) -> list[dict[str, Any]]:
        """Find contagion paths from a ticker (e.g., NIFTY → sector → peers)."""
        G = self._build_networkx()
        if G is None:
            return []

        paths = []

        # Outward: who is affected by this ticker?
        outgoing = []
        for pred in nx.predecessors(G, ticker):  # type: ignore
            edge = G[pred][ticker]
            outgoing.append({
                "node": pred,
                "edge_label": edge.get("label", ""),
                "depth": 1,
            })
            # Depth 2
            for pred2 in nx.predecessors(G, pred):
                edge2 = G[pred2][pred]
                outgoing.append({
                    "node": pred2,
                    "edge_label": f"{edge2.get('label', '')}→{edge.get('label', '')}",
                    "depth": 2,
                })

        # Inward: what affects this ticker?
        incoming = []
        for succ in nx.successors(G, ticker):  # type: ignore
            edge = G[ticker][succ]
            incoming.append({
                "node": succ,
                "edge_label": edge.get("label", ""),
                "depth": 1,
            })

        paths.extend(outgoing)
        paths.extend(incoming)
        return paths

    def find_peer_exposure(self, ticker: str) -> list[dict[str, Any]]:
        """Find peer exposure for a ticker (who else is exposed to same risk)."""
        G = self._build_networkx()
        if G is None:
            return []

        peers = []
        # Find nodes that share incoming edges with ticker
        for pred in nx.predecessors(G, ticker):
            for succ in nx.successors(G, pred):
                if succ != ticker and succ not in [p["node"] for p in peers]:
                    peers.append({
                        "peer": succ,
                        "via_node": pred,
                        "edge_label": G[pred][succ].get("label", ""),
                    })
        return peers

    def query(self, entity_id: str) -> dict[str, Any]:
        """Get all context for an entity."""
        if entity_id not in self._nodes:
            return {"found": False, "entity": entity_id}

        node = self._nodes[entity_id]
        incoming_edges = [e for e in self._edges if e.target == entity_id]
        outgoing_edges = [e for e in self._edges if e.source == entity_id]

        return {
            "found": True,
            "entity": entity_id,
            "label": node.label,
            "properties": node.properties,
            "incoming": [{"source": e.source, "label": e.label} for e in incoming_edges],
            "outgoing": [{"target": e.target, "label": e.label} for e in outgoing_edges],
        }

    def build_from_news(self, news_items: list[dict[str, Any]]) -> int:
        """Build/update knowledge graph from news items.

        Expected news item format:
        {
            "tickers": ["NIFTY", "BANKNIFTY"],
            "events": ["budget", "rbi_policy"],
            "sectors": ["banking", "it"],
            "entities": ["modi", "raghuran_rajan"],
            "relations": [{"from": "NIFTY", "to": "banking", "label": "in_sector"}],
        }
        """
        count = 0
        for item in news_items:
            # Add tickers as nodes
            for ticker in item.get("tickers", []):
                if ticker not in self._nodes:
                    self.add_node(KGNode(id=ticker, label="ticker", properties={"source": "news"}))
                    count += 1

            # Add sectors
            for sector in item.get("sectors", []):
                if sector not in self._nodes:
                    self.add_node(KGNode(id=sector, label="sector", properties={"source": "news"}))
                    count += 1

            # Add events
            for event in item.get("events", []):
                if event not in self._nodes:
                    self.add_node(KGNode(id=event, label="event", properties={"source": "news"}))
                    count += 1

            # Add edges
            for rel in item.get("relations", []):
                src, tgt = rel.get("from", ""), rel.get("to", "")
                if src and tgt and src != tgt:
                    # Check if edge exists
                    existing = any(
                        e.source == src and e.target == tgt
                        for e in self._edges
                    )
                    if not existing:
                        self.add_edge(KGEdge(
                            source=src, target=tgt,
                            label=rel.get("label", "related"),
                        ))
                        count += 1

        return count