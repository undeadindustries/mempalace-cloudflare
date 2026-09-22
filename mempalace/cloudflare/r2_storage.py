"""Cloudflare R2 verbatim drawer storage adapter.

Stores verbatim text drawers in Cloudflare R2 bucket with key pattern:
`drawers/{drawer_id}.txt`
Never summarizes, compresses, or alters verbatim content.
"""

import asyncio
from typing import Any, Dict, List, Optional


class R2DrawerStorage:
    """Handles verbatim drawer content persistence in Cloudflare R2."""

    def __init__(self, bucket_binding: Any, prefix: str = "drawers/"):
        self.bucket = bucket_binding
        self.prefix = prefix

    def _key(self, drawer_id: str) -> str:
        return f"{self.prefix}{drawer_id}.txt"

    async def put_drawer(self, drawer_id: str, content: str) -> str:
        """Store verbatim drawer content in R2."""
        key = self._key(drawer_id)
        put_fn = getattr(self.bucket, "put", None)
        if put_fn is None:
            raise RuntimeError("R2 bucket binding does not have a 'put' method")

        res = put_fn(key, content)
        if hasattr(res, "__await__"):
            await res
        return key

    async def get_drawer(self, drawer_id: str) -> Optional[str]:
        """Retrieve verbatim content of a single drawer by ID."""
        key = self._key(drawer_id)
        get_fn = getattr(self.bucket, "get", None)
        if get_fn is None:
            raise RuntimeError("R2 bucket binding does not have a 'get' method")

        obj = get_fn(key)
        if hasattr(obj, "__await__"):
            obj = await obj

        if obj is None:
            return None

        # R2 object text extraction (obj.text() or obj.text attribute or reading bytes)
        if hasattr(obj, "text"):
            text_val = obj.text
            if callable(text_val):
                res = text_val()
                if hasattr(res, "__await__"):
                    return await res
                return res
            return text_val
        elif hasattr(obj, "read"):
            data = obj.read()
            if hasattr(data, "__await__"):
                data = await data
            if isinstance(data, bytes):
                return data.decode("utf-8")
            return str(data)

        return str(obj)

    async def get_drawers(self, drawer_ids: List[str]) -> Dict[str, str]:
        """Retrieve multiple verbatim drawers concurrently."""
        if not drawer_ids:
            return {}

        tasks = [self.get_drawer(did) for did in drawer_ids]
        contents = await asyncio.gather(*tasks)

        result: Dict[str, str] = {}
        for did, content in zip(drawer_ids, contents):
            if content is not None:
                result[did] = content
        return result

    async def delete_drawer(self, drawer_id: str) -> None:
        """Delete a single drawer from R2."""
        await self.delete_drawers([drawer_id])

    async def delete_drawers(self, drawer_ids: List[str]) -> None:
        """Delete multiple drawers from R2."""
        if not drawer_ids:
            return

        keys = [self._key(did) for did in drawer_ids]
        delete_fn = getattr(self.bucket, "delete", None)
        if delete_fn is None:
            raise RuntimeError("R2 bucket binding does not have a 'delete' method")

        # In Workers, delete can accept a single key or a list of keys
        res = delete_fn(keys)
        if hasattr(res, "__await__"):
            await res
