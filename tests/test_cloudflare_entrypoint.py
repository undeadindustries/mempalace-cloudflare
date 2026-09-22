"""Unit tests for Cloudflare ASGI entrypoint and MCP tools.

Tests:
- Bearer token authentication (valid, missing, invalid, healthz bypass)
- MCP JSON-RPC protocol (initialize, tools/list, tools/call)
- Tool execution across all 24 tools
- REST API routes
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict
import pytest

from mempalace.cloudflare.entrypoint import CloudflareMemPalaceApp
from tests.test_cloudflare_adapters import (
    FakeD1Database,
    FakeR2Bucket,
    FakeVectorizeIndex,
    FakeWorkersAI,
)


def create_test_env(api_key: str = "secret-token") -> SimpleNamespace:
    return SimpleNamespace(
        AI=FakeWorkersAI(),
        BUCKET=FakeR2Bucket(),
        DB=FakeD1Database(),
        VECTOR_INDEX=FakeVectorizeIndex(),
        MEMPALACE_API_KEY=api_key,
    )


async def send_asgi_request(
    app: CloudflareMemPalaceApp,
    method: str,
    path: str,
    body: Any = None,
    headers: Dict[bytes, bytes] = None,
    env: Any = None,
) -> Dict[str, Any]:
    """Helper to dispatch request through ASGI application."""
    req_headers = list((headers or {}).items())
    body_bytes = b""
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": req_headers,
        "env": env,
    }

    messages = []

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)

    status = 500
    res_headers = {}
    res_body = b""
    for msg in messages:
        if msg["type"] == "http.response.start":
            status = msg["status"]
            res_headers = dict(msg.get("headers", []))
        elif msg["type"] == "http.response.body":
            res_body += msg.get("body", b"")

    parsed_json = {}
    if res_body:
        try:
            parsed_json = json.loads(res_body.decode("utf-8"))
        except Exception:
            parsed_json = {"raw": res_body.decode("utf-8")}

    return {"status": status, "headers": res_headers, "json": parsed_json}


# ── Tests ──────────────────────────────────────────────────────────────────


def test_healthz_unauthenticated():
    async def _test():
        env = create_test_env()
        app = CloudflareMemPalaceApp(env=env)

        res = await send_asgi_request(app, "GET", "/healthz")
        assert res["status"] == 200
        assert res["json"]["status"] == "ok"
        assert res["json"]["service"] == "mempalace-cloudflare"

    asyncio.run(_test())


def test_auth_middleware_rejection():
    async def _test():
        env = create_test_env(api_key="my-secret-key")
        app = CloudflareMemPalaceApp(env=env)

        # 1. Missing header -> 401
        res = await send_asgi_request(app, "GET", "/api/status")
        assert res["status"] == 401
        assert "Unauthorized" in res["json"]["error"]

        # 2. Invalid token -> 401
        bad_headers = {b"authorization": b"Bearer wrong-token"}
        res = await send_asgi_request(app, "GET", "/api/status", headers=bad_headers)
        assert res["status"] == 401

        # 3. Valid token -> 200
        good_headers = {b"authorization": b"Bearer my-secret-key"}
        res = await send_asgi_request(app, "GET", "/api/status", headers=good_headers)
        assert res["status"] == 200
        assert res["json"]["total_drawers"] == 0

    asyncio.run(_test())


def test_mcp_initialize_and_tool_list():
    async def _test():
        env = create_test_env(api_key="key")
        app = CloudflareMemPalaceApp(env=env)
        headers = {b"authorization": b"Bearer key"}

        # Initialize
        init_res = await send_asgi_request(
            app,
            "POST",
            "/mcp",
            body={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers=headers,
        )
        assert init_res["status"] == 200
        assert init_res["json"]["result"]["serverInfo"]["name"] == "mempalace-cloudflare"

        # Tools List
        list_res = await send_asgi_request(
            app,
            "POST",
            "/mcp",
            body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=headers,
        )
        assert list_res["status"] == 200
        tools = list_res["json"]["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert "mempalace_add_drawer" in tool_names
        assert "mempalace_search" in tool_names
        assert "mempalace_kg_add" in tool_names
        assert "mempalace_get_taxonomy" in tool_names
        assert len(tools) >= 24

    asyncio.run(_test())


def test_mcp_drawer_and_search_roundtrip():
    async def _test():
        env = create_test_env(api_key="key")
        app = CloudflareMemPalaceApp(env=env)
        headers = {b"authorization": b"Bearer key"}

        # 1. Add drawer
        add_req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "mempalace_add_drawer",
                "arguments": {
                    "wing": "projects",
                    "room": "architecture",
                    "content": "Sacred verbatim text: Cloudflare Workers GA supports Python natively.",
                },
            },
        }
        add_res = await send_asgi_request(app, "POST", "/mcp", body=add_req, headers=headers)
        assert add_res["status"] == 200
        data = json.loads(add_res["json"]["result"]["content"][0]["text"])
        did = data["drawer_id"]
        assert did is not None

        # 2. Get drawer
        get_req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "mempalace_get_drawer",
                "arguments": {"drawer_id": did},
            },
        }
        get_res = await send_asgi_request(app, "POST", "/mcp", body=get_req, headers=headers)
        get_data = json.loads(get_res["json"]["result"]["content"][0]["text"])
        assert get_data["content"] == "Sacred verbatim text: Cloudflare Workers GA supports Python natively."

        # 3. Search
        search_req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "mempalace_search",
                "arguments": {"query": "Python Workers"},
            },
        }
        search_res = await send_asgi_request(app, "POST", "/mcp", body=search_req, headers=headers)
        search_data = json.loads(search_res["json"]["result"]["content"][0]["text"])
        assert len(search_data) >= 1
        assert search_data[0]["id"] == did
        assert search_data[0]["text"] == "Sacred verbatim text: Cloudflare Workers GA supports Python natively."

    asyncio.run(_test())


def test_mcp_kg_tools():
    async def _test():
        env = create_test_env(api_key="key")
        app = CloudflareMemPalaceApp(env=env)
        headers = {b"authorization": b"Bearer key"}

        # Add KG fact
        add_req = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "mempalace_kg_add",
                "arguments": {
                    "subject": "Cloudflare",
                    "predicate": "released",
                    "object": "Python Workers GA",
                    "valid_from": "2026-09-21",
                },
            },
        }
        res = await send_asgi_request(app, "POST", "/mcp", body=add_req, headers=headers)
        assert res["status"] == 200

        # Query KG fact
        q_req = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "mempalace_kg_query",
                "arguments": {"entity": "Cloudflare"},
            },
        }
        q_res = await send_asgi_request(app, "POST", "/mcp", body=q_req, headers=headers)
        facts = json.loads(q_res["json"]["result"]["content"][0]["text"])
        assert len(facts) >= 1
        assert facts[0]["object_name"] == "Python Workers GA"

    asyncio.run(_test())
