"""Cloudflare Workers entrypoint for MemPalace.

Provides an ASGI 3.0 serverless entrypoint exposed via `workers.asgi`.
Handles:
- Bearer token authentication middleware
- MCP Streamable HTTP protocol (POST /mcp) with 24 memory tools
- Direct REST endpoints for health, search, and drawer CRUD
- Dynamic Cloudflare binding resolution (env.AI, env.VECTOR_INDEX, env.DB, env.BUCKET)
"""

import hmac
import json
import logging
import os
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

# Maximum page size for list endpoints and tool queries
MAX_PAGE_LIMIT = 100

# JSON-RPC notifications are owed no body. Upstream MCP HTTP uses 202.
_HTTP_ACCEPTED = 202
_HTTP_METHOD_NOT_ALLOWED = 405

_MCP_PATH = "/mcp"
# Streamable HTTP: a server with no SSE stream MUST answer GET with 405, and
# MAY answer session-terminating DELETE with 405. Only POST carries messages.
_MCP_ALLOWED_METHOD = "POST"

# Newest first. The Worker only serves tools with JSON responses, which every
# version listed here supports unchanged.
_SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def negotiate_protocol_version(requested: object) -> str:
    """Pick the MCP protocol version to answer ``initialize`` with.

    The spec requires echoing the client's version when the server supports
    it, and otherwise offering the newest one the server does support. Clients
    such as Cursor may drop the connection if the server answers with an older
    version than they asked for.
    """
    if requested in _SUPPORTED_PROTOCOL_VERSIONS:
        return requested  # type: ignore[return-value]
    return _SUPPORTED_PROTOCOL_VERSIONS[0]


# Safe imports supporting both top-level deployment and module execution
try:
    from ._shims import install_shims
    from .d1_kg import D1KnowledgeGraph
    from .d1_registry import D1DrawerRegistry
    from .r2_storage import R2DrawerStorage
    from .tools import CloudflarePalaceTools
    from .vectorize_collection import CloudflareVectorizeCollection
    from .workers_ai import WorkersAIEmbedder
    from ..version import __version__
except (ImportError, ValueError):
    from _shims import install_shims  # type: ignore
    from d1_kg import D1KnowledgeGraph  # type: ignore
    from d1_registry import D1DrawerRegistry  # type: ignore
    from r2_storage import R2DrawerStorage  # type: ignore
    from tools import CloudflarePalaceTools  # type: ignore
    from vectorize_collection import CloudflareVectorizeCollection  # type: ignore
    from workers_ai import WorkersAIEmbedder  # type: ignore

    __version__ = "3.10.0"

install_shims()


def coerce_body_bytes(value: object) -> bytes:
    """Normalize a Pyodide request body to ``bytes``.

    ``JsProxy.to_py()`` on a Uint8Array can yield ``bytes``, ``bytearray``,
    ``memoryview``, or a ``list`` of ints. The ASGI handler decodes the body
    as UTF-8, so every shape has to become ``bytes`` first.
    """
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytearray, memoryview, list)):
        return bytes(value)
    return bytes(value)


