"""Cloudflare Vectorize + R2 + D1 backend adapter for MemPalace.

Implements `BaseBackend` and `BaseCollection` from `mempalace.backends.base`.
Coordinates:
- Vectorize: approximate nearest neighbor (ANN) vector search (384-dim cosine)
- Workers AI: on-demand embedding via `@cf/baai/bge-small-en-v1.5`
- R2: verbatim drawer text storage
- D1: drawer registry (metadata, taxonomy, content hash)

Additive-only: sits alongside upstream backends without modifying existing files.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, ClassVar, Dict, List, Optional

from .base import (
    BaseBackend,
    BaseCollection,
    GetResult,
    HealthStatus,
    PalaceRef,
    QueryResult,
)
from ..cloudflare.d1_registry import D1DrawerRegistry
from ..cloudflare.r2_storage import R2DrawerStorage
from ..cloudflare.vectorize_collection import CloudflareVectorizeCollection as CloudflareVectorizeCollectionBase
from ..cloudflare.workers_ai import WorkersAIEmbedder


def _run_async(coro):
    """Run an async coroutine synchronously if no loop is running, or create a task."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # In an existing loop (e.g. running inside Workers/ASGI), if a sync method is called:
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


class CloudflareVectorizeCollection(CloudflareVectorizeCollectionBase, BaseCollection):
    """Collection implementation backed by Vectorize, R2, and D1."""

    # ── Sync BaseCollection Interface Implementation ──────────────────────

    def add(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[dict]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        self.upsert(documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings)

    def upsert(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[dict]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        _run_async(self.a_upsert(documents=documents, ids=ids, metadatas=metadatas, embeddings=embeddings))

    def query(
        self,
        *,
        query_texts: Optional[List[str]] = None,
        query_embeddings: Optional[List[List[float]]] = None,
        n_results: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        include: Optional[List[str]] = None,
    ) -> QueryResult:
        return _run_async(
            self.a_query(
                query_texts=query_texts,
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where,
                where_document=where_document,
                include=include,
            )
        )

    def get(
        self,
        *,
        ids: Optional[List[str]] = None,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[List[str]] = None,
    ) -> GetResult:
        return _run_async(
            self.a_get(
                ids=ids,
                where=where,
                where_document=where_document,
                limit=limit,
                offset=offset,
                include=include,
            )
        )

    def delete(
        self,
        *,
        ids: Optional[List[str]] = None,
        where: Optional[dict] = None,
    ) -> None:
        _run_async(self.a_delete(ids=ids, where=where))

    def count(self) -> int:
        return _run_async(self.a_count())


class CloudflareVectorizeBackend(BaseBackend):
    """Factory creating CloudflareVectorizeCollection instances."""

    name: ClassVar[str] = "cloudflare"
    spec_version: ClassVar[str] = "1.0"
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {
            "supports_namespace_isolation",
            "server_mode",
        }
    )
    distance_metric: ClassVar[str] = "cosine"
    maintenance_kinds: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, options: Optional[dict] = None):
        self.options = options or {}

    def get_collection(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        create: bool = False,
        options: Optional[dict] = None,
    ) -> BaseCollection:
        self.require_namespace_support(palace)
        opts = {**self.options, **(options or {})}

        vector_index = opts.get("vector_index")
        ai = opts.get("ai")
        db = opts.get("db")
        bucket = opts.get("bucket")

        if not vector_index or not ai or not db or not bucket:
            raise ValueError(
                "CloudflareVectorizeBackend requires 'vector_index', 'ai', 'db', and 'bucket' bindings in options"
            )

        ai_embedder = WorkersAIEmbedder(ai)
        r2_storage = R2DrawerStorage(bucket)
        d1_registry = D1DrawerRegistry(db)

        return CloudflareVectorizeCollection(
            vector_index=vector_index,
            ai_embedder=ai_embedder,
            r2_storage=r2_storage,
            d1_registry=d1_registry,
            namespace=palace.namespace,
        )

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        return HealthStatus.healthy("Cloudflare Vectorize/D1/R2 backend active")


try:
    from .registry import register

    register("cloudflare", CloudflareVectorizeBackend)
except Exception:
    pass


