"""Copy local palace drawers into the Cloudflare Worker.

Reads Chroma read-only, embeds with local bge-small ONNX, and POSTs batches
to /api/drawers/batch with the vectors attached so Workers AI is not called.
Progress is recorded under ~/.mempalace so a stopped run can resume.

Closets are not drawers. This script imports mempalace_drawers only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from bge_small_onnx import BgeSmallOnnx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.mempalace_cloudflare_remote import (  # noqa: E402
    USER_AGENT,
    resolve_access_headers,
    resolve_setting,
)

logger = logging.getLogger("import_local_palace")

READ_PAGE = 100
DEFAULT_UPLOAD_BATCH = 20
HTTP_TIMEOUT_SECONDS = 120
MAX_ATTEMPTS = 6
BACKOFF_SECONDS = 2
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_PROGRESS = Path(os.path.expanduser("~/.mempalace/cloudflare-import-progress.json"))


def _palace_collection(palace_path: str):
    import mempalace.palace as palace

    return palace.get_collection(palace_path)


def _load_progress(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"offset": 0, "stored": 0}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"progress file is not an object: {path}")
    return data


def _save_progress(path: Path, progress: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(progress, handle)
    tmp.replace(path)


def _worker_settings() -> tuple[str, dict[str, str]]:
    base = resolve_setting(
        key="url",
        env_vars=["MEMPALACE_CLOUDFLARE_URL"],
        config_key="cloudflare_url",
    ).rstrip("/")
    token = resolve_setting(
        key="token",
        env_vars=["MEMPALACE_API_KEY"],
        config_key="cloudflare_token",
    )
    if not base or not token:
        raise SystemExit("cloudflare_url and cloudflare_token are required")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **resolve_access_headers({}),
    }
    return base, headers


def _post_batch(
    base: str, headers: dict[str, str], drawers: list[dict[str, Any]]
) -> dict[str, Any]:
    """POST one batch, retrying transient failures.

    Upserts are idempotent, so resending a batch that half-landed is safe.
    Error 1102 (Worker CPU limit) and 429/5xx are retried with backoff; any
    other status stops the run so progress is not advanced past it.
    """
    body = json.dumps({"drawers": drawers}).encode("utf-8")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            f"{base}/api/drawers/batch", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"batch upload HTTP {exc.code}: {detail}") from exc
            logger.warning(
                "HTTP %s (%s), retry %s/%s", exc.code, detail[:80], attempt, MAX_ATTEMPTS
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"batch upload failed: {exc}") from exc
            logger.warning("network error %s, retry %s/%s", exc, attempt, MAX_ATTEMPTS)
        time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _row(
    drawer_id: str, document: str, metadata: dict[str, Any], embedding: list[float]
) -> dict[str, Any]:
    meta = dict(metadata or {})
    return {
        "id": drawer_id,
        "wing": str(meta.get("wing") or "general"),
        "room": str(meta.get("room") or "inbox"),
        "content": document,
        "source_file": meta.get("source_file"),
        "metadata": meta,
        "embedding": embedding,
    }


def import_drawers(
    *,
    palace_path: str,
    limit: int | None,
    progress_path: Path,
    resume: bool,
    upload_batch: int = DEFAULT_UPLOAD_BATCH,
) -> dict[str, Any]:
    """Embed and upload drawers. Returns the progress object."""
    base, headers = _worker_settings()
    progress = _load_progress(progress_path) if resume else {"offset": 0, "stored": 0}
    offset = int(progress.get("offset") or 0)
    stored = int(progress.get("stored") or 0)
    collection = _palace_collection(palace_path)
    embedder = BgeSmallOnnx()
    remaining = limit
    while remaining is None or remaining > 0:
        page = READ_PAGE if remaining is None else min(READ_PAGE, remaining)
        batch = collection.get(limit=page, offset=offset, include=["documents", "metadatas"])
        ids = batch["ids"]
        if not ids:
            break
        vectors = embedder.embed(list(batch["documents"]))
        drawers = [
            _row(did, doc, meta, vec)
            for did, doc, meta, vec in zip(ids, batch["documents"], batch["metadatas"], vectors)
        ]
        for start in range(0, len(drawers), upload_batch):
            chunk = drawers[start : start + upload_batch]
            result = _post_batch(base, headers, chunk)
            if result.get("embedded_by") != "client":
                raise RuntimeError(f"worker embedded the batch itself: {result}")
            stored += int(result.get("stored") or 0)
        offset += len(ids)
        if remaining is not None:
            remaining -= len(ids)
        progress = {"offset": offset, "stored": stored}
        _save_progress(progress_path, progress)
        logger.info("stored=%s offset=%s", stored, offset)
    return progress


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", default=os.path.expanduser("~/.mempalace/palace"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--progress", type=Path, default=DEFAULT_PROGRESS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_UPLOAD_BATCH,
        help="drawers per request; the free plan's 10 ms CPU limit needs small batches",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    if not 1 <= args.batch <= 100:
        raise SystemExit("--batch must be between 1 and 100")
    result = import_drawers(
        palace_path=args.palace,
        limit=args.limit,
        progress_path=args.progress,
        resume=args.resume,
        upload_batch=args.batch,
    )
    logger.info("done %s", result)


if __name__ == "__main__":
    main()
