#!/usr/bin/env python3
"""Backfill script for D1 FTS5 virtual table.

Usage:
    uv run python scripts/backfill_d1_fts.py --url <worker_url> [--batch 100] [--workers 4]

Secrets are read from the environment or ~/.cursor/mcp.json:
    MEMPALACE_API_KEY         Bearer token (used when --token is not given)
    CF_ACCESS_CLIENT_ID       Cloudflare Access service token, when the Worker
    CF_ACCESS_CLIENT_SECRET   is behind Access (both or neither)
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

ACCESS_ENV = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
USER_AGENT = "mempalace-backfill-fts"
MAX_RETRIES = 8
RETRY_DELAY_SEC = 3.0


def access_headers_from_env() -> Dict[str, str]:
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
                headers = (
                    data.get("mcpServers", {}).get("mempalace-cloudflare", {}).get("headers", {})
                )
                cid = headers.get("CF-Access-Client-Id", "")
                csec = headers.get("CF-Access-Client-Secret", "")
                if cid and csec:
                    return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}
            except Exception:
                pass
        return {}
    return {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": secret}


def post_backfill_batch(
    url: str,
    token: str,
    limit: int,
    cursor: Optional[str] = None,
    end_cursor: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Post a backfill batch request with retries."""
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }
    payload: Dict[str, Any] = {"limit": limit}
    if cursor:
        payload["cursor"] = cursor
    if end_cursor:
        payload["end_cursor"] = end_cursor
    data = json.dumps(payload).encode("utf-8")

    endpoint = f"{url.rstrip('/')}/api/backfill/fts"
    ssl_context = None
    try:
        import certifi
        import ssl

        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass

    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60, context=ssl_context) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                err_data = json.loads(raw)
            except Exception:
                err_data = {"raw": raw[:300]}
            if e.code in (500, 502, 503, 504, 429) and attempt < MAX_RETRIES:
                sleep_time = RETRY_DELAY_SEC * (2 ** (attempt - 1))
                print(
                    f"HTTP {e.code} on attempt {attempt}/{MAX_RETRIES}, retrying in {sleep_time:.1f}s...",
                    flush=True,
                )
                time.sleep(sleep_time)
                continue
            return e.code, err_data
        except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
            if attempt < MAX_RETRIES:
                sleep_time = RETRY_DELAY_SEC * (2 ** (attempt - 1))
                print(
                    f"Connection error ({e}) on attempt {attempt}/{MAX_RETRIES}, retrying in {sleep_time:.1f}s...",
                    flush=True,
                )
                time.sleep(sleep_time)
                continue
            raise
    return 500, {"error": "Max retries exceeded"}


