"""Unit tests for Cloudflare adapters: Workers AI, R2 Storage, D1 KG, D1 Registry, Vectorize.

Uses in-memory fake Cloudflare bindings to test all CRUD, search, and graph operations.
"""

import asyncio
import math
import sqlite3
from typing import Any, Dict, List, Optional

from mempalace.backends.base import PalaceRef
from mempalace.backends.cloudflare_vectorize import (
    CloudflareVectorizeBackend,
    CloudflareVectorizeCollection,
)
from mempalace.cloudflare.d1_kg import D1KnowledgeGraph
from mempalace.cloudflare.d1_registry import D1DrawerRegistry
from mempalace.cloudflare.r2_storage import R2DrawerStorage
from mempalace.cloudflare.search import execute_hybrid_search
from mempalace.cloudflare.workers_ai import EMBEDDING_DIMENSION, WorkersAIEmbedder


# ── In-Memory Fake Cloudflare Bindings ─────────────────────────────────────


class FakeWorkersAI:
    """Fake Workers AI binding producing deterministic 384-dim embeddings."""

    async def run(self, model: str, payload: dict) -> dict:
        texts = payload.get("text", [])
        data = []
        for text in texts:
            # Generate deterministic unit vector of size 384
            vec = [0.0] * EMBEDDING_DIMENSION
            words = text.lower().split()
            for w in words:
                idx = hash(w) % EMBEDDING_DIMENSION
                vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]
            else:
                vec[0] = 1.0
            data.append(vec)
        return {"data": data}


class FakeR2Object:
    def __init__(self, content: str):
        self._content = content

    async def text(self) -> str:
        return self._content


class FakeR2Bucket:
    """Fake R2 bucket storing objects in a Python dictionary."""

    def __init__(self):
        self.store: Dict[str, str] = {}

    async def put(self, key: str, value: str):
        self.store[key] = value

    async def get(self, key: str) -> Optional[FakeR2Object]:
        if key not in self.store:
            return None
        return FakeR2Object(self.store[key])

    async def delete(self, keys: Any):
        if isinstance(keys, list):
            for k in keys:
                self.store.pop(k, None)
        else:
            self.store.pop(keys, None)


