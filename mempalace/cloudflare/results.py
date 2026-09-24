"""Core result dataclasses for Cloudflare Workers standalone bundle.

Matches upstream mempalace.backends.base typed results without ChromaDB/native dependencies.
"""

from dataclasses import dataclass
from typing import Optional

_TYPED_RESULT_FIELDS = ("ids", "documents", "metadatas", "distances", "embeddings")


class _DictCompatMixin:
    """Dict-compatibility mixin for QueryResult and GetResult."""

    def __getitem__(self, key: str):
        if key in _TYPED_RESULT_FIELDS:
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default=None):
        if key in _TYPED_RESULT_FIELDS:
            val = getattr(self, key, default)
            return default if val is None else val
        return default

    def __contains__(self, key: object) -> bool:
        return key in _TYPED_RESULT_FIELDS and getattr(self, key, None) is not None


@dataclass(frozen=True)
class QueryResult(_DictCompatMixin):
    """Typed return from collection query."""

    ids: list[list[str]]
    documents: list[list[str]]
    metadatas: list[list[dict]]
    distances: list[list[float]]
    embeddings: Optional[list[list[list[float]]]] = None


@dataclass(frozen=True)
class GetResult(_DictCompatMixin):
    """Typed return from collection get."""

    ids: list[str]
    documents: list[str]
    metadatas: list[dict]
    embeddings: Optional[list[list[float]]] = None
