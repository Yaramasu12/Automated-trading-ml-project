from __future__ import annotations

"""VectorMemory and RAG retriever for local agent grounding.

Uses keyword-overlap (TF-IDF-flavoured Jaccard) similarity so there are
zero external ML dependencies.  In production this layer can be swapped
for a FAISS / ChromaDB backend while keeping the same RAGRetriever API.

Architecture
------------
VectorDocument
    ↓ (stored in)
VectorMemoryStore   ←── add() / search()
    ↓ (wrapped by)
RAGRetriever        ←── retrieve(query, top_k, category_filter)
    ↓ (used by)
LocalModelGateway   ←── injects evidence_ids into every model call

Seed documents
--------------
Preloaded categories:
  strategy   - trading strategy descriptions and invalidation rules
  model      - model cards for neural forecaster, GARCH, regime classifier
  risk       - position limit rules, drawdown halt rules
  compliance - SEBI regulations, order size limits, time-in-force rules
  postmortem - example failure mode descriptions
  market     - Indian market microstructure facts
"""

import logging
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trading_platform.agents.schemas import EvidenceRef

logger = logging.getLogger(__name__)


# ── Document ──────────────────────────────────────────────────────────────────

@dataclass
class VectorDocument:
    """A text document stored in VectorMemoryStore."""
    doc_id: str
    content: str
    category: str   # strategy | model | risk | compliance | postmortem | market | news
    tags: list[str] = field(default_factory=list)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "category": self.category,
            "tags": self.tags,
            "content_preview": self.content[:200],
            "ts": self.ts.isoformat(),
        }


# ── Tokeniser ─────────────────────────────────────────────────────────────────

def _tokenise(text: str) -> set[str]:
    """Lower-case word tokeniser; strips punctuation and short stop words."""
    _STOP = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "to", "of", "and", "or", "in", "on", "at", "by", "for", "with",
        "this", "that", "it", "its", "as", "from", "but", "not", "so", "if",
        "then", "when", "which", "who", "how", "all", "each", "any",
    }
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) > 2 and t not in _STOP}


