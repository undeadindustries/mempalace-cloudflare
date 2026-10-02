#!/usr/bin/env python3
"""Cursor hook: upload the agent transcript to the MemPalace Cloudflare Worker.

Why this exists: ``mempalace mine`` is a local CLI, so Cursor's stock save hook
files transcripts into whatever palace sits on that machine. A server that only
talks to the Worker through MCP never reaches the shared palace that way. This
hook needs nothing but ``python3`` (standard library only): it POSTs the raw
transcript to ``/api/transcripts`` and the Worker parses, chunks and files it.

Behaviour contract:

* Fail-open. The hook always prints ``{}`` and exits 0 so it can never block or
  slow Cursor. Every failure is written, with traceback, to the log file.
* Idempotent. The Worker skips exchanges it already holds, so uploading the
  whole file again is safe.
* Debounced with a trailing edge. At most one upload per interval per
  conversation, and a turn that arrives inside the interval still gets
  uploaded once the interval ends, so the last turn of a session is not lost.

Usage as a hook (both events send the same stdin shape):
    python3 cursor_upload_hook.py [--config FILE]
One-time setup on a machine:
    python3 cursor_upload_hook.py --install [--config FILE]

Config (first match wins): env ``MEMPALACE_CLOUDFLARE_URL`` /
``MEMPALACE_CLOUDFLARE_TOKEN`` / ``CF_ACCESS_CLIENT_ID`` /
``CF_ACCESS_CLIENT_SECRET``, then the matching ``cloudflare_*`` keys in
``--config`` or ``$MEMPALACE_CONFIG_DIR/config.json`` or
``~/.mempalace/config.json``. The Cursor server process rarely sees shell
profile exports, so the config file is the reliable place.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

USER_AGENT = "mempalace-cloudflare-cursor-hook/0.1.0"
HOOK_EVENTS = ("stop", "preCompact")
DEFAULT_INTERVAL_SECONDS = 60.0
PENDING_GRACE_SECONDS = 300.0
REQUEST_TIMEOUT_SECONDS = 120
MAX_ATTEMPTS = 3
MAX_UPLOAD_ROUNDS = 50  # 50 * 100 chunks; guards against a server that never reaches 0
MAX_TRANSCRIPT_BYTES = 25 * 1024 * 1024  # mirrors the Worker's limit
FALLBACK_CA_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt")
DEFAULT_WING = "cursor_session"
KILL_SWITCH_ENVS = ("MEMPAL_DISABLE_HOOK", "MEMPALACE_CF_UPLOAD_DISABLED")

STATE_DIR = Path(os.environ.get("MEMPAL_STATE_DIR", "~/.mempalace/hook_state")).expanduser()
LOG_PATH = STATE_DIR / "cloudflare_upload.log"


def log(message: str) -> None:
    """Append one line to the hook log; logging must never raise."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def log_exception(context: str) -> None:
    log(f"ERROR {context}\n{traceback.format_exc().rstrip()}")


def infer_wing(workspace: str) -> str:
    """Wing from the workspace folder name, same rule as hooks/cursor/lib/common.sh."""
    override = os.environ.get("MEMPAL_CF_WING", "").strip()
    if override:
        return override
    trimmed = workspace.rstrip("/\\")
    if not trimmed:
        return "root" if workspace.startswith("/") else DEFAULT_WING
    base = re.split(r"[/\\]", trimmed)[-1].lower()
    slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", base)).strip("_")
    return slug or DEFAULT_WING


def is_valid_transcript(path: str) -> bool:
    """Accept only an existing regular .json/.jsonl file."""
    if not path or not path.endswith((".jsonl", ".json")):
        return False
    return Path(path).is_file()