def get_partition_boundaries(num_partitions: int) -> List[Optional[str]]:
    """Fetch partition boundaries from D1 via wrangler."""
    if num_partitions <= 1:
        return [None, None]

    try:
        # Check total count
        out = subprocess.check_output(
            [
                "npx",
                "wrangler",
                "d1",
                "execute",
                "mempalace-kg",
                "--remote",
                "--command=SELECT count(*) as total FROM drawers",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        data = json.loads(out[out.find("[") :])
        total = data[0]["results"][0]["total"]

        chunk_size = total // num_partitions
        selects = []
        for i in range(1, num_partitions):
            offset = i * chunk_size
            selects.append(
                f"(SELECT id FROM drawers ORDER BY id ASC LIMIT 1 OFFSET {offset}) as b{i}"
            )
        sql = f"SELECT {', '.join(selects)}"

        out = subprocess.check_output(
            ["npx", "wrangler", "d1", "execute", "mempalace-kg", "--remote", f"--command={sql}"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        data = json.loads(out[out.find("[") :])
        row = data[0]["results"][0]
        boundaries: List[Optional[str]] = [None]
        for i in range(1, num_partitions):
            boundaries.append(row[f"b{i}"])
        boundaries.append(None)
        return boundaries
    except Exception as e:
        print(
            f"Could not compute partition boundaries ({e}), falling back to 1 worker.", flush=True
        )
        return [None, None]


def run_worker_partition(
    worker_id: int,
    url: str,
    token: str,
    batch_size: int,
    start_cursor: Optional[str],
    end_cursor: Optional[str],
    access_headers: Dict[str, str],
    checkpoint_file: str,
) -> int:
    cursor = start_cursor
    if not cursor and os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, "r") as f:
                saved = f.read().strip()
                if saved:
                    cursor = saved
                    print(f"[Worker {worker_id}] Resuming from checkpoint: {cursor}", flush=True)
        except Exception:
            pass

    indexed = 0
    t0 = time.time()
    while True:
        status, resp = post_backfill_batch(
            url=url,
            token=token,
            limit=batch_size,
            cursor=cursor,
            end_cursor=end_cursor,
            extra_headers=access_headers,
        )
        if status != 200:
            print(
                f"[Worker {worker_id}] ERROR: Server returned {status}: {resp}",
                file=sys.stderr,
                flush=True,
            )
            return indexed

        processed = resp.get("processed", 0)
        next_cursor = resp.get("next_cursor")
        done = resp.get("done", False)

        indexed += processed
        if next_cursor:
            cursor = next_cursor
            try:
                with open(checkpoint_file, "w") as f:
                    f.write(cursor)
            except Exception:
                pass

        if indexed % 1000 == 0 or done or not next_cursor:
            dt = time.time() - t0
            rate = indexed / dt if dt > 0 else 0
            print(
                f"[Worker {worker_id}] Indexed {indexed} drawers ({rate:.1f} d/s). Next cursor: {next_cursor or 'NONE'}",
                flush=True,
            )

        if done or not next_cursor:
            print(f"[Worker {worker_id}] Partition complete! Total indexed: {indexed}", flush=True)
            if os.path.exists(checkpoint_file):
                try:
                    os.remove(checkpoint_file)
                except Exception:
                    pass
            break

    return indexed


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill MemPalace D1 FTS5 virtual table")
    parser.add_argument("--url", required=True, help="Base URL of the Cloudflare Worker")
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token (default: $MEMPALACE_API_KEY)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=100,
        help="Batch size per request (default: 100, max: 200)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of concurrent worker threads (default: 4)",
    )
    args = parser.parse_args()

    token = args.token or os.environ.get("MEMPALACE_API_KEY")
    if not token:
        mcp_cfg = os.path.expanduser("~/.cursor/mcp.json")
        if os.path.exists(mcp_cfg):
            try:
                with open(mcp_cfg, "r") as f:
                    data = json.load(f)
                auth = (
                    data.get("mcpServers", {})
                    .get("mempalace-cloudflare", {})
                    .get("headers", {})
                    .get("Authorization", "")
                )
                if auth.startswith("Bearer "):
                    token = auth[7:].strip()
            except Exception:
                pass

    if not token:
        sys.exit("MEMPALACE_API_KEY is required via --token or environment variable.")

    access_headers = access_headers_from_env()
    batch_size = max(1, min(args.batch, 200))

    # Boundary definitions must always match the original 4 partitions
    # when existing partition checkpoints are present.
    original_partitions = 4
    boundaries = get_partition_boundaries(original_partitions)
    num_partitions = len(boundaries) - 1
    existing_ckpts = [
        os.path.exists(f"scripts/.fts_backfill_cursor_{w}") for w in range(num_partitions)
    ]

    active_partitions = [w for w in range(num_partitions) if existing_ckpts[w]]
    if not active_partitions:
        # Fresh start across all partitions
        active_partitions = list(range(num_partitions))

    max_concurrency = min(len(active_partitions), max(1, args.workers))
    print(
        f"Starting FTS5 backfill across {len(active_partitions)} partitions with concurrency {max_concurrency} (batch size: {batch_size})",
        flush=True,
    )

    t0 = time.time()
    total_indexed = 0

    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = []
        for w in active_partitions:
            start_cur = boundaries[w] if not existing_ckpts[w] else None
            end_cur = boundaries[w + 1]
            ckpt = f"scripts/.fts_backfill_cursor_{w}"
            futures.append(
                executor.submit(
                    run_worker_partition,
                    w,
                    args.url,
                    token,
                    batch_size,
                    start_cur,
                    end_cur,
                    access_headers,
                    ckpt,
                )
            )

        for f in as_completed(futures):
            total_indexed += f.result()

    elapsed = time.time() - t0
    overall_rate = total_indexed / elapsed if elapsed > 0 else 0
    print(
        f"\n========================================\n"
        f"Backfill finished in {elapsed:.1f}s!\n"
        f"Total drawers indexed: {total_indexed}\n"
        f"Overall rate: {overall_rate:.1f} drawers/s\n"
        f"========================================",
        flush=True,
    )


if __name__ == "__main__":
    main()