class CloudflareMemPalaceApp:
    """Serverless ASGI Application for MemPalace on Cloudflare Workers."""

    def __init__(self, env: Optional[Any] = None):
        self.env = env

    def _get_env_val(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if self.env is not None and hasattr(self.env, key):
            val = getattr(self.env, key)
            if isinstance(val, str):
                return val
        return os.environ.get(key, default)

    def _get_binding(self, key: str) -> Any:
        if self.env is not None and hasattr(self.env, key):
            return getattr(self.env, key)
        return None

    def _get_tools(self, env: Any) -> CloudflarePalaceTools:
        ai = getattr(env, "AI", None)
        vec = getattr(env, "VECTOR_INDEX", None)
        db = getattr(env, "DB", None)
        bucket = getattr(env, "BUCKET", None)

        embedder = WorkersAIEmbedder(ai)
        r2 = R2DrawerStorage(bucket)
        d1_reg = D1DrawerRegistry(db)
        d1_kg = D1KnowledgeGraph(db)

        col = CloudflareVectorizeCollection(
            vector_index=vec,
            ai_embedder=embedder,
            r2_storage=r2,
            d1_registry=d1_reg,
        )
        return CloudflarePalaceTools(collection=col, d1_kg=d1_kg, d1_registry=d1_reg, r2_storage=r2)

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        """Standard ASGI 3.0 handler."""
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()
        headers = dict(scope.get("headers", []))

        # Cloudflare Workers environment is passed in scope["env"] by workers.asgi
        env = scope.get("env") or self.env

        # 1. Health check is always unauthenticated
        if path == "/healthz" and method == "GET":
            await self._send_json(
                send,
                200,
                {
                    "status": "ok",
                    "service": "mempalace-cloudflare",
                    "version": __version__,
                },
            )
            return

        # 2. Bearer Authentication Middleware
        expected_token = None
        if env is not None and hasattr(env, "MEMPALACE_API_KEY"):
            expected_token = getattr(env, "MEMPALACE_API_KEY")
        if not expected_token:
            expected_token = os.environ.get("MEMPALACE_API_KEY")

        if not expected_token:
            await self._send_json(
                send,
                503,
                {"error": "Service Unavailable: MEMPALACE_API_KEY is not configured"},
            )
            return

        auth_header = headers.get(b"authorization", b"").decode("utf-8")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

        if not token or not hmac.compare_digest(
            token.encode("utf-8"), expected_token.encode("utf-8")
        ):
            await self._send_json(
                send,
                401,
                {"error": "Unauthorized: Invalid or missing Bearer token"},
            )
            return

        if path == _MCP_PATH and method != _MCP_ALLOWED_METHOD:
            await self._send_method_not_allowed(send, _MCP_ALLOWED_METHOD)
            return

        # 3. Read request body
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        body_json = {}
        if body:
            try:
                body_json = json.loads(body.decode("utf-8"))
            except Exception:
                await self._send_json(send, 400, {"error": "Invalid JSON body"})
                return

        # 4. Route Dispatch
        tools = self._get_tools(env)

        if path == _MCP_PATH:
            await self._handle_mcp(send, body_json, tools)
            return

        # REST endpoints
        if path == "/api/status" and method == "GET":
            res = await tools.tool_status()
            await self._send_json(send, 200, res)
            return

        if path == "/api/taxonomy" and method == "GET":
            res = await tools.tool_get_taxonomy()
            await self._send_json(send, 200, res)
            return

        if path == "/api/search" and method == "POST":
            q = body_json.get("query", "")
            wing = body_json.get("wing")
            room = body_json.get("room")
            raw_max = body_json.get("max_results", 10)
            try:
                max_res = int(raw_max)
                if max_res < 1:
                    max_res = 10
            except (ValueError, TypeError):
                max_res = 10
            max_res = min(max_res, MAX_PAGE_LIMIT)
            res = await tools.tool_search(query=q, wing=wing, room=room, max_results=max_res)
            await self._send_json(send, 200, {"results": res})
            return

        if path == "/api/drawers" and method == "POST":
            w = body_json.get("wing", "general")
            r = body_json.get("room", "inbox")
            c = body_json.get("content", "")
            src = body_json.get("source_file")
            did = body_json.get("drawer_id") or body_json.get("id")
            res = await tools.tool_add_drawer(
                wing=w, room=r, content=c, source_file=src, drawer_id=did
            )
            await self._send_json(send, 200, res)
            return

        if path.startswith("/api/drawers/") and method == "GET":
            did = path[len("/api/drawers/") :]
            res = await tools.tool_get_drawer(drawer_id=did)
            status_code = 404 if "error" in res else 200
            await self._send_json(send, status_code, res)
            return

        if path.startswith("/api/drawers/") and method == "DELETE":
            did = path[len("/api/drawers/") :]
            res = await tools.tool_delete_drawer(drawer_id=did)
            await self._send_json(send, 200, res)
            return

        if path == "/api/drawers" and method == "GET":
            await self._handle_get_drawers(scope, send, tools)
            return

        await self._send_json(send, 404, {"error": f"Not found: {method} {path}"})

    async def _handle_get_drawers(
        self, scope: Dict[str, Any], send: Callable, tools: CloudflarePalaceTools
    ) -> None:
        query_str = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_str)
        wing = params.get("wing", [None])[0]
        room = params.get("room", [None])[0]

        raw_limit = params.get("limit", ["50"])[0]
        try:
            limit = int(raw_limit)
            if limit < 0:
                raise ValueError
        except (ValueError, TypeError):
            await self._send_json(send, 400, {"error": f"Invalid limit parameter: {raw_limit}"})
            return

        raw_offset = params.get("offset", ["0"])[0]
        try:
            offset = int(raw_offset)
            if offset < 0:
                raise ValueError
        except (ValueError, TypeError):
            await self._send_json(send, 400, {"error": f"Invalid offset parameter: {raw_offset}"})
            return

        limit = min(limit, MAX_PAGE_LIMIT)

        content_param = params.get("content", ["false"])[0].lower()
        include_content = content_param in ("true", "1", "yes")
        res = await tools.tool_list_drawers(
            wing=wing, room=room, limit=limit, offset=offset, include_content=include_content
        )
        await self._send_json(send, 200, {"drawers": res})

    async def _handle_mcp(
        self,
        send: Callable,
        body: Dict[str, Any],
        tools: CloudflarePalaceTools,
    ) -> None:
        """Handle MCP JSON-RPC 2.0 requests."""
        # A missing id means a notification. Do not dispatch and do not
        # return a JSON-RPC body — Cursor treats a result here as a failed handshake.
        if "id" not in body:
            await self._send_empty(send, _HTTP_ACCEPTED)
            return

        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params", {})

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": negotiate_protocol_version(
                        params.get("protocolVersion") if isinstance(params, dict) else None
                    ),
                    "serverInfo": {
                        "name": "mempalace-cloudflare",
                        "version": __version__,
                    },
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                },
            }
            await self._send_json(send, 200, resp)
            return

        if method == "tools/list":
            tool_list = tools.get_tool_definitions()
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": tool_list},
            }
            await self._send_json(send, 200, resp)
            return

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            call_res = await tools.call_tool(tool_name, tool_args)
            content_text = json.dumps(call_res) if not isinstance(call_res, str) else call_res

            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": content_text}],
                    "isError": "error" in call_res if isinstance(call_res, dict) else False,
                },
            }
            await self._send_json(send, 200, resp)
            return

        # Method not found
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
        await self._send_json(send, 200, resp)

    async def _send_empty(self, send: Callable, status: int) -> None:
        """Send a response with no body. Used for JSON-RPC notifications."""
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _send_method_not_allowed(self, send: Callable, allowed: str) -> None:
        """Send 405 with the ``Allow`` header RFC 9110 requires on that status."""
        body = json.dumps({"error": f"Method not allowed. Use {allowed}."}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": _HTTP_METHOD_NOT_ALLOWED,
                "headers": [
                    (b"allow", allowed.encode("ascii")),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_json(self, send: Callable, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )


app = CloudflareMemPalaceApp()

# Export for Cloudflare Workers Python runtime
try:
    from workers import asgi

    Default = asgi.entrypoint(app)
except Exception:
    Default = app


async def on_fetch(request, env):
    import js
    from pyodide.ffi import to_js

    # Convert JS Request to ASGI scope
    url_str = str(request.url)
    from urllib.parse import urlparse

    parsed = urlparse(url_str)

    headers_list = []
    # Inspect request headers
    try:
        entries = request.headers.entries()
        for pair in entries:
            headers_list.append(
                (str(pair[0]).lower().encode("utf-8"), str(pair[1]).encode("utf-8"))
            )
    except Exception:
        logger.exception("failed to read request headers")

    scope = {
        "type": "http",
        "method": str(request.method).upper(),
        "path": parsed.path or "/",
        "raw_path": (parsed.path or "/").encode("utf-8"),
        "query_string": (parsed.query or "").encode("utf-8"),
        "headers": headers_list,
        "env": env,
    }

    # Read body. arrayBuffer().to_bytes() is the documented Pyodide path;
    # coerce_body_bytes covers the shapes to_py() has returned in this runtime.
    try:
        body_raw = await request.arrayBuffer()
        body_bytes = coerce_body_bytes(body_raw.to_bytes())
    except Exception:
        logger.exception("request.arrayBuffer() failed; falling back to request.text()")
        try:
            body_text = await request.text()
            body_bytes = coerce_body_bytes(body_text)
        except Exception:
            logger.exception("request.text() failed; treating body as empty")
            body_bytes = b""

    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    response_status = 200
    response_headers = []
    response_body = []

    async def send(message):
        nonlocal response_status, response_headers, response_body
        m_type = message.get("type")
        if m_type == "http.response.start":
            response_status = message.get("status", 200)
            for k, v in message.get("headers", []):
                response_headers.append((k.decode("latin1"), v.decode("latin1")))
        elif m_type == "http.response.body":
            b = message.get("body", b"")
            if b:
                response_body.append(b)

    try:
        await app(scope, receive, send)
    except Exception as exc:
        cf_ray = "unknown"
        try:
            for h_name, h_val in headers_list:
                if h_name == b"cf-ray":
                    cf_ray = h_val.decode("latin1", errors="replace")
                    break
        except Exception:
            pass
        logger.exception("unhandled error in on_fetch (request_id=%s): %s", cf_ray, exc)
        err_payload = json.dumps({"error": "Internal Server Error", "request_id": cf_ray}).encode(
            "utf-8"
        )
        err_headers = js.Headers.new()
        err_headers.append("content-type", "application/json")
        init = js.Object.fromEntries(to_js([["status", 500], ["headers", err_headers]]))
        return js.Response.new(to_js(err_payload), init)

    full_body = b"".join(response_body)
    js_headers = js.Headers.new()
    for k, v in response_headers:
        js_headers.append(k, v)

    init = js.Object.fromEntries(
        to_js(
            [
                ["status", response_status],
                ["headers", js_headers],
            ]
        )
    )
    return js.Response.new(to_js(full_body), init)