def load_config(config_file: Optional[str] = None) -> Dict[str, str]:
    """Resolve Worker URL and credentials from env, then the config file."""
    if config_file:
        path = Path(config_file).expanduser()
    else:
        directory = os.environ.get("MEMPALACE_CONFIG_DIR", "").strip() or "~/.mempalace"
        path = Path(directory).expanduser() / "config.json"
    stored: Dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            stored = loaded
    except (OSError, ValueError):
        pass

    def pick(env_names: List[str], key: str) -> str:
        for name in env_names:
            if os.environ.get(name):
                return os.environ[name].strip()
        return str(stored.get(key) or "").strip()

    return {
        "url": pick(["MEMPALACE_CLOUDFLARE_URL"], "cloudflare_url").rstrip("/"),
        "token": pick(["MEMPALACE_CLOUDFLARE_TOKEN", "MEMPALACE_API_KEY"], "cloudflare_token"),
        "access_id": pick(["CF_ACCESS_CLIENT_ID"], "cloudflare_access_client_id"),
        "access_secret": pick(["CF_ACCESS_CLIENT_SECRET"], "cloudflare_access_client_secret"),
    }


def config_problem(config: Dict[str, str]) -> Optional[str]:
    """Return why the config cannot be used, or None when it is complete."""
    if not config["url"] or not config["token"]:
        return "cloudflare_url and cloudflare_token are required"
    if bool(config["access_id"]) != bool(config["access_secret"]):
        return "Cloudflare Access needs both client id and client secret"
    return None


def tls_context() -> ssl.SSLContext:
    """Default TLS context, falling back to a system CA file when Python has none.

    python.org builds on macOS ship without a CA bundle, which makes every
    HTTPS call fail certificate verification.
    """
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) > 0:
        return context
    for bundle in FALLBACK_CA_BUNDLES:
        if os.path.isfile(bundle):
            return ssl.create_default_context(cafile=bundle)
    return context


