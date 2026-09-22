#!/usr/bin/env python3
"""Smoke test script for MemPalace Cloudflare Worker.

Usage:
    python scripts/test_smoke.py [--url http://localhost:8787] [--token my-token]
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def make_request(url, method="GET", data=None, token=None):
    headers = {"User-Agent": "mempalace-smoke-test"}
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
            parsed = {"raw": raw}
        return e.code, parsed


def main():
    parser = argparse.ArgumentParser(description="Smoke test for MemPalace Cloudflare Worker")
    parser.add_argument("--url", default="http://localhost:8787", help="Base URL of the Worker")
    parser.add_argument("--token", default="test-secret", help="Bearer token")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    token = args.token

    print(f"Running smoke test against: {base_url}")

    # 1. Healthz
    print("Testing /healthz (unauthenticated)...", end=" ")
    status, res = make_request(f"{base_url}/healthz")
    assert status == 200, f"Expected 200, got {status}: {res}"
    assert res.get("status") == "ok"
    print("OK")

    # 2. Auth rejection
    print("Testing auth rejection without token...", end=" ")
    status, res = make_request(f"{base_url}/api/status")
    assert status == 401, f"Expected 401, got {status}: {res}"
    print("OK")

    # 3. Authenticated Status
    print("Testing /api/status with token...", end=" ")
    status, res = make_request(f"{base_url}/api/status", token=token)
    assert status == 200, f"Expected 200, got {status}: {res}"
    print("OK")

    # 4. MCP Tools List
    print("Testing MCP tools/list...", end=" ")
    status, res = make_request(
        f"{base_url}/mcp",
        method="POST",
        data={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        token=token,
    )
    assert status == 200, f"Expected 200, got {status}: {res}"
    tools = res.get("result", {}).get("tools", [])
    assert len(tools) >= 24, f"Expected >= 24 tools, got {len(tools)}"
    print(f"OK ({len(tools)} tools discovered)")

    print("==> All smoke checks passed successfully!")


if __name__ == "__main__":
    main()
