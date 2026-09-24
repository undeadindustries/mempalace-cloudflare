"""Cloudflare Remote Backend Plugin for MemPalace.

Enables local CLI, hooks, and MCP servers to connect to a Cloudflare Workers
MemPalace deployment over HTTPS using Bearer token authentication, plus a
Cloudflare Access service token when the Worker is behind Access.
Zero external dependencies — uses Python standard library `urllib`.
"""

from __future__ import annotations

import json
import os
from typing import Any, ClassVar, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from mempalace.backends.base import (
    BaseBackend,
    BaseCollection,
    GetResult,
    HealthStatus,
    PalaceRef,
    QueryResult,
)

USER_AGENT = "mempalace-cloudflare-remote/0.1.0"
ACCESS_CLIENT_ID_ENV = "CF_ACCESS_CLIENT_ID"
ACCESS_CLIENT_SECRET_ENV = "CF_ACCESS_CLIENT_SECRET"
_HTTP_FORBIDDEN = 403


def resolve_access_headers(options: Dict[str, Any]) -> Dict[str, str]:
    """Build the Cloudflare Access service-token headers, or none.

    Access rejects requests without a valid service token at Cloudflare's edge,
    before the Worker runs, so floods never count against the Worker's request
    quota. Both halves must be present: sending only one would be rejected by
    Access anyway, and a silent half-configuration is hard to diagnose.
    """
    client_id = options.get("access_client_id") or os.environ.get(ACCESS_CLIENT_ID_ENV) or ""
    secret = options.get("access_client_secret") or os.environ.get(ACCESS_CLIENT_SECRET_ENV) or ""
    if bool(client_id) != bool(secret):
        missing = ACCESS_CLIENT_SECRET_ENV if client_id else ACCESS_CLIENT_ID_ENV
        raise ValueError(
            f"Cloudflare Access needs both {ACCESS_CLIENT_ID_ENV} and "
            f"{ACCESS_CLIENT_SECRET_ENV}; {missing} is not set."
        )
    if not client_id:
        return {}
    return {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": secret}


class CloudflareRemoteCollection(BaseCollection):
    """Collection proxying operations to a remote Cloudflare Worker over HTTP."""

    def __init__(
        self,
        base_url: str,
        token: str,
        namespace: Optional[str] = None,
        *,
        access_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.access_headers = dict(access_headers or {})
        self._next_rpc_id = 1

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += f"?{urllib.parse.urlencode(params)}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
            **self.access_headers,
        }
        req_body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            req_body = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(url, data=req_body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            hint = ""
            if e.code == _HTTP_FORBIDDEN and not self.access_headers:
                hint = (
                    " (likely blocked by Cloudflare Access: set "
                    f"{ACCESS_CLIENT_ID_ENV} and {ACCESS_CLIENT_SECRET_ENV})"
                )
            raise RuntimeError(f"Cloudflare Worker HTTP {e.code}{hint}: {err_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Failed to connect to Cloudflare Worker at {self.base_url}: {e.reason}"
            ) from e

    def _mcp_call(self, name: str, arguments: Dict[str, Any]) -> Any:
        """POST one JSON-RPC tools/call. An id is required so the Worker executes it.

        Messages without an id are notifications: the Worker answers 202 and
        does not run the tool.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        self._next_rpc_id += 1
        return self._request("POST", "/mcp", data=payload)

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
        metas = metadatas or [{} for _ in documents]
        drawers = []
        for did, doc, meta in zip(ids, documents, metas):
            drawers.append(
                {
                    "id": did,
                    "wing": meta.get("wing", "general"),
                    "room": meta.get("room", "inbox"),
                    "content": doc,
                    "source_file": meta.get("source_file"),
                }
            )

        self._mcp_call("mempalace_checkpoint", {"drawers": drawers})

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
        if not query_texts:
            return QueryResult(ids=[[]], documents=[[]], metadatas=[[]], distances=[[]])

        query_str = query_texts[0]
        wing = where.get("wing") if where else None
        room = where.get("room") if where else None

        res = self._request(
            "POST",
            "/api/search",
            data={
                "query": query_str,
                "wing": wing,
                "room": room,
                "max_results": n_results,
            },
        )
        hits = res.get("results", [])

        ids = [h["id"] for h in hits]
        docs = [h.get("text", "") for h in hits]
        metas = [h.get("metadata", {}) for h in hits]
        distances = [h.get("distance", 0.0) for h in hits]

        return QueryResult(
            ids=[ids],
            documents=[docs],
            metadatas=[metas],
            distances=[distances],
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
        if ids:
            res = self._mcp_call("mempalace_get_drawers", {"drawer_ids": ids})
            content_str = res.get("result", {}).get("content", [{}])[0].get("text", "[]")
            items = json.loads(content_str)
            out_ids = [it["drawer_id"] for it in items]
            out_docs = [it["content"] for it in items]
            out_metas = [it["metadata"] for it in items]
            return GetResult(ids=out_ids, documents=out_docs, metadatas=out_metas)

        params: Dict[str, Any] = {"content": "true"}
        if where:
            if "wing" in where:
                params["wing"] = where["wing"]
            if "room" in where:
                params["room"] = where["room"]
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        res = self._request("GET", "/api/drawers", params=params)
        drawers = res.get("drawers", [])
        return GetResult(
            ids=[d["id"] for d in drawers],
            documents=[d.get("content", "") for d in drawers],
            metadatas=[d.get("metadata", {}) for d in drawers],
        )

    def delete(
        self,
        *,
        ids: Optional[List[str]] = None,
        where: Optional[dict] = None,
    ) -> None:
        if ids:
            self._mcp_call("mempalace_delete_drawers", {"drawer_ids": ids})

    def count(self) -> int:
        res = self._request("GET", "/api/status")
        return int(res.get("total_drawers", 0))


class CloudflareRemoteBackend(BaseBackend):
    """MemPalace Backend factory connecting to a remote Cloudflare Worker."""

    name: ClassVar[str] = "cloudflare-remote"
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

        url = (
            opts.get("url") or os.environ.get("MEMPALACE_CLOUDFLARE_URL") or "http://localhost:8787"
        )
        token = (
            opts.get("token")
            or os.environ.get("MEMPALACE_CLOUDFLARE_TOKEN")
            or os.environ.get("MEMPALACE_API_KEY")
            or ""
        )

        return CloudflareRemoteCollection(
            base_url=url,
            token=token,
            namespace=palace.namespace,
            access_headers=resolve_access_headers(opts),
        )

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        url = (
            self.options.get("url")
            or os.environ.get("MEMPALACE_CLOUDFLARE_URL")
            or "http://localhost:8787"
        ).rstrip("/")
        try:
            headers = {"User-Agent": USER_AGENT, **resolve_access_headers(self.options)}
            req = urllib.request.Request(f"{url}/healthz", headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return HealthStatus.healthy(f"Connected to Cloudflare Worker at {url}")
        except Exception as e:
            return HealthStatus.unhealthy(f"Cannot reach Cloudflare Worker at {url}: {e}")
        return HealthStatus.healthy()
