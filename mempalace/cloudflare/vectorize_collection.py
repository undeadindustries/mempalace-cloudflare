"""Standalone Cloudflare Vectorize collection adapter for Workers runtime."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Dict, List, Optional

try:
    from .d1_registry import D1DrawerRegistry
    from .r2_storage import R2DrawerStorage
    from .results import GetResult, QueryResult
    from .workers_ai import WorkersAIEmbedder
except (ImportError, ValueError):
    from d1_registry import D1DrawerRegistry  # type: ignore
    from r2_storage import R2DrawerStorage  # type: ignore
    from results import GetResult, QueryResult  # type: ignore
    from workers_ai import WorkersAIEmbedder  # type: ignore


class CloudflareVectorizeCollection:
    """Collection implementation backed by Vectorize, R2, and D1."""

    def __init__(
        self,
        *,
        vector_index: Any,
        ai_embedder: WorkersAIEmbedder,
        r2_storage: R2DrawerStorage,
        d1_registry: D1DrawerRegistry,
        namespace: Optional[str] = None,
    ):
        self.vector_index = vector_index
        self.ai = ai_embedder
        self.r2 = r2_storage
        self.d1 = d1_registry
        self.namespace = namespace

    async def a_upsert(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[dict]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        """Async upsert: embed -> Vectorize + R2 + D1."""
        if len(documents) != len(ids):
            raise ValueError(f"documents ({len(documents)}) and ids ({len(ids)}) count mismatch")

        metas = metadatas if metadatas is not None else [{} for _ in documents]

        # 1. Embed documents if embeddings not supplied
        if embeddings is None:
            vectors = await self.ai.embed(documents)
        else:
            vectors = embeddings

        # 2. Put verbatim drawer text in R2
        r2_tasks = [self.r2.put_drawer(did, doc) for did, doc in zip(ids, documents)]
        await asyncio.gather(*r2_tasks)

        # 3. Upsert into D1 drawer registry
        d1_tasks = []
        for did, doc, meta in zip(ids, documents, metas):
            wing = str(meta.get("wing", "general"))
            room = str(meta.get("room", "inbox"))
            src = meta.get("source_file")
            chash = hashlib.sha256(doc.encode("utf-8")).hexdigest()
            r2_key = f"drawers/{did}.txt"
            d1_tasks.append(
                self.d1.upsert_drawer(
                    drawer_id=did,
                    wing=wing,
                    room=room,
                    r2_key=r2_key,
                    content_hash=chash,
                    metadata=meta,
                    source_file=src,
                )
            )
        await asyncio.gather(*d1_tasks)

        # 4. Upsert into Cloudflare Vectorize (batch size <= 500)
        vectorize_vectors = []
        for did, vec, meta in zip(ids, vectors, metas):
            vmeta: Dict[str, Any] = {}
            if "wing" in meta:
                vmeta["wing"] = str(meta["wing"])[:64]
            if "room" in meta:
                vmeta["room"] = str(meta["room"])[:64]
            if "source_file" in meta and meta["source_file"]:
                vmeta["source_file"] = str(meta["source_file"])[:64]

            v_item = {
                "id": did,
                "values": vec,
                "metadata": vmeta,
            }
            if self.namespace:
                v_item["namespace"] = self.namespace
            vectorize_vectors.append(v_item)

        upsert_fn = getattr(self.vector_index, "upsert", None)
        if upsert_fn is None:
            raise RuntimeError("Vectorize binding missing 'upsert'")

        for i in range(0, len(vectorize_vectors), 500):
            batch = vectorize_vectors[i : i + 500]
            res = upsert_fn(batch)
            if hasattr(res, "__await__"):
                await res

    async def a_query(
        self,
        *,
        query_texts: Optional[List[str]] = None,
        query_embeddings: Optional[List[List[float]]] = None,
        n_results: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        include: Optional[List[str]] = None,
    ) -> QueryResult:
        """Async query: embed query -> Vectorize ANN -> hydrate verbatim bodies from R2."""
        if not query_texts and not query_embeddings:
            raise ValueError("query_texts or query_embeddings must be provided")

        if query_embeddings:
            q_vec = query_embeddings[0]
        else:
            q_vec = await self.ai.embed_query(query_texts[0])

        query_fn = getattr(self.vector_index, "query", None)
        if query_fn is None:
            raise RuntimeError("vector_index binding missing 'query' method")

        cf_filter = self._translate_where_filter(where)
        kwargs: Dict[str, Any] = {
            "topK": min(n_results, 50),
            "returnMetadata": "all",
        }
        if self.namespace:
            kwargs["namespace"] = self.namespace
        if cf_filter:
            kwargs["filter"] = cf_filter

        res = query_fn(q_vec, **kwargs)
        if hasattr(res, "__await__"):
            res = await res

        matches = []
        if hasattr(res, "to_py"):
            try:
                res = res.to_py()
            except Exception:
                pass

        if isinstance(res, dict):
            matches = res.get("matches", [])
        elif hasattr(res, "matches"):
            matches = res.matches

        hit_ids = [m.get("id") if isinstance(m, dict) else m.id for m in matches]
        scores = [float(m.get("score", 0.0) if isinstance(m, dict) else getattr(m, "score", 0.0)) for m in matches]

        distances = [max(0.0, 1.0 - s) for s in scores]

        docs_map = await self.r2.get_drawers(hit_ids)
        d1_drawers = await self.d1.get_drawers(hit_ids)
        meta_map = {d["id"]: d["metadata"] for d in d1_drawers}

        out_docs = [docs_map.get(did, "") for did in hit_ids]
        out_metas = [meta_map.get(did, {}) for did in hit_ids]

        return QueryResult(
            ids=[hit_ids],
            documents=[out_docs],
            metadatas=[out_metas],
            distances=[distances],
        )

    async def a_get(
        self,
        *,
        ids: Optional[List[str]] = None,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[List[str]] = None,
    ) -> GetResult:
        """Async get: fetch drawer records by IDs or where filter."""
        target_ids = ids
        if target_ids is None and where is not None:
            wing = where.get("wing")
            room = where.get("room")
            lim = limit or 50
            offset_val = offset or 0
            d1_rows = await self.d1.list_drawers(wing=wing, room=room, limit=lim, offset=offset_val)
            target_ids = [r["id"] for r in d1_rows]

        if not target_ids:
            return GetResult(ids=[], documents=[], metadatas=[])

        d1_records = await self.d1.get_drawers(target_ids)
        docs_map = await self.r2.get_drawers(target_ids)

        out_ids = []
        out_docs = []
        out_metas = []

        for rec in d1_records:
            did = rec["id"]
            out_ids.append(did)
            out_docs.append(docs_map.get(did, ""))
            out_metas.append(rec["metadata"])

        return GetResult(
            ids=out_ids,
            documents=out_docs,
            metadatas=out_metas,
        )

    async def a_delete(
        self,
        *,
        ids: Optional[List[str]] = None,
        where: Optional[dict] = None,
    ) -> None:
        """Async delete: remove from Vectorize, R2, and D1."""
        target_ids = ids
        if target_ids is None and where is not None:
            wing = where.get("wing")
            room = where.get("room")
            d1_rows = await self.d1.list_drawers(wing=wing, room=room, limit=1000)
            target_ids = [r["id"] for r in d1_rows]

        if not target_ids:
            return

        await self.r2.delete_drawers(target_ids)
        await self.d1.delete_drawers(target_ids)
        del_fn = getattr(self.vector_index, "deleteByIds", None)
        if del_fn is not None:
            res = del_fn(target_ids)
            if hasattr(res, "__await__"):
                await res

    async def a_count(self) -> int:
        """Async count total drawers via D1 registry."""
        return await self.d1.count()

    def _translate_where_filter(self, where: Optional[dict]) -> Optional[dict]:
        """Translate MemPalace where dictionary to Cloudflare Vectorize filter syntax."""
        if not where:
            return None
        cf_filter: Dict[str, Any] = {}
        for k, v in where.items():
            if k in ("wing", "room", "source_file"):
                if isinstance(v, dict):
                    cf_filter[k] = v
                else:
                    cf_filter[k] = {"$eq": str(v)}
        return cf_filter if cf_filter else None