def post_transcript(
    config: Dict[str, str], wing: str, source_file: str, transcript: str
) -> Dict[str, Any]:
    """POST one upload request; retry network errors and 5xx, never 4xx."""
    headers = {
        "Authorization": f"Bearer {config['token']}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if config["access_id"]:
        headers["CF-Access-Client-Id"] = config["access_id"]
        headers["CF-Access-Client-Secret"] = config["access_secret"]
    body = json.dumps({"wing": wing, "source_file": source_file, "transcript": transcript})
    request = urllib.request.Request(
        f"{config['url']}/api/transcripts",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS, context=tls_context()
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code < 500:
                raise RuntimeError(f"Worker rejected upload: HTTP {exc.code}: {detail}") from exc
            last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        if attempt < MAX_ATTEMPTS:
            time.sleep(2**attempt)
    raise RuntimeError(f"upload failed after {MAX_ATTEMPTS} attempts: {last_error}")


def upload_transcript(config: Dict[str, str], wing: str, transcript_path: str) -> Dict[str, int]:
    """Upload the file, repeating until the Worker reports nothing remaining."""
    size = os.path.getsize(transcript_path)
    if size > MAX_TRANSCRIPT_BYTES:
        raise RuntimeError(f"transcript is {size} bytes, over the {MAX_TRANSCRIPT_BYTES} limit")
    text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    totals = {"stored": 0, "already_filed": 0, "parsed_chunks": 0}
    for _ in range(MAX_UPLOAD_ROUNDS):
        result = post_transcript(config, wing, transcript_path, text)
        totals["stored"] += int(result.get("stored", 0))
        totals["already_filed"] = int(result.get("already_filed", 0))
        totals["parsed_chunks"] = int(result.get("parsed_chunks", 0))
        if int(result.get("remaining", 0)) <= 0:
            return totals
    raise RuntimeError(f"still chunks remaining after {MAX_UPLOAD_ROUNDS} rounds")


def _safe_name(conversation_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", conversation_id) or "unknown"


def claim_upload(
    state_dir: Path, conversation_id: str, now: float, interval: float
) -> Optional[float]:
    """Decide whether this invocation must start an uploader, and after what delay.

    Returns None when an uploader is already waiting: it reads the transcript
    only after its delay, so it will include this turn too. Otherwise returns
    the seconds to wait so uploads stay at least ``interval`` apart.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_name(conversation_id)
    pending = state_dir / f"{name}.cf_pending"
    last = state_dir / f"{name}.cf_last"
    try:
        if now - pending.stat().st_mtime < interval + PENDING_GRACE_SECONDS:
            return None
        pending.unlink()  # a worker died without clearing its marker
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return None
    try:
        since_last = now - last.stat().st_mtime
    except FileNotFoundError:
        return 0.0
    return max(0.0, interval - since_last)


def run_worker(conversation_id: str, transcript_path: str, wing: str, delay: float) -> None:
    """Detached uploader: wait, release the pending marker, then upload."""
    name = _safe_name(conversation_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    time.sleep(delay)
    (STATE_DIR / f"{name}.cf_last").touch()
    try:
        (STATE_DIR / f"{name}.cf_pending").unlink()
    except FileNotFoundError:
        pass
    config = load_config(os.environ.get("MEMPAL_CF_CONFIG") or None)
    problem = config_problem(config)
    if problem:
        log(f"ERROR conv={conversation_id} not uploading: {problem}")
        return
    try:
        totals = upload_transcript(config, wing, transcript_path)
        log(f"conv={conversation_id} wing={wing} uploaded {totals}")
    except Exception:
        log_exception(f"conv={conversation_id} wing={wing} upload failed for {transcript_path}")


def spawn_worker(conversation_id: str, transcript_path: str, wing: str, delay: float) -> None:
    """Start the uploader detached so the hook returns to Cursor immediately."""
    command = [
        sys.executable,
        os.path.abspath(__file__),
        "--worker",
        conversation_id,
        transcript_path,
        wing,
        str(delay),
    ]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "ab") as sink:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
            close_fds=True,
        )


def handle_hook_event(stdin_text: str, now: Optional[float] = None) -> None:
    """React to one Cursor hook event; raises nothing the caller must handle."""
    if any(os.environ.get(name) for name in KILL_SWITCH_ENVS):
        return
    data = json.loads(stdin_text)
    if not isinstance(data, dict):
        raise ValueError("hook stdin is not a JSON object")
    if int(data.get("loop_count") or 0) > 0:
        return
    transcript = str(data.get("transcript_path") or "")
    if not is_valid_transcript(transcript):
        log(f"skip: no usable transcript_path ({transcript!r})")
        return
    conversation_id = str(data.get("conversation_id") or data.get("session_id") or "unknown")
    roots = data.get("workspace_roots") or []
    workspace = str(roots[0]) if isinstance(roots, list) and roots else os.getcwd()
    interval = float(os.environ.get("MEMPAL_CF_UPLOAD_INTERVAL") or DEFAULT_INTERVAL_SECONDS)
    delay = claim_upload(STATE_DIR, conversation_id, now or time.time(), interval)
    if delay is None:
        return
    spawn_worker(conversation_id, transcript, infer_wing(workspace), delay)


def install(config_file: Optional[str]) -> Path:
    """Copy this script into ~/.mempalace/hooks/cloudflare and register it with Cursor.

    Idempotent: re-running updates the copy and never duplicates an entry. The
    previous hooks.json is kept next to it as a timestamped backup.
    """
    destination = Path("~/.mempalace/hooks/cloudflare/cursor_upload_hook.py").expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(os.path.abspath(__file__), destination)
    destination.chmod(0o755)

    command = f"python3 {destination}"
    if config_file:
        command += f" --config {Path(config_file).expanduser()}"

    hooks_path = Path("~/.cursor/hooks.json").expanduser()
    document: Dict[str, Any] = {"version": 1, "hooks": {}}
    if hooks_path.exists():
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        shutil.copyfile(hooks_path, f"{hooks_path}.bak-{int(time.time())}")
    hooks = document.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        entries[:] = [
            e for e in entries if "cursor_upload_hook.py" not in str(e.get("command", ""))
        ]
        entries.append({"command": command})
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return destination


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", help="config.json holding the cloudflare_* keys")
    parser.add_argument("--install", action="store_true", help="register as a Cursor hook")
    parser.add_argument("--worker", nargs=4, metavar=("CONV", "TRANSCRIPT", "WING", "DELAY"))
    args = parser.parse_args(argv)

    if args.config:
        os.environ["MEMPAL_CF_CONFIG"] = args.config
    if args.worker:
        conv, transcript, wing, delay = args.worker
        run_worker(conv, transcript, wing, float(delay))
        return 0
    if args.install:
        print(f"installed: {install(args.config)}")
        return 0

    try:
        handle_hook_event(sys.stdin.read())
    except Exception:
        log_exception("hook event failed")
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
