"""Cloudflare-native hybrid search implementation.

Combines Vectorize semantic similarity (ANN candidates) with pure-Python
BM25 lexical re-ranking over candidate texts.
Guarantees verbatim recall and high-precision keyword ranking at the edge.
"""

import asyncio
import math
import re
from typing import Any, Dict, List, Optional

try:
    from ._shims import install_shims
except (ImportError, ValueError):
    from _shims import install_shims  # type: ignore

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
        dist = c.get("distance")
        if dist is not None:
            # Cosine distance to similarity: similarity = max(0.0, 1.0 - distance)
            vec_sim = max(0.0, 1.0 - dist)
            hybrid_score = (vector_weight * vec_sim) + (bm25_weight * norm)
            c["vector_similarity"] = round(vec_sim, 4)
        else:
            # Pure lexical match (from D1 FTS5): strong keyword signal.
            # Impute similarity from lexical alignment so exact matches outrank
            # semantic near-misses that contain none of the query terms.
            vec_sim = norm * 0.7
            hybrid_score = (vector_weight * vec_sim) + (bm25_weight * norm)
            c["vector_similarity"] = None

        c["bm25_score"] = round(raw, 3)
        c["score"] = round(hybrid_score, 4)
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


def _parse_wing_room_filter(where: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    """Extract wing and room strings from where filter."""
    wing: Optional[str] = None
    room: Optional[str] = None
    if isinstance(where, dict):
        w = where.get("wing")
        if isinstance(w, str):
            wing = w
        elif isinstance(w, dict) and "$eq" in w:
            wing = str(w["$eq"])
        r = where.get("room")
        if isinstance(r, str):
            room = r
        elif isinstance(r, dict) and "$eq" in r:
            room = str(r["$eq"])
    return wing, room


def _collect_vector_candidates(q_res: Any) -> List[Dict[str, Any]]:
    """Transform Vectorize query result into candidate list."""
    if q_res is None:
        return []
    ids = q_res.ids[0] if getattr(q_res, "ids", None) else []
    docs = q_res.documents[0] if getattr(q_res, "documents", None) else []
    metas = q_res.metadatas[0] if getattr(q_res, "metadatas", None) else []
    dists = q_res.distances[0] if getattr(q_res, "distances", None) else []

    candidates = []
    seen = set()
    for did, doc, meta, dist in zip(ids, docs, metas, dists):
        if did in seen:
            continue
        seen.add(did)
        m = meta or {}
        candidates.append(
            {
                "id": did,
                "text": doc,
                "distance": dist,
                "metadata": m,
                "wing": m.get("wing"),
                "room": m.get("room"),
                "matched_via": "vector",
            }
        )
    return candidates


async def _collect_fts_candidates(
    fts_hits: list,
    seen_ids: set,
    d1_reg: Any,
) -> List[Dict[str, Any]]:
    """Transform FTS query result into candidate list, hydrating metadata if needed."""
    fts_only = [h for h in (fts_hits or []) if h.get("id") and h["id"] not in seen_ids]
    if not fts_only:
        return []

    meta_map: Dict[str, Any] = {}
    if d1_reg and hasattr(d1_reg, "get_drawers"):
        try:
            d1_rows = d1_reg.get_drawers([h["id"] for h in fts_only])
            if hasattr(d1_rows, "__await__"):
                d1_rows = await d1_rows
            meta_map = {r["id"]: r.get("metadata", {}) for r in d1_rows}
        except Exception:
            meta_map = {}

    candidates = []
    for h in fts_only:
        did = h.get("id")
        if not did or did in seen_ids:
            continue
        seen_ids.add(did)
        meta = dict(meta_map.get(did, {}))
        if "wing" not in meta and h.get("wing"):
            meta["wing"] = h["wing"]
        if "room" not in meta and h.get("room"):
            meta["room"] = h["room"]
        candidates.append(
            {
                "id": did,
                "text": h.get("content", ""),
                "distance": None,
                "metadata": meta,
                "wing": h.get("wing") or meta.get("wing"),
                "room": h.get("room") or meta.get("room"),
                "matched_via": "fts",
            }
        )
    return candidates


async def execute_hybrid_search(
    collection: Any,
    query: str,
    n_results: int = 10,
    where: Optional[dict] = None,
) -> List[Dict[str, Any]]:
    """Execute end-to-end hybrid search against Cloudflare collection.

    Unions Vectorize ANN candidates with D1 FTS5 trigram lexical candidates,
    then re-ranks the combined pool with Okapi BM25 and cosine similarity.
    """
    # 1. Candidate pool size (fetch 2x n_results to allow lexical re-ranking, max 50)
    fetch_count = min(50, max(20, n_results * 2))
    fts_wing, fts_room = _parse_wing_room_filter(where)
    d1_reg = getattr(collection, "d1", None)

    async def _fetch_vector():
        try:
            if hasattr(collection, "a_query"):
                return await collection.a_query(
                    query_texts=[query],
                    n_results=fetch_count,
                    where=where,
                )
            elif hasattr(collection, "query"):
                res = collection.query(
                    query_texts=[query],
                    n_results=fetch_count,
                    where=where,
                )
                if hasattr(res, "__await__"):
                    return await res
                return res
        except Exception:
            return None
        return None

    async def _fetch_fts():
        if d1_reg and hasattr(d1_reg, "search_fts"):
            try:
                res = d1_reg.search_fts(
                    query=query,
                    wing=fts_wing,
                    room=fts_room,
                    limit=fetch_count,
                )
                if hasattr(res, "__await__"):
                    return await res
                return res
            except Exception:
                return []
        return []

    q_res, fts_hits = await asyncio.gather(_fetch_vector(), _fetch_fts())

    candidates = _collect_vector_candidates(q_res)
    seen_ids = {c["id"] for c in candidates}
    fts_candidates = await _collect_fts_candidates(fts_hits, seen_ids, d1_reg)
    candidates.extend(fts_candidates)

    # 2. Re-rank unioned pool with BM25
    ranked = hybrid_rerank(candidates, query)
    return ranked[:n_results]
