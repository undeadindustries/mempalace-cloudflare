#!/usr/bin/env python3
"""Smoke test script for MemPalace Cloudflare Worker.

Usage:
    python scripts/test_smoke.py [--url http://localhost:8787] [--token my-token]

Secrets are read from the environment so they stay out of shell history and
the process list:
    MEMPALACE_API_KEY         Bearer token (used when --token is not given)
    CF_ACCESS_CLIENT_ID       Cloudflare Access service token, when the Worker
    CF_ACCESS_CLIENT_SECRET   is behind Access (both or neither)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_LOCAL_TOKEN = "test-secret"
ACCESS_ENV = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


def access_headers_from_env():
    """Return the Access service-token headers, or {} when Access is not in use."""
    client_id, secret = (os.environ.get(name, "") for name in ACCESS_ENV)
    if bool(client_id) != bool(secret):
        sys.exit(f"Set both {ACCESS_ENV[0]} and {ACCESS_ENV[1]}, or neither.")
    if not client_id:
        mcp_cfg = os.path.expanduser("~/.cursor/mcp.json")
        if os.path.exists(mcp_cfg):
            try:
                with open(mcp_cfg, "r") as f:
                    data = json.load(f)
                server_cfg = data.get("mcpServers", {}).get("mempalace") or data.get(
                    "mcpServers", {}
                ).get("mempalace-cloudflare", {})
                headers = server_cfg.get("headers", {})
                cid = headers.get("CF-Access-Client-Id", "")
                csec = headers.get("CF-Access-Client-Secret", "")
                if cid and csec:
                    return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}
            except Exception:
                pass
        return {}
    return {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": secret}


def make_request(url, method="GET", data=None, token=None, extra_headers=None):
    headers = {"User-Agent": "mempalace-smoke-test", **(extra_headers or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw[:200]}
        return e.code, parsed


def main():
    parser = argparse.ArgumentParser(description="Smoke test for MemPalace Cloudflare Worker")
    parser.add_argument("--url", default="http://localhost:8787", help="Base URL of the Worker")
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token (default: $MEMPALACE_API_KEY, else the local dev token)",
    )
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    token = args.token or os.environ.get("MEMPALACE_API_KEY")
    if not token and base_url.startswith("https://"):
        mcp_cfg = os.path.expanduser("~/.cursor/mcp.json")
        if os.path.exists(mcp_cfg):
            try:
                with open(mcp_cfg, "r") as f:
                    data = json.load(f)
                server_cfg = data.get("mcpServers", {}).get("mempalace") or data.get(
                    "mcpServers", {}
                ).get("mempalace-cloudflare", {})
                auth = server_cfg.get("headers", {}).get("Authorization", "")
                if auth.startswith("Bearer "):
                    token = auth[7:].strip()
            except Exception:
                pass
    token = token or DEFAULT_LOCAL_TOKEN
    access = access_headers_from_env()

    print(f"Running smoke test against: {base_url}")
    print(f"Cloudflare Access service token: {'yes' if access else 'no'}")

    if access:
        print("Testing Access rejects a request without the service token...", end=" ")
        status, res = make_request(f"{base_url}/api/status", token=token)
        assert status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN), f"Expected 401/403, got {status}"
        print(f"OK ({status})")

    print("Testing /healthz (no bearer token)...", end=" ")
    status, res = make_request(f"{base_url}/healthz", extra_headers=access)
    assert status == 200, f"Expected 200, got {status}: {res}"
    assert res.get("status") == "ok"
    print("OK")

    print("Testing auth rejection without bearer token...", end=" ")
    status, res = make_request(f"{base_url}/api/status", extra_headers=access)
    assert status == HTTP_UNAUTHORIZED, f"Expected 401, got {status}: {res}"
    print("OK")

    print("Testing /api/status with token...", end=" ")
    status, res = make_request(f"{base_url}/api/status", token=token, extra_headers=access)
    assert status == 200, f"Expected 200, got {status}: {res}"
    print("OK")

    print("Testing MCP tools/list...", end=" ")
    status, res = make_request(
        f"{base_url}/mcp",
        method="POST",
        data={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        token=token,
        extra_headers=access,
    )
    assert status == 200, f"Expected 200, got {status}: {res}"
    tools = res.get("result", {}).get("tools", [])
    assert len(tools) >= 24, f"Expected >= 24 tools, got {len(tools)}"
    hinted = [t["name"] for t in tools if t.get("readOnlyHint")]
    assert len(hinted) == 14, f"Expected 14 readOnlyHint tools, got {len(hinted)}"
    assert "mempalace_memories_filed_away" not in hinted, (
        "mempalace_memories_filed_away unlinks checkpoint and must not declare readOnlyHint"
    )
    search_desc = next(t["description"] for t in tools if t["name"] == "mempalace_search")
    assert "past-session memories" in search_desc
    assert "keywords only" in search_desc
    assert len(search_desc) <= 200
    print(f"OK ({len(tools)} tools discovered, {len(hinted)} read-only hinted)")

    print("==> All smoke checks passed successfully!")


if __name__ == "__main__":
    main()
