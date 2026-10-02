"""Parse and chunk Cursor agent transcripts inside the Worker.

Why this exists: clients (servers running Cursor over SSH) should not need a
local MemPalace install to have their sessions captured. They upload the raw
JSONL and the Worker turns it into drawers. The Worker bundles only
``mempalace/cloudflare/``, so the small amount of upstream logic needed here
(``normalize._try_cursor_jsonl`` and ``convo_miner.chunk_exchanges``) is copied,
the same way ID hashing and ranking are (AGENTS.md decision 7).

Deliberate differences from the local miner, all in the direction of keeping
more of the user's exact words:

* No spellcheck pass: the local miner rewrites user text, this never does.
* Tool calls and tool results are dropped. Only prose the user and the
  assistant actually wrote is filed.
* One fixed room per wing (``TRANSCRIPT_ROOM``) instead of keyword-guessed
  rooms, so a drawer id never depends on a heuristic that could change.

All functions are pure: no I/O, no clock, no bindings.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

TRANSCRIPT_ROOM = "cursor-sessions"
CHUNK_CHARS = 800  # matches convo_miner.CHUNK_SIZE so drawers look alike
MIN_CHUNK_CHARS = 30  # matches convo_miner.MIN_CHUNK_SIZE: below this is noise
MAX_WING_CHARS = 64  # Vectorize metadata values are truncated at 64 chars

_ROLES = frozenset({"user", "assistant"})
_CLAUDE_CODE_RECORD_TYPES = frozenset({"user", "assistant", "human"})
_SKIP_RECORD_TYPES = frozenset({"turn_ended"})
# Blocks Cursor injects into user messages around the real <user_query>.
_INJECTED_TAGS = (
    "system_reminder",
    "timestamp",
    "manually_attached_skills",
    "dynamic_tool_catalog",
    "hooks_context",
    "attached_files",
    "system_notification",
)
_INJECTED_TAG_RES = [re.compile(rf"<{tag}(?:\s[^>]*)?>[\s\S]*?</{tag}>") for tag in _INJECTED_TAGS]
_USER_QUERY_RE = re.compile(r"<user_query(?:\s[^>]*)?>([\s\S]*?)</user_query>")
_WING_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]*$")

Message = Tuple[str, str]


def validate_wing(wing: object) -> Optional[str]:
    """Return an error phrase when ``wing`` is not safe to use in ids, else None."""
    if not isinstance(wing, str) or not wing:
        return "wing must be a non-empty string"
    if len(wing) > MAX_WING_CHARS:
        return f"wing is longer than {MAX_WING_CHARS} characters"
    if not _WING_RE.match(wing):
        return "wing may only contain letters, digits, underscore, dot, space and hyphen"
    return None


def _strip_cursor_noise(text: str) -> str:
    """Drop Cursor-injected blocks and unwrap ``<user_query>``; inner text is verbatim."""
    for pattern in _INJECTED_TAG_RES:
        text = pattern.sub("", text)
    queries = [part.strip() for part in _USER_QUERY_RE.findall(text) if part.strip()]
    if queries:
        text = "\n".join(queries)
    return text.strip()


def _message_text(content: Any) -> str:
    """Join the prose (``text``) blocks of one message, ignoring tool blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return str(content.get("text", "")).strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(part for part in parts if part).strip()


def parse_cursor_jsonl(text: str) -> List[Message]:
    """Turn a Cursor agent transcript into ``[(role, text), ...]``.

    Returns an empty list when the input is not Cursor-shaped. Claude Code
    records use a top-level ``type`` of user/assistant/human, so seeing one
    means this is the wrong parser and nothing should be filed from it.
    """
    messages: List[Message] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        record_type = entry.get("type", "")
        if record_type in _CLAUDE_CODE_RECORD_TYPES:
            return []
        if record_type in _SKIP_RECORD_TYPES:
            continue
        role = entry.get("role")
        message = entry.get("message")
        if role not in _ROLES or not isinstance(message, dict):
            continue
        body = _message_text(message.get("content", ""))
        if body:
            body = _strip_cursor_noise(body)
        if not body:
            continue
        if role == "user":
            messages.append(("user", body))
        elif messages and messages[-1][0] == "assistant":
            previous_role, previous_body = messages[-1]
            messages[-1] = (previous_role, f"{previous_body}\n{body}")
        else:
            messages.append(("assistant", body))
    return messages


def _emit_bounded(chunks: List[str], content: str) -> None:
    """Append ``content`` in slices of at most CHUNK_CHARS, dropping only noise.

    The floor gates the whole exchange, never a slice, so a short trailing
    remainder of a long reply is kept instead of silently lost.
    """
    if len(content.strip()) <= MIN_CHUNK_CHARS:
        return
    for start in range(0, len(content), CHUNK_CHARS):
        chunks.append(content[start : start + CHUNK_CHARS])


def chunk_messages(messages: List[Message]) -> List[str]:
    """Chunk by exchange: one user turn plus the assistant reply that follows it."""
    chunks: List[str] = []
    index = 0
    while index < len(messages):
        role, body = messages[index]
        if role == "user":
            content = f"> {body}"
            if index + 1 < len(messages) and messages[index + 1][0] == "assistant":
                content = f"{content}\n{messages[index + 1][1]}"
                index += 1
            _emit_bounded(chunks, content)
        else:
            _emit_bounded(chunks, body)
        index += 1
    return chunks


def make_chunk_id(wing: str, source_file: str, chunk_index: int, content: str) -> str:
    """Stable drawer id: same transcript position and words always give the same id.

    The room is left out of the hash on purpose so the id survives a future
    change of room naming. Length-prefixing keeps field boundaries unambiguous.
    """
    key = "".join(
        f"{len(part)}:{part}" for part in (wing, source_file, str(chunk_index), content)
    ).encode("utf-8")
    return f"drawer_{wing}_{TRANSCRIPT_ROOM}_{hashlib.sha256(key).hexdigest()[:24]}"


def build_drawers(wing: str, source_file: str, transcript: str) -> List[Dict[str, Any]]:
    """Return drawer dicts (id, wing, room, content, source_file, metadata) for a transcript.

    Transcripts are append-only, so earlier exchanges keep their index and
    therefore their id on every re-upload; only new exchanges produce new ids.
    """
    chunks = chunk_messages(parse_cursor_jsonl(transcript))
    return [
        {
            "id": make_chunk_id(wing, source_file, index, content),
            "wing": wing,
            "room": TRANSCRIPT_ROOM,
            "content": content,
            "source_file": source_file,
            "metadata": {"chunk_index": index, "ingest": "cursor-transcript-upload"},
        }
        for index, content in enumerate(chunks)
    ]
