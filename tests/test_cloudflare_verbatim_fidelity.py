"""Verbatim fidelity tests for Cloudflare MemPalace.

Verifies the sacred invariant: "Never summarize, paraphrase, or lossy-compress user data.
Verbatim always — exact bytes in -> exact bytes out."
Tests across Unicode, multiline code, special punctuation, and complex text.
"""

import asyncio

from mempalace.backends.cloudflare_vectorize import CloudflareVectorizeCollection
from mempalace.cloudflare.d1_registry import D1DrawerRegistry
from mempalace.cloudflare.r2_storage import R2DrawerStorage
from mempalace.cloudflare.workers_ai import WorkersAIEmbedder
from tests.test_cloudflare_adapters import (
    FakeD1Database,
    FakeR2Bucket,
    FakeVectorizeIndex,
    FakeWorkersAI,
)


def test_verbatim_fidelity_exact_bytes():
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

        sacred_texts = [
            # 1. Complex code snippet with whitespace, tabs, and indentation
            """def calculate_recall(retrieved: list, ground_truth: set) -> float:
\t\"\"\"Compute 100% recall metric verbatim.\"\"\"
    if not ground_truth:
        return 1.0
    return len(set(retrieved) & ground_truth) / len(ground_truth)
""",
            # 2. Rich unicode, emojis, accents, and non-latin text
            "Memoria del palacio: ¡MemPalace es sagrado! 🧠 🏛️ 🚀 漢字とひらがな и русский текст.",
            # 3. Raw JSON and Markdown with backticks and formatting
            '{"event": "system_prompt", "tags": ["verbatim", "zero-loss"], "quote": "Memory is identity."}',
            # 4. Leading/trailing whitespace and unusual characters
            "  \n\r\t---BEGIN USER DATA---\nSpecial symbols: @#$%^&*()_+-=[]{}|;':\",./<>?`~\n---END USER DATA---\n\t  ",
        ]

        ids = [f"verbatim-{i}" for i in range(len(sacred_texts))]
        metas = [{"wing": "fidelity", "room": f"test_{i}"} for i in range(len(sacred_texts))]

        # Store
        await col.a_upsert(documents=sacred_texts, ids=ids, metadatas=metas)

        # Retrieve and verify exact byte-for-byte equality
        result = await col.a_get(ids=ids)
        assert len(result.documents) == len(sacred_texts)

        for original, retrieved, did in zip(sacred_texts, result.documents, ids):
            assert retrieved == original, f"Fidelity violation on drawer {did}!"
            assert len(retrieved) == len(original)
            assert retrieved.encode("utf-8") == original.encode("utf-8")

    asyncio.run(_test())
