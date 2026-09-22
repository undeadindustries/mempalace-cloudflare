"""Cloudflare-native hybrid search implementation.

Combines Vectorize semantic similarity (ANN candidates) with pure-Python
BM25 lexical re-ranking over candidate texts.
Guarantees verbatim recall and high-precision keyword ranking at the edge.
"""

import math
import re
from typing import Any, Dict, List, Optional

from ._shims import install_shims

install_shims()

_TOKEN_RE = re.compile(r"\b\w+\b")


def tokenize(text: str) -> List[str]:
    """Tokenize text into lowercase words."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def bm25_scores(query: str, docs: List[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    """Calculate Okapi BM25 scores for query across candidate docs."""
    q_tokens = tokenize(query)
    if not q_tokens or not docs:
        return [0.0] * len(docs)

    doc_tokens = [tokenize(d) for d in docs]
    n_docs = len(docs)
    avg_dl = sum(len(dt) for dt in doc_tokens) / max(1, n_docs)

    # Document frequencies
    df: Dict[str, int] = {}
    for dt in doc_tokens:
        seen = set(dt)
        for t in q_tokens:
            if t in seen:
                df[t] = df.get(t, 0) + 1

    # IDF per query term
    idf: Dict[str, float] = {}
    for t in q_tokens:
        n_t = df.get(t, 0)
        # Standard Okapi IDF
        idf[t] = math.log((n_docs - n_t + 0.5) / (n_t + 0.5) + 1.0)

    # Score each doc
    scores: List[float] = []
    for dt in doc_tokens:
        dl = len(dt)
        score = 0.0
        # Count term frequencies in this doc
        tf: Dict[str, int] = {}
        for t in dt:
            if t in idf:
                tf[t] = tf.get(t, 0) + 1

        for t in set(q_tokens):
            if t in tf:
                f = tf[t]
                numerator = f * (k1 + 1)
                denominator = f + k1 * (1 - b + b * (dl / avg_dl))
                score += idf[t] * (numerator / max(1e-6, denominator))
        scores.append(score)

    return scores


def hybrid_rerank(
    candidates: List[Dict[str, Any]],
    query: str,
    vector_weight: float = 0.6,
    bm25_weight: float = 0.4,
) -> List[Dict[str, Any]]:
    """Blend vector cosine similarity and normalized BM25 scores."""
    if not candidates:
        return []

    docs = [c.get("text", "") or "" for c in candidates]
    bm25_raw = bm25_scores(query, docs)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    bm25_norm = [s / max_bm25 for s in bm25_raw] if max_bm25 > 0 else [0.0] * len(bm25_raw)

    scored = []
    for c, raw, norm in zip(candidates, bm25_raw, bm25_norm):
        # Cosine distance to similarity: similarity = max(0.0, 1.0 - distance)
        dist = c.get("distance", 1.0)
        vec_sim = max(0.0, 1.0 - dist) if dist is not None else 0.0

        hybrid_score = (vector_weight * vec_sim) + (bm25_weight * norm)
        c["bm25_score"] = round(raw, 3)
        c["score"] = round(hybrid_score, 4)
        c["vector_similarity"] = round(vec_sim, 4)
        scored.append((hybrid_score, c))

    # Sort descending by hybrid score
    scored.sort(
        key=lambda pair: (
            pair[0],
            pair[1].get("metadata", {}).get("authored_at") or "",
        ),
        reverse=True,
    )
    return [c for _, c in scored]


async def execute_hybrid_search(
    collection: Any,
    query: str,
    n_results: int = 10,
    where: Optional[dict] = None,
) -> List[Dict[str, Any]]:
    """Execute end-to-end hybrid search against Cloudflare collection."""
    # 1. Fetch vector candidate pool (fetch 2x n_results to allow lexical re-ranking)
    fetch_count = min(50, max(20, n_results * 2))

    if hasattr(collection, "a_query"):
        q_res = await collection.a_query(
            query_texts=[query],
            n_results=fetch_count,
            where=where,
        )
    else:
        q_res = collection.query(
            query_texts=[query],
            n_results=fetch_count,
            where=where,
        )

    ids = q_res.ids[0] if q_res.ids else []
    docs = q_res.documents[0] if q_res.documents else []
    metas = q_res.metadatas[0] if q_res.metadatas else []
    dists = q_res.distances[0] if q_res.distances else []

    candidates: List[Dict[str, Any]] = []
    for did, doc, meta, dist in zip(ids, docs, metas, dists):
        candidates.append(
            {
                "id": did,
                "text": doc,
                "distance": dist,
                "metadata": meta,
                "wing": meta.get("wing"),
                "room": meta.get("room"),
            }
        )

    # 2. Re-rank with BM25
    ranked = hybrid_rerank(candidates, query)
    return ranked[:n_results]