class FakeD1Statement:
    def __init__(self, conn: sqlite3.Connection, sql: str, params: Optional[list] = None):
        self.conn = conn
        self.sql = sql
        self.params = params or []

    def bind(self, *params):
        return FakeD1Statement(self.conn, self.sql, list(params))

    async def all(self) -> dict:
        cur = self.conn.cursor()
        cur.execute(self.sql, self.params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return {"results": [dict(zip(cols, r)) for r in rows]}

    async def run(self) -> dict:
        cur = self.conn.cursor()
        cur.execute(self.sql, self.params)
        self.conn.commit()
        return {"success": True}


class FakeD1Database:
    """Fake D1 database backed by SQLite."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._init_schemas()
        self.batch_calls: List[List[FakeD1Statement]] = []

    def _init_schemas(self):
        with open("migrations/0001_kg.sql") as f:
            self.conn.executescript(f.read())
        with open("migrations/0002_registry.sql") as f:
            self.conn.executescript(f.read())
        self.conn.commit()

    def prepare(self, sql: str) -> FakeD1Statement:
        return FakeD1Statement(self.conn, sql)

    async def batch(self, statements: List[FakeD1Statement]) -> List[dict]:
        self.batch_calls.append(statements)
        results = []
        for stmt in statements:
            cur = self.conn.cursor()
            cur.execute(stmt.sql, stmt.params)
            self.conn.commit()
            results.append({"success": True})
        return results


class FakeVectorizeIndex:
    """Fake Vectorize vector index supporting cosine similarity search & filters."""

    def __init__(self):
        self.vectors: Dict[str, dict] = {}

    async def upsert(self, vectors: List[dict]):
        for v in vectors:
            self.vectors[v["id"]] = v

    async def deleteByIds(self, ids: List[str]):
        for did in ids:
            self.vectors.pop(did, None)

    async def query(
        self, vector: List[float], topK: int = 10, filter: Optional[dict] = None, **kwargs
    ) -> dict:
        matches = []
        for vid, item in self.vectors.items():
            meta = item.get("metadata", {})
            # Check filter
            if filter:
                matched = True
                for k, condition in filter.items():
                    val = meta.get(k)
                    if isinstance(condition, dict) and "$eq" in condition:
                        if val != condition["$eq"]:
                            matched = False
                            break
                    elif val != condition:
                        matched = False
                        break
                if not matched:
                    continue

            # Compute cosine similarity
            ivec = item["values"]
            dot = sum(a * b for a, b in zip(vector, ivec))
            matches.append({"id": vid, "score": dot, "metadata": meta})

        matches.sort(key=lambda m: m["score"], reverse=True)
        return {"matches": matches[:topK]}


# ── Tests ──────────────────────────────────────────────────────────────────


def test_workers_ai_embedder():
    async def _test():
        ai = FakeWorkersAI()
        embedder = WorkersAIEmbedder(ai)
        vectors = await embedder.embed(["hello world", "test memory"])
        assert len(vectors) == 2
        assert len(vectors[0]) == 384
        assert len(vectors[1]) == 384

        q_vec = await embedder.embed_query("query")
        assert len(q_vec) == 384

    asyncio.run(_test())


def test_r2_storage_verbatim():
    async def _test():
        bucket = FakeR2Bucket()
        r2 = R2DrawerStorage(bucket)

        content = "Verbatim Sacred User Memory: Exactly as spoken."
        key = await r2.put_drawer("drawer-1", content)
        assert key == "drawers/drawer-1.txt"

        fetched = await r2.get_drawer("drawer-1")
        assert fetched == content

        # Batch get
        await r2.put_drawer("drawer-2", "Second memory")
        batch = await r2.get_drawers(["drawer-1", "drawer-2"])
        assert batch["drawer-1"] == content
        assert batch["drawer-2"] == "Second memory"

        # Delete
        await r2.delete_drawer("drawer-1")
        assert await r2.get_drawer("drawer-1") is None

    asyncio.run(_test())


def test_d1_knowledge_graph():
    async def _test():
        db = FakeD1Database()
        kg = D1KnowledgeGraph(db)

        # 1. Add fact
        t_id = await kg.add_triple("Max", "child_of", "Alice", valid_from="2020-01-01")
        assert t_id is not None

        # 2. Query entity
        facts = await kg.query_entity("Max")
        assert len(facts) == 1
        assert facts[0]["subject_name"] == "Max"
        assert facts[0]["object_name"] == "Alice"

        # 3. Supersede fact
        await kg.supersede(
            "Max", "works_at", "CompanyA", "Max", "works_at", "CompanyB", boundary="2026-01-01"
        )
        # Verify db.batch was called with batched statements
        assert len(db.batch_calls) >= 1
        cur_facts = await kg.query_entity("Max", as_of="2026-06-01")
        preds = [f["object_name"] for f in cur_facts if f["predicate"] == "works_at"]
        assert "CompanyB" in preds
        assert "CompanyA" not in preds

        # 4. Stats
        stats = await kg.stats()
        assert stats["triple_count"] >= 2
        assert stats["entity_count"] >= 2

    asyncio.run(_test())


def test_d1_registry_and_taxonomy():
    async def _test():
        db = FakeD1Database()
        reg = D1DrawerRegistry(db)

        await reg.upsert_drawer(
            "d1", "tech", "python", "drawers/d1.txt", "hash1", {"author": "rob"}
        )
        await reg.upsert_drawer("d2", "tech", "python", "drawers/d2.txt", "hash2")
        await reg.upsert_drawer("d3", "tech", "rust", "drawers/d3.txt", "hash3")
        await reg.upsert_drawer("d4", "personal", "travel", "drawers/d4.txt", "hash4")

        # Count
        assert await reg.count() == 4
        assert await reg.count(wing="tech") == 3
        assert await reg.count(wing="tech", room="python") == 2

        # Taxonomy
        tax = await reg.get_taxonomy()
        assert tax["tech"]["python"] == 2
        assert tax["tech"]["rust"] == 1
        assert tax["personal"]["travel"] == 1

        # Exact dup check
        assert await reg.check_duplicate("hash1") == "d1"
        assert await reg.check_duplicate("nonexistent") is None

        # Wing/room scoped duplicate check
        await reg.upsert_drawer("d5", "personal", "notes", "drawers/d5.txt", "hash1")
        assert await reg.check_duplicate("hash1", wing="tech") == "d1"
        assert await reg.check_duplicate("hash1", wing="personal") == "d5"
        assert await reg.check_duplicate("hash1", wing="nonexistent_wing") is None

    asyncio.run(_test())


def test_cloudflare_vectorize_collection_full_crud():
    async def _test():
        ai = FakeWorkersAI()
        r2 = FakeR2Bucket()
        db = FakeD1Database()
        vec = FakeVectorizeIndex()

        embedder = WorkersAIEmbedder(ai)
        r2_storage = R2DrawerStorage(r2)
        d1_reg = D1DrawerRegistry(db)

        # Missing upsert raises RuntimeError
        bad_col = CloudflareVectorizeCollection(
            vector_index=object(),
            ai_embedder=embedder,
            r2_storage=R2DrawerStorage(FakeR2Bucket()),
            d1_registry=D1DrawerRegistry(FakeD1Database()),
        )
        try:
            await bad_col.a_upsert(
                documents=["doc without vectorize"],
                ids=["bad-id"],
                metadatas=[{"wing": "test", "room": "err"}],
            )
            assert False, "Should have raised RuntimeError for missing upsert"
        except RuntimeError as e:
            assert "Vectorize binding missing 'upsert'" in str(e)

        col = CloudflareVectorizeCollection(
            vector_index=vec,
            ai_embedder=embedder,
            r2_storage=r2_storage,
            d1_registry=d1_reg,
        )

        docs = [
            "MemPalace stores every word verbatim without lossy summarization.",
            "Cloudflare Workers provides edge computing with low latency.",
        ]
        ids = ["doc-1", "doc-2"]
        metas = [
            {"wing": "projects", "room": "mempalace"},
            {"wing": "tech", "room": "cloudflare"},
        ]

        # Upsert
        await col.a_upsert(documents=docs, ids=ids, metadatas=metas)
        assert await col.a_count() == 2

        # Get
        get_res = await col.a_get(ids=["doc-1"])
        assert get_res.ids == ["doc-1"]
        assert get_res.documents[0] == docs[0]

        # Query
        q_res = await col.a_query(query_texts=["verbatim memory"], n_results=5)
        assert len(q_res.ids[0]) == 2
        assert q_res.ids[0][0] == "doc-1"
        assert q_res.documents[0][0] == docs[0]

        # Hybrid search
        ranked = await execute_hybrid_search(col, query="verbatim memory", n_results=2)
        assert len(ranked) == 2
        assert ranked[0]["id"] == "doc-1"
        assert ranked[0]["text"] == docs[0]

        # Delete
        await col.a_delete(ids=["doc-1"])
        assert await col.a_count() == 1
        assert (await col.a_get(ids=["doc-1"])).ids == []

    asyncio.run(_test())


def test_delete_removes_vectorize_before_d1_and_r2():
    async def _test():
        ai = FakeWorkersAI()
        r2 = FakeR2Bucket()
        db = FakeD1Database()
        vec = FakeVectorizeIndex()
        order: List[str] = []

        embedder = WorkersAIEmbedder(ai)
        r2_storage = R2DrawerStorage(r2)
        d1_reg = D1DrawerRegistry(db)
        col = CloudflareVectorizeCollection(
            vector_index=vec,
            ai_embedder=embedder,
            r2_storage=r2_storage,
            d1_registry=d1_reg,
        )

        await col.a_upsert(
            documents=["A drawer that will be deleted."],
            ids=["doc-del"],
            metadatas=[{"wing": "projects", "room": "cleanup"}],
        )

        original_delete_by_ids = vec.deleteByIds
        original_d1_delete = d1_reg.delete_drawers
        original_r2_delete = r2_storage.delete_drawers

        async def record_vectorize(ids):
            order.append("vectorize")
            await original_delete_by_ids(ids)

        async def record_d1(ids):
            order.append("d1")
            await original_d1_delete(ids)

        async def record_r2(ids):
            order.append("r2")
            await original_r2_delete(ids)

        vec.deleteByIds = record_vectorize
        d1_reg.delete_drawers = record_d1
        r2_storage.delete_drawers = record_r2

        await col.a_delete(ids=["doc-del"])
        assert order == ["vectorize", "d1", "r2"]

    asyncio.run(_test())


def test_query_drops_vectorize_hits_missing_from_d1():
    async def _test():
        ai = FakeWorkersAI()
        r2 = FakeR2Bucket()
        db = FakeD1Database()
        vec = FakeVectorizeIndex()
        col = CloudflareVectorizeCollection(
            vector_index=vec,
            ai_embedder=WorkersAIEmbedder(ai),
            r2_storage=R2DrawerStorage(r2),
            d1_registry=D1DrawerRegistry(db),
        )

        await col.a_upsert(
            documents=["Ghost drawer still indexed by Vectorize."],
            ids=["doc-ghost"],
            metadatas=[{"wing": "projects", "room": "ghosts"}],
        )
        await col.d1.delete_drawers(["doc-ghost"])

        q_res = await col.a_query(query_texts=["ghost drawer"], n_results=5)
        assert "doc-ghost" not in q_res.ids[0]
        assert q_res.documents[0] == []
        assert q_res.metadatas[0] == []
        assert q_res.distances[0] == []

    asyncio.run(_test())


def test_backend_factory_registration():
    ai = FakeWorkersAI()
    r2 = FakeR2Bucket()
    db = FakeD1Database()
    vec = FakeVectorizeIndex()

    backend = CloudflareVectorizeBackend(
        options={"ai": ai, "bucket": r2, "db": db, "vector_index": vec}
    )
    palace = PalaceRef(id="test-palace")
    col = backend.get_collection(palace=palace, collection_name="drawers")
    assert isinstance(col, CloudflareVectorizeCollection)
    assert backend.health().ok is True