def _jaccard_score(a: set[str], b: set[str]) -> float:
    """Weighted Jaccard: intersection / union, 0.0 if either empty."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union > 0 else 0.0


# ── Store ─────────────────────────────────────────────────────────────────────

class VectorMemoryStore:
    """In-process document store with keyword-similarity retrieval.

    All methods are thread-safe.  For large corpora (> 50 k documents)
    replace `search` with a proper ANN index without changing the interface.
    """

    def __init__(self) -> None:
        self._docs: dict[str, VectorDocument] = {}
        self._tokens: dict[str, set[str]] = {}   # doc_id → token set
        self._lock = threading.RLock()

    # ── Mutators ──────────────────────────────────────────────────────────────

    def add(self, doc: VectorDocument) -> None:
        with self._lock:
            self._docs[doc.doc_id] = doc
            self._tokens[doc.doc_id] = _tokenise(doc.content + " " + " ".join(doc.tags))

    def remove(self, doc_id: str) -> None:
        with self._lock:
            self._docs.pop(doc_id, None)
            self._tokens.pop(doc_id, None)

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        category_filter: str | None = None,
    ) -> list[tuple[VectorDocument, float]]:
        """Return up to top_k (document, score) pairs ordered by descending similarity."""
        query_tokens = _tokenise(query)
        if not query_tokens:
            return []

        results: list[tuple[VectorDocument, float]] = []
        with self._lock:
            for doc_id, doc in self._docs.items():
                if category_filter and doc.category != category_filter:
                    continue
                score = _jaccard_score(query_tokens, self._tokens.get(doc_id, set()))
                if score > 0.0:
                    results.append((doc, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def get(self, doc_id: str) -> VectorDocument | None:
        with self._lock:
            return self._docs.get(doc_id)

    def count(self, category: str | None = None) -> int:
        with self._lock:
            if category:
                return sum(1 for d in self._docs.values() if d.category == category)
            return len(self._docs)

    def all_categories(self) -> list[str]:
        with self._lock:
            return list({d.category for d in self._docs.values()})

    # ── Seed data ─────────────────────────────────────────────────────────────

    def seed_defaults(self) -> None:
        """Preload the canonical knowledge base used by agent grounding."""
        docs = _build_seed_documents()
        for doc in docs:
            self.add(doc)
        logger.info("VectorMemoryStore: seeded %d documents across %d categories",
                    len(docs), len({d.category for d in docs}))


# ── RAG Retriever ─────────────────────────────────────────────────────────────

class RAGRetriever:
    """Wraps VectorMemoryStore and returns typed EvidenceRef lists.

    Injected into LocalModelGateway so every model call is grounded in
    retrieved evidence.  Evidence ids are appended to the model's context
    and echoed back in the response so the trace is auditable.
    """

    def __init__(
        self,
        store: VectorMemoryStore,
        default_top_k: int = 4,
        min_score: float = 0.05,
    ) -> None:
        self._store = store
        self._top_k = default_top_k
        self._min_score = min_score

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        category_filter: str | None = None,
    ) -> list[EvidenceRef]:
        """Return top-k EvidenceRef objects relevant to the query."""
        k = top_k or self._top_k
        results = self._store.search(query, top_k=k, category_filter=category_filter)
        refs: list[EvidenceRef] = []
        for doc, score in results:
            if score < self._min_score:
                continue
            refs.append(EvidenceRef(
                doc_id=doc.doc_id,
                source=doc.category,
                relevance=round(score, 4),
                excerpt=doc.content[:120],
            ))
        return refs

    def retrieve_ids(self, query: str, top_k: int | None = None) -> list[str]:
        return [ref.doc_id for ref in self.retrieve(query, top_k=top_k)]

    def build_context_snippet(self, query: str, top_k: int | None = None) -> str:
        """Return a compact text block for injection into model prompts."""
        refs = self.retrieve(query, top_k=top_k)
        if not refs:
            return ""
        lines = ["[Evidence]"]
        for ref in refs:
            lines.append(f"  [{ref.doc_id}] ({ref.source}) {ref.excerpt}")
        return "\n".join(lines)

    @property
    def store(self) -> VectorMemoryStore:
        return self._store


# ── Seed document corpus ──────────────────────────────────────────────────────

def _build_seed_documents() -> list[VectorDocument]:
    """Return the default knowledge base used by the agent council."""

    docs: list[VectorDocument] = []

    def _add(doc_id: str, category: str, tags: list[str], content: str) -> None:
        docs.append(VectorDocument(doc_id=doc_id, content=content, category=category, tags=tags))

    # ── Strategy docs ─────────────────────────────────────────────────────────

    _add("strat-trend-001", "strategy",
         ["trend", "momentum", "ema", "crossover", "nifty", "banknifty"],
         "Trend-momentum strategy: enter long when 20-EMA crosses above 50-EMA on 15-minute bars "
         "with RSI > 55 and volume above 20-day average. Invalidation: price closes below 50-EMA. "
         "Target: 1.5x ATR. Stop: 0.75x ATR below entry. Best in trending regimes; avoid flat/choppy markets. "
         "NSE equities and index futures. Position size: max 5 % of capital.")

    _add("strat-revert-001", "strategy",
         ["mean", "reversion", "bollinger", "zscore", "overextended"],
         "Mean-reversion strategy: enter short when price is more than 2 standard deviations above "
         "20-bar Bollinger Band with declining volume. Invalidation: new momentum burst above 2.5 SD. "
         "Target: band midline. Stop: 3 SD close. Regime: low-volatility range-bound markets only. "
         "Avoid during earnings, RBI policy, and index rebalancing windows.")

    _add("strat-breakout-001", "strategy",
         ["breakout", "consolidation", "range", "volume", "open-interest"],
         "Breakout strategy: enter when price exceeds a 20-bar high on the opening 30 minutes "
         "with volume > 150 % of 20-day average and open interest rising. "
         "Invalidation: close back inside the range within 2 bars. "
         "Target: measured move equal to range width. Stop: midpoint of range. "
         "Higher success rate on index futures than equities due to liquidity.")

    _add("strat-gap-001", "strategy",
         ["gap", "opening", "event", "earnings", "corporate-action"],
         "Gap/event strategy: trade the morning gap fill when the gap is < 1.5 % and no significant "
         "news catalyst. Enter in the direction of the fill within the first 15 minutes. "
         "Invalidation: gap holds and price continues in the gap direction. "
         "Avoid: earnings surprises, dividend ex-dates, index additions.")

    _add("strat-pairs-001", "strategy",
         ["pairs", "stat-arb", "cointegration", "spread", "nse", "sector"],
         "Pairs/stat-arb strategy: trade the spread between two cointegrated equities in the same sector. "
         "Enter when z-score of spread > 2. Exit at mean-reversion z-score < 0.5. "
         "Stop: z-score exceeds 3.5 (regime break). "
         "Recalibrate cointegration parameters monthly. Monitor sector-specific catalysts.")

    _add("strat-options-vol-001", "strategy",
         ["options", "volatility", "iv", "vix", "straddle", "strangle", "delta"],
         "Options volatility strategy: sell straddles on Nifty weekly expiry when IV Rank > 70 "
         "and realized vol is 20 % lower than implied vol. Delta-neutral at entry. "
         "Manage at 50 % profit or 200 % loss. Roll 2 DTE. "
         "Risk: gap events, RBI announcement, sudden index moves > 2 %.")

    _add("strat-futures-carry-001", "strategy",
         ["futures", "carry", "basis", "rollover", "calendar", "mcx"],
         "Futures carry strategy: trade the basis between spot and near-month futures when "
         "the carry is anomalously high or low relative to the risk-free rate. "
         "Entry when carry deviates > 1.5x from 60-day rolling mean. "
         "Exit at carry normalisation. Applicable to Nifty futures, BankNifty futures, MCX commodities.")

    _add("strat-hedge-001", "strategy",
         ["hedge", "protection", "put", "collar", "portfolio", "drawdown"],
         "Portfolio hedge strategy: buy Nifty put spreads when portfolio delta exceeds 0.60 "
         "and VIX is below 14 (cheap protection). Cost: 0.5 % of notional per month. "
         "Strike: 2-3 % OTM. Expiry: 4–6 weeks. "
         "Purpose: limit drawdown, not profit generation. "
         "Activate automatically when drawdown approaches 7 % of capital.")

    # ── Model cards ───────────────────────────────────────────────────────────

    _add("model-neural-forecast-001", "model",
         ["neural", "forecast", "tft", "patchtst", "return", "direction"],
         "Neural Forecasting Ensemble model card. "
         "Models: Temporal Fusion Transformer, PatchTST, N-BEATS, LSTM baseline. "
         "Inputs: multi-timeframe OHLCV, volume-weighted features, options Greeks summary, macro embeddings. "
         "Outputs: return quantiles (5th/25th/50th/75th/95th), direction probability (0-1), "
         "volatility band (annualised), stop/target hit probability. "
         "Calibration: isotonic regression on validation set. "
         "Advisory only until walk-forward OOS improvement confirmed. "
         "No profit guarantee. Retrain quarterly or on drift alert.")

    _add("model-garch-vol-001", "model",
         ["garch", "volatility", "variance", "forecast", "risk"],
         "GARCH Volatility Forecaster model card. "
         "GARCH(1,1) baseline plus deep volatility network for fat-tail estimation. "
         "Inputs: last 60 daily log-returns. "
         "Outputs: 1-day ahead conditional volatility, 5-day ahead annualised vol band, "
         "probability of > 2 % move tomorrow. "
         "Used for position sizing, hedge sizing, and event-risk gates. "
         "Recalibrated weekly using last 252 trading days.")

    _add("model-regime-001", "model",
         ["regime", "classifier", "trending", "ranging", "volatile", "hidden-markov"],
         "Regime Classifier model card. "
         "Classifies current market into: TRENDING, MEAN_REVERTING, VOLATILE, BREAKOUT, STABLE. "
         "Features: realised volatility, momentum 5/20, volume ratio, trend strength, options skew. "
         "Algorithm: trained on historical NSE data 2015-2024. "
         "Output used to gate strategy selection and weight ensemble decision engine. "
         "Regime changes trigger conservative sizing until confirmation on 3 consecutive bars.")

    _add("model-meta-label-001", "model",
         ["meta-label", "champion", "challenger", "filter", "promotion"],
         "Meta-Labeling Filter model card. "
         "Secondary ML filter trained on OutcomeLabel.meta_label_score targets. "
         "Takes strategy signal + neural forecast + regime + portfolio state as features. "
         "Outputs: trade quality probability in [0,1]. "
         "Threshold: 0.55 for live trading, 0.45 for paper. "
         "Champion/challenger: only promoted after OOS Sharpe > baseline for 3 months. "
         "Advisory — final decision gate is deterministic RiskEngine.")

    _add("model-sentiment-001", "model",
         ["sentiment", "news", "nlp", "embedding", "event"],
         "Sentiment and Event Model card. "
         "Pipeline: RSS news ingestion → keyword extraction → embedding → symbol/sector linker → decay model. "
         "Output: event_risk_score per symbol (0-1), affected_symbols list, volatility_window estimate. "
         "Sentiment decays with half-life of 4 hours for intraday, 2 days for swing. "
         "High event_risk_score (> 0.7) triggers HALT recommendation to agent council.")

    # ── Risk rules ────────────────────────────────────────────────────────────

    _add("risk-position-limits-001", "risk",
         ["position", "limit", "exposure", "concentration", "max"],
         "Position limit rules: "
         "Max 5 % of total capital in any single instrument. "
         "Max 20 % in any single sector. "
         "Max 30 % total notional margin utilisation. "
         "Options: max 10 % capital in net premium. "
         "Futures: max 60 % margin utilisation. "
         "Stop trading if drawdown exceeds 10 % from peak. "
         "Max 200 orders per day (compliance hard limit).")

    _add("risk-drawdown-halt-001", "risk",
         ["drawdown", "halt", "kill-switch", "daily-loss", "circuit-breaker"],
         "Drawdown and halt rules: "
         "Daily loss > 2 %: no new entries, exit open positions at next opportunity. "
         "Peak-to-trough drawdown > 10 %: activate kill switch, escalate to manual review. "
         "Single trade loss > 2 %: block same strategy for the session. "
         "Market circuit breaker (NSE > 10 % move): halt all trading immediately. "
         "VIX spike > 35: reduce position size to 50 %, no new option sells.")

    _add("risk-slippage-impact-001", "risk",
         ["slippage", "market-impact", "bid-ask", "spread", "liquidity"],
         "Slippage and market impact rules: "
         "Do not trade if bid-ask spread > 0.15 % for equities, > 0.10 % for index futures. "
         "Max order size: 1 % of average daily volume. "
         "Use limit orders for entries; market orders only for emergency exits. "
         "Expected slippage: 0.10 % for Nifty futures, 0.20 % for mid-cap equities. "
         "Reject trade if estimated impact cost exceeds 0.30 %.")

    _add("risk-event-blackout-001", "risk",
         ["event", "blackout", "rbi", "budget", "expiry", "elections"],
         "Event blackout windows — no new entries: "
         "RBI Monetary Policy Committee: 2 days before and day of announcement. "
         "Union Budget: 3 days before and 1 day after. "
         "NSE weekly options expiry (Thursday): no new option sells after 2:30 PM IST. "
         "State and general elections: reduce position size 50 % during counting day. "
         "US Fed meeting days: reduce index futures exposure by 30 % after 11:30 PM IST.")

    # ── Compliance notes ──────────────────────────────────────────────────────

    _add("compliance-sebi-001", "compliance",
         ["sebi", "regulation", "algo", "approved", "order", "reporting"],
         "SEBI algorithmic trading regulations: "
         "Algo strategies must be approved by the exchange before live use. "
         "Minimum order-to-trade ratio monitoring required. "
         "Daily P&L reports must be maintained for 5 years. "
         "Client order segregation: system must never mix client and proprietary orders. "
         "Kill switch mandatory for all algo systems. "
         "Algo testing required on mock exchange before production deployment.")

    _add("compliance-order-types-001", "compliance",
         ["order", "type", "limit", "market", "stoploss", "time-in-force", "ioc", "day"],
         "Approved order types on NSE/BSE/MCX: "
         "LIMIT (Day): standard entry orders. "
         "MARKET: emergency exits only. "
         "STOP-LOSS: automated exit plans, always paired with entry. "
         "IOC (Immediate or Cancel): fragmented block trades. "
         "GTC (Good Till Cancelled): not supported on NSE equity intraday. "
         "Angel One API: product types INTRADAY, DELIVERY, CARRYFORWARD for futures/options.")

    # ── Postmortem examples ───────────────────────────────────────────────────

    _add("pm-example-model-miss-001", "postmortem",
         ["failure", "model", "miss", "drift", "prediction", "error"],
         "Postmortem example — Model miss failure mode: "
         "Predicted return +2.5 %, realized return −3.1 %. "
         "Root cause: regime shift from trending to volatile not detected in time. "
         "Features used 20-day momentum which lagged the intraday regime change. "
         "Fix: add intraday regime signal with 5-minute bars, reduce feature lookback in volatile regime. "
         "Lesson: model drift check should run every 100 fills, not just daily.")

    _add("pm-example-tail-risk-001", "postmortem",
         ["failure", "tail", "risk", "gap", "news", "slippage"],
         "Postmortem example — Tail risk realized: "
         "Held position over earnings announcement despite EventRiskGuard advisory. "
         "Gap down 8 % at open, stop loss filled at 6.2 % loss (gap through stop). "
         "Fix: hard-code flat-before-event rule for top-50 NSE equities with earnings. "
         "Lesson: stop-loss orders do not protect against gap risk; reduce size to 25 % before events.")

    _add("pm-example-slippage-001", "postmortem",
         ["failure", "slippage", "execution", "market-impact", "large-order"],
         "Postmortem example — Execution slippage: "
         "Entered 200-lot Nifty futures in a single market order at illiquid time (8:55 AM IST). "
         "Realized slippage: 4 points (0.12 %) vs expected 1 point (0.03 %). "
         "Fix: slice large orders into 50-lot blocks with 2-second gaps. "
         "Lesson: use VWAP slicing for orders > 1 % of average daily volume.")

    # ── Market microstructure ─────────────────────────────────────────────────

    _add("market-nse-sessions-001", "market",
         ["nse", "session", "hours", "pre-market", "closing", "ist"],
         "NSE/BSE trading sessions (IST): "
         "Pre-market order entry: 09:00 – 09:08. "
         "Pre-market order modification: 09:08 – 09:12. "
         "Normal market: 09:15 – 15:30. "
         "Post-market session: 15:40 – 16:00. "
         "NSE F&O expiry: last Thursday of month (index options); weekly: every Thursday. "
         "NSE currency derivatives: 09:00 – 17:00. "
         "MCX commodity: 09:00 – 23:30 (extended for international commodities).")

    _add("market-angel-one-api-001", "market",
         ["angel", "one", "api", "broker", "smartapi", "token", "websocket"],
         "Angel One SmartAPI integration notes: "
         "Authentication: API key + TOTP-based session token, refreshed daily. "
         "Rate limits: 3 orders/second, 200 orders/day for algo accounts. "
         "WebSocket: SmartWebSocketV2 for live tick feed, mode 3 for full quote. "
         "Historical data: candles endpoint, max 365 days per request. "
         "Order types supported: LIMIT, MARKET, STOP_LOSS, STOP_LOSS_MARKET. "
         "Instruments: 70,000+ listed; use instrument master JSON for symbol lookup.")

    _add("market-index-composition-001", "market",
         ["nifty", "banknifty", "sensex", "index", "constituent", "rebalance"],
         "Key Indian index facts: "
         "Nifty 50: 50 large-cap NSE stocks, rebalanced semi-annually. "
         "BankNifty: 12 banking stocks, highly liquid options, weekly expiry. "
         "FinNifty: 20 financial services stocks. "
         "Sensex: 30 BSE stocks, similar to Nifty but BSE listed. "
         "Nifty IT, Pharma, FMCG, Auto, Metal: sectoral indices for pairs trading. "
         "SGX Nifty (GIFT Nifty): pre-market indicator from Singapore exchange.")

    return docs
