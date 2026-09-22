"""Workers AI embedding provider for Cloudflare Workers.

Uses `@cf/baai/bge-small-en-v1.5` (384-dimensional) via the Cloudflare Workers AI binding.
Supports async execution and batching for embedding documents and queries.
"""

from typing import Any, List, Optional


DEFAULT_EMBEDDING_MODEL = "@cf/baai/bge-small-en-v1.5"
EMBEDDING_DIMENSION = 384
MAX_BATCH_SIZE = 100


class WorkersAIEmbedder:
    """Embedder backed by Cloudflare Workers AI."""

    def __init__(self, ai_binding: Any, model: str = DEFAULT_EMBEDDING_MODEL):
        self.ai = ai_binding
        self.model = model
        self.dimension = EMBEDDING_DIMENSION

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a list of texts into 384-dimensional vector representations.

        Handles chunking into batches of MAX_BATCH_SIZE to respect payload limits.
        """
        if not texts:
            return []

        embeddings: List[List[float]] = []

        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i : i + MAX_BATCH_SIZE]
            res = await self._run_inference(batch)
            embeddings.extend(res)

        return embeddings

    async def embed_query(self, query: str) -> List[float]:
        """Embed a single query string."""
        results = await self.embed([query])
        if not results:
            return [0.0] * self.dimension
        return results[0]

    async def _run_inference(self, batch: List[str]) -> List[List[float]]:
        """Execute inference against the Workers AI binding."""
        # Cloudflare AI binding accepts {"text": list_of_strings}
        payload = {"text": batch}

        # Handle both async and sync AI binding runs
        run_fn = getattr(self.ai, "run", None)
        if run_fn is None:
            raise RuntimeError("Workers AI binding does not have a 'run' method")

        res = run_fn(self.model, payload)
        if hasattr(res, "__await__"):
            res = await res

        # Workers AI returns {"shape": [...], "data": [[...], ...]}
        # or an object with a .data attribute
        if isinstance(res, dict):
            data = res.get("data", [])
        elif hasattr(res, "data"):
            data = res.data
        else:
            raise ValueError(f"Unexpected response format from Workers AI: {type(res)}")

        return data
