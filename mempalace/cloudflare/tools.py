"""Cloudflare Worker MCP tools implementation (24 core memory tools).

Exposes core MemPalace memory, drawer, hybrid search, taxonomy, and knowledge graph
tools over the Model Context Protocol (MCP) Streamable HTTP transport.
"""

from datetime import datetime
import hashlib
import math
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from ._shims import install_shims
except (ImportError, ValueError):
    from _shims import install_shims  # type: ignore

install_shims()


def make_drawer_id_from_content(wing: str, room: str, content: str) -> str:
    """Drawer ID matching upstream ids.make_drawer_id_from_content contract."""
    key = "".join(f"{len(part)}:{part}" for part in map(str, (wing, room, content))).encode()
    hash24 = hashlib.sha256(key).hexdigest()[:24]
    return f"drawer_{wing}_{room}_{hash24}"


AAAK_SPEC = """AAAK is a compressed memory dialect that MemPalace uses for efficient storage.
It is designed to be readable by both humans and LLMs without decoding.

FORMAT:
  ENTITIES: 3-letter uppercase codes. ALC=Alice, JOR=Jordan, RIL=Riley, MAX=Max, BEN=Ben.
  EMOTIONS: *action markers* before/during text. *warm*=joy, *fierce*=determined, *raw*=vulnerable, *bloom*=tenderness.
  STRUCTURE: Pipe-separated fields. FAM: family | PROJ: projects | ⚠: warnings/reminders.
  DATES: ISO format (2026-03-31). COUNTS: Nx = N mentions (e.g., 570x).
  IMPORTANCE: ★ to ★★★★★ (1-5 scale).
  HALLS: hall_facts, hall_events, hall_discoveries, hall_preferences, hall_advice.
  WINGS: wing_user, wing_agent, wing_team, wing_code, wing_myproject, wing_hardware, wing_ue5, wing_ai_research.
  ROOMS: Hyphenated slugs representing named ideas (e.g., chromadb-setup, gpu-pricing).

EXAMPLE:
  FAM: ALC→♡JOR | 2D(kids): RIL(18,sports) MAX(11,chess+swimming) | BEN(contributor)

Read AAAK naturally — expand codes mentally, treat *markers* as emotional context.
When WRITING AAAK: use entity codes, mark emotions, keep structure tight."""

MAX_PAGE_LIMIT = 100
MAX_BATCH_DRAWERS = 100
# Each stored drawer costs R2, D1 and FTS subrequests, and Workers cap a single
# invocation at 1000 on the Paid plan. 100 drawers stays far below that; callers
# resume from ``remaining`` instead of one request trying to file everything.
MAX_NEW_CHUNKS_PER_REQUEST = 100
# D1 allows 100 bound parameters per statement, so existence checks go in pages.
D1_MAX_BOUND_PARAMS = 100
MAX_TRANSCRIPT_BYTES = 25 * 1024 * 1024
HTTP_BAD_REQUEST = 400
HTTP_PAYLOAD_TOO_LARGE = 413

try:
    from .transcripts import build_drawers, validate_wing
    from .workers_ai import EMBEDDING_DIMENSION
except (ImportError, ValueError):
    from transcripts import build_drawers, validate_wing  # type: ignore
    from workers_ai import EMBEDDING_DIMENSION  # type: ignore


def prepare_drawer_batch(
    drawers: object,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Validate a batch before any R2, D1, or Vectorize write.

    Returns the normalized rows and an error message. An error means nothing
    was safe to store.
    """
    if not isinstance(drawers, list) or not drawers:
        return [], "drawers must be a non-empty list"
    if len(drawers) > MAX_BATCH_DRAWERS:
        return [], f"drawers exceeds {MAX_BATCH_DRAWERS}"

    prepared: List[Dict[str, Any]] = []
    saw_embedding = False
    saw_missing = False
    for index, item in enumerate(drawers):
        if not isinstance(item, dict):
            return [], f"drawers[{index}] must be an object"
        content = item.get("content")
        if not isinstance(content, str) or content == "":
            return [], f"drawers[{index}].content must be a non-empty string"
        wing = str(item.get("wing") or "general")
        room = str(item.get("room") or "inbox")
        drawer_id = item.get("drawer_id") or item.get("id")
        if drawer_id is not None and not isinstance(drawer_id, str):
            return [], f"drawers[{index}].id must be a string"
        did = drawer_id or make_drawer_id_from_content(wing, room, content)
        meta_in = item.get("metadata") or {}
        if not isinstance(meta_in, dict):
            return [], f"drawers[{index}].metadata must be an object"
        meta = dict(meta_in)
        meta.pop("embedding", None)
        meta["wing"] = wing
        meta["room"] = room
        source = item.get("source_file", meta.get("source_file"))
        if isinstance(source, str) and source:
            meta["source_file"] = source
        if "authored_at" not in meta:
            meta["authored_at"] = datetime.now().isoformat()

        embedding = item.get("embedding")
        parsed: Optional[List[float]]
        if embedding is None:
            saw_missing = True
            parsed = None
        else:
            parsed_or_error = _parse_embedding(embedding)
            if isinstance(parsed_or_error, str):
                return [], f"drawers[{index}].embedding {parsed_or_error}"
            parsed = parsed_or_error
            saw_embedding = True
        prepared.append({"id": did, "content": content, "metadata": meta, "embedding": parsed})

    if saw_embedding and saw_missing:
        return [], "either every drawer has an embedding, or none do"
    return prepared, None


def _parse_embedding(value: object) -> Union[List[float], str]:
    """Return a 384-float vector, or an error phrase."""
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSION:
        return f"must be {EMBEDDING_DIMENSION} floats"
    out: List[float] = []
    for number in value:
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return f"must be {EMBEDDING_DIMENSION} floats"
        as_float = float(number)
        if not math.isfinite(as_float):
            return "must be finite"
        out.append(as_float)
    return out


class CloudflarePalaceTools:
    """Dispatches MCP tool calls to Cloudflare D1, R2, Vectorize, and Workers AI."""

    def __init__(
        self,
        collection: Any,
        d1_kg: Any,
        d1_registry: Any,
        r2_storage: Any,
    ):
        self.col = collection
        self.kg = d1_kg
        self.reg = d1_registry
        self.r2 = r2_storage

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Return MCP tool definitions list."""
        definitions = []
        for name, spec in self._tools_registry().items():
            defn: Dict[str, Any] = {
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["input_schema"],
            }
            if spec.get("read_only"):
                defn["readOnlyHint"] = True
            definitions.append(defn)
        return definitions

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Execute a tool by name with arguments."""
        reg = self._tools_registry()
        if name not in reg:
            return {"error": f"Unknown tool: {name}"}
        handler = reg[name]["handler"]
        args = arguments or {}
        try:
            return await handler(**args)
        except Exception as e:
            return {"error": f"Tool execution failed: {str(e)}"}

    def _tools_registry(self) -> Dict[str, Dict[str, Any]]:
        return {
            "mempalace_status": {
                "description": "Palace overview — total drawers, wing and room counts",
                "input_schema": {"type": "object", "properties": {}},
                "handler": self.tool_status,
                "read_only": True,
            },
            "mempalace_list_wings": {
                "description": "List all wings with drawer counts",
                "input_schema": {"type": "object", "properties": {}},
                "handler": self.tool_list_wings,
                "read_only": True,
            },
            "mempalace_list_rooms": {
                "description": "List rooms within a wing (or all rooms if no wing given)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "wing": {
                            "type": "string",
                            "description": "Wing to list rooms for (optional)",
                        },
                    },
                },
                "handler": self.tool_list_rooms,
                "read_only": True,
            },
            "mempalace_get_taxonomy": {
                "description": "Full taxonomy: wing → room → drawer count",
                "input_schema": {"type": "object", "properties": {}},
                "handler": self.tool_get_taxonomy,
                "read_only": True,
            },
            "mempalace_get_aaak_spec": {
                "description": "Get the AAAK dialect specification — the compressed memory format MemPalace uses.",
                "input_schema": {"type": "object", "properties": {}},
                "handler": self.tool_get_aaak_spec,
                "read_only": True,
            },
            "mempalace_search": {
                "description": (
                    "Search past-session memories. Returns matching drawers. "
                    "Not for the current conversation — if it happened in this session, "
                    "answer from context instead."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "wing": {"type": "string", "description": "Filter by wing (optional)"},
                        "room": {"type": "string", "description": "Filter by room (optional)"},
                        "max_results": {
                            "type": "integer",
                            "description": "Max results to return (default: 5)",
                        },
                    },
                    "required": ["query"],
                },
                "handler": self.tool_search,
                "read_only": True,
            },
            "mempalace_check_duplicate": {
                "description": "Check if similar content already exists before saving to avoid duplicate drawers.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Content to check for duplicates",
                        },
                        "wing": {
                            "type": "string",
                            "description": "Wing to check within (optional)",
                        },
                        "room": {
                            "type": "string",
                            "description": "Room to check within (optional)",
                        },
                    },
                    "required": ["content"],
                },
                "handler": self.tool_check_duplicate,
                "read_only": True,
            },
            "mempalace_add_drawer": {
                "description": "Add a new memory drawer. Saves verbatim text to the specified wing and room.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "wing": {"type": "string", "description": "Wing (person/project/topic)"},
                        "room": {"type": "string", "description": "Room (subtopic/context)"},
                        "content": {"type": "string", "description": "Verbatim text to store"},
                        "source_file": {
                            "type": "string",
                            "description": "Optional source reference",
                        },
                        "drawer_id": {
                            "type": "string",
                            "description": "Optional explicit drawer ID",
                        },
                    },
                    "required": ["wing", "room", "content"],
                },
                "handler": self.tool_add_drawer,
            },
            "mempalace_checkpoint": {
                "description": "Save multiple related facts/memories together in a single operation.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "drawers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "wing": {"type": "string"},
                                    "room": {"type": "string"},
                                    "content": {"type": "string"},
                                    "source_file": {"type": "string"},
                                },
                                "required": ["wing", "room", "content"],
                            },
                        },
                    },
                    "required": ["drawers"],
                },
                "handler": self.tool_checkpoint,
            },
            "mempalace_get_drawer": {
                "description": "Retrieve the full verbatim text of a drawer by its ID.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "drawer_id": {
                            "type": "string",
                            "description": "ID of the drawer to retrieve",
                        },
                    },
                    "required": ["drawer_id"],
                },
                "handler": self.tool_get_drawer,
                "read_only": True,
            },
            "mempalace_get_drawers": {
                "description": "Retrieve full verbatim text of multiple drawers by IDs.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "drawer_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["drawer_ids"],
                },
                "handler": self.tool_get_drawers,
                "read_only": True,
            },
            "mempalace_list_drawers": {
                "description": "List drawers in a room or wing with pagination.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "wing": {"type": "string", "description": "Filter by wing (optional)"},
                        "room": {"type": "string", "description": "Filter by room (optional)"},
                        "limit": {"type": "integer", "description": "Max to return (default: 50)"},
                        "offset": {
                            "type": "integer",
                            "description": "Pagination offset (default: 0)",
                        },
                        "include_content": {
                            "type": "boolean",
                            "description": "Hydrate verbatim content from R2 (default: false)",
                        },
                    },
                },
                "handler": self.tool_list_drawers,
                "read_only": True,
            },
            "mempalace_update_drawer": {
                "description": "Update the content, wing, or room of an existing drawer.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "drawer_id": {
                            "type": "string",
                            "description": "ID of the drawer to update",
                        },
                        "content": {"type": "string", "description": "New verbatim content"},
                        "wing": {"type": "string", "description": "New wing (optional)"},
                        "room": {"type": "string", "description": "New room (optional)"},
                    },
                    "required": ["drawer_id", "content"],
                },
                "handler": self.tool_update_drawer,
            },
            "mempalace_delete_drawer": {
                "description": "Delete a single memory drawer by its ID.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "drawer_id": {
                            "type": "string",
                            "description": "ID of the drawer to delete",
                        },
                    },
                    "required": ["drawer_id"],
                },
                "handler": self.tool_delete_drawer,
            },
            "mempalace_delete_drawers": {
                "description": "Delete multiple memory drawers by their IDs.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "drawer_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["drawer_ids"],
                },
                "handler": self.tool_delete_drawers,
            },
            "mempalace_delete_by_source": {
                "description": "Delete all drawers that came from a specific source file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source_file": {"type": "string", "description": "Source file path"},
                    },
                    "required": ["source_file"],
                },
                "handler": self.tool_delete_by_source,
            },
            "mempalace_diary_write": {
                "description": "Write an agent diary entry recording thoughts, state, or decisions.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Diary entry content"},
                        "day": {
                            "type": "string",
                            "description": "Day in YYYY-MM-DD format (optional)",
                        },
                    },
                    "required": ["content"],
                },
                "handler": self.tool_diary_write,
            },
            "mempalace_diary_read": {
                "description": "Read recent diary entries.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Number of recent days (default: 7)",
                        },
                    },
                },
                "handler": self.tool_diary_read,
                "read_only": True,
            },
            "mempalace_memories_filed_away": {
                "description": "Check what memories have been filed recently.",
                "input_schema": {"type": "object", "properties": {}},
                "handler": self.tool_memories_filed_away,
                "read_only": True,
            },
            "mempalace_kg_query": {
                "description": "Query the knowledge graph for an entity's relationships.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string", "description": "Entity to query"},
                        "as_of": {
                            "type": "string",
                            "description": "Point-in-time filter (YYYY-MM-DD)",
                        },
                        "direction": {
                            "type": "string",
                            "description": "outgoing, incoming, or both (default: both)",
                        },
                    },
                    "required": ["entity"],
                },
                "handler": self.tool_kg_query,
                "read_only": True,
            },
            "mempalace_kg_add": {
                "description": "Add a fact triple to the knowledge graph (subject → predicate → object).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "valid_from": {"type": "string"},
                        "valid_to": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["subject", "predicate", "object"],
                },
                "handler": self.tool_kg_add,
            },
            "mempalace_kg_invalidate": {
                "description": "Mark an existing knowledge graph fact as ended.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "ended": {"type": "string"},
                    },
                    "required": ["subject", "predicate", "object"],
                },
                "handler": self.tool_kg_invalidate,
            },
            "mempalace_kg_supersede": {
                "description": "Replace an old fact with a new one at a boundary date.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "old_subject": {"type": "string"},
                        "old_predicate": {"type": "string"},
                        "old_object": {"type": "string"},
                        "new_subject": {"type": "string"},
                        "new_predicate": {"type": "string"},
                        "new_object": {"type": "string"},
                        "boundary": {"type": "string"},
                    },
                    "required": [
                        "old_subject",
                        "old_predicate",
                        "old_object",
                        "new_subject",
                        "new_predicate",
                        "new_object",
                    ],
                },
                "handler": self.tool_kg_supersede,
            },
            "mempalace_kg_timeline": {
                "description": "View chronological knowledge graph timeline.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                },
                "handler": self.tool_kg_timeline,
                "read_only": True,
            },
            "mempalace_kg_stats": {
                "description": "Knowledge graph summary statistics.",
                "input_schema": {"type": "object", "properties": {}},
                "handler": self.tool_kg_stats,
                "read_only": True,
            },
        }

    # ── Handlers ──────────────────────────────────────────────────────────

    async def tool_status(self) -> Dict[str, Any]:
        count = await self.reg.count()
        wings = await self.reg.list_wings()
        rooms = await self.reg.list_rooms()
        return {
            "total_drawers": count,
            "total_wings": len(wings),
            "total_rooms": len(rooms),
            "backend": "cloudflare-vectorize-d1-r2",
        }

    async def tool_list_wings(self) -> List[Dict[str, Any]]:
        return await self.reg.list_wings()

    async def tool_list_rooms(self, wing: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self.reg.list_rooms(wing=wing)

    async def tool_get_taxonomy(self) -> Dict[str, Any]:
        tax = await self.reg.get_taxonomy()
        total = await self.reg.count()
        return {"taxonomy": tax, "total_drawers": total}

    async def tool_get_aaak_spec(self) -> Dict[str, Any]:
        return {"aaak_spec": AAAK_SPEC}

    async def tool_search(
        self,
        query: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        try:
            from .search import execute_hybrid_search
        except (ImportError, ValueError):
            from search import execute_hybrid_search  # type: ignore

        where: Dict[str, Any] = {}
        if wing:
            where["wing"] = wing
        if room:
            where["room"] = room

        clamped_max = max(1, min(int(max_results), MAX_PAGE_LIMIT))

        return await execute_hybrid_search(
            collection=self.col,
            query=query,
            n_results=clamped_max,
            where=where if where else None,
        )

    async def tool_check_duplicate(
        self,
        content: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
    ) -> Dict[str, Any]:
        chash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = await self.reg.check_duplicate(chash, wing=wing, room=room)
        if existing:
            return {"is_duplicate": True, "exact_match": True, "existing_drawer_id": existing}

        # Check semantic similarity using search
        try:
            from .search import execute_hybrid_search
        except (ImportError, ValueError):
            from search import execute_hybrid_search  # type: ignore

        where: Dict[str, Any] = {}
        if wing:
            where["wing"] = wing
        if room:
            where["room"] = room

        hits = await execute_hybrid_search(
            self.col,
            query=content[:500],
            n_results=1,
            where=where if where else None,
        )
        if hits and hits[0].get("score", 0.0) >= 0.95:
            return {"is_duplicate": True, "exact_match": False, "existing_drawer_id": hits[0]["id"]}

        return {"is_duplicate": False}

    async def tool_add_drawer(
        self,
        wing: str,
        room: str,
        content: str,
        source_file: Optional[str] = None,
        drawer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        did = drawer_id or make_drawer_id_from_content(wing, room, content)
        meta = {
            "wing": wing,
            "room": room,
            "authored_at": datetime.now().isoformat(),
        }
        if source_file:
            meta["source_file"] = source_file

        await self.col.a_upsert(
            documents=[content],
            ids=[did],
            metadatas=[meta],
        )
        return {"drawer_id": did, "wing": wing, "room": room, "status": "stored"}

    async def tool_add_drawers(self, drawers: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Store a batch of drawers in one upsert.

        Precomputed ``embedding`` vectors skip Workers AI. That is how a local
        palace can be imported without spending the free-tier neuron quota.
        """
        prepared, error = prepare_drawer_batch(drawers)
        if error:
            return {"error": error}

        documents = [item["content"] for item in prepared]
        ids = [item["id"] for item in prepared]
        metadatas = [item["metadata"] for item in prepared]
        embeddings = [item["embedding"] for item in prepared] if prepared[0]["embedding"] else None
        await self.col.a_upsert(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        return {
            "stored": len(ids),
            "drawer_ids": ids,
            "embedded_by": "client" if embeddings is not None else "workers_ai",
        }

    async def tool_ingest_transcript(
        self, wing: object, source_file: object, transcript: object
    ) -> Dict[str, Any]:
        """File a Cursor transcript as verbatim drawers, skipping what is already filed.

        Why it works this way: the uploading hook cannot know which exchanges
        the palace already holds, and transcripts only ever grow. Ids are
        derived from position and content, so re-uploading the whole file is
        safe and only new exchanges cost embeddings. The per-request cap keeps
        one call inside the Worker subrequest limit; ``remaining`` tells the
        caller to repeat the identical request until it reaches 0.

        Failure model: R2, D1 and Vectorize writes happen inside one
        ``tool_add_drawers`` call. If it raises, nothing is reported as stored
        and the next identical upload retries the same chunks. A crash after
        the D1 row but before the vector leaves the drawer findable by exact
        text (FTS) but not by meaning; ``add_drawer`` has the same window.
        """
        problem = validate_wing(wing)
        if problem:
            return {"error": problem, "http_status": HTTP_BAD_REQUEST}
        if not isinstance(source_file, str) or not source_file:
            return {
                "error": "source_file must be a non-empty string",
                "http_status": HTTP_BAD_REQUEST,
            }
        if not isinstance(transcript, str) or not transcript:
            return {
                "error": "transcript must be a non-empty string",
                "http_status": HTTP_BAD_REQUEST,
            }
        if len(transcript.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
            return {
                "error": f"transcript exceeds {MAX_TRANSCRIPT_BYTES} bytes",
                "http_status": HTTP_PAYLOAD_TOO_LARGE,
            }

        drawers = build_drawers(str(wing), source_file, transcript)
        filed_ids = await self._existing_drawer_ids([d["id"] for d in drawers])
        pending = [d for d in drawers if d["id"] not in filed_ids]
        batch = pending[:MAX_NEW_CHUNKS_PER_REQUEST]

        stored_ids: List[str] = []
        if batch:
            result = await self.tool_add_drawers(batch)
            if "error" in result:
                return result
            stored_ids = result["drawer_ids"]
        return {
            "parsed_chunks": len(drawers),
            "already_filed": len(drawers) - len(pending),
            "stored": len(stored_ids),
            "remaining": len(pending) - len(batch),
            "drawer_ids": stored_ids,
        }

    async def _existing_drawer_ids(self, drawer_ids: List[str]) -> set:
        """Return which of ``drawer_ids`` are already in the registry."""
        found: set = set()
        for start in range(0, len(drawer_ids), D1_MAX_BOUND_PARAMS):
            rows = await self.reg.get_drawers(drawer_ids[start : start + D1_MAX_BOUND_PARAMS])
            found.update(row["id"] for row in rows)
        return found

    async def tool_checkpoint(self, drawers: List[Dict[str, str]]) -> Dict[str, Any]:
        stored_ids = []
        for d in drawers:
            res = await self.tool_add_drawer(
                wing=d["wing"],
                room=d["room"],
                content=d["content"],
                source_file=d.get("source_file"),
                drawer_id=d.get("id"),
            )
            stored_ids.append(res["drawer_id"])
        return {"checkpoint": "saved", "count": len(stored_ids), "drawer_ids": stored_ids}

    async def tool_get_drawer(self, drawer_id: str) -> Dict[str, Any]:
        res = await self.col.a_get(ids=[drawer_id])
        if not res.ids:
            return {"error": f"Drawer '{drawer_id}' not found"}
        return {
            "drawer_id": res.ids[0],
            "content": res.documents[0],
            "metadata": res.metadatas[0],
        }

    async def tool_get_drawers(self, drawer_ids: List[str]) -> List[Dict[str, Any]]:
        res = await self.col.a_get(ids=drawer_ids)
        out = []
        for did, doc, meta in zip(res.ids, res.documents, res.metadatas):
            out.append({"drawer_id": did, "content": doc, "metadata": meta})
        return out

    async def tool_list_drawers(
        self,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_content: bool = False,
    ) -> List[Dict[str, Any]]:
        clamped_limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        safe_offset = max(0, int(offset))
        drawers = await self.reg.list_drawers(
            wing=wing, room=room, limit=clamped_limit, offset=safe_offset
        )
        if include_content and drawers:
            ids = [d["id"] for d in drawers]
            docs_map = await self.r2.get_drawers(ids)
            for d in drawers:
                d["content"] = docs_map.get(d["id"], "")
        return drawers

    async def tool_update_drawer(
        self,
        drawer_id: str,
        content: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
    ) -> Dict[str, Any]:
        existing = await self.reg.get_drawer(drawer_id)
        if not existing:
            return {"error": f"Drawer '{drawer_id}' not found"}

        w = wing or existing["wing"]
        r = room or existing["room"]
        meta = existing["metadata"]
        meta["wing"] = w
        meta["room"] = r
        meta["updated_at"] = datetime.now().isoformat()

        await self.col.a_upsert(
            documents=[content],
            ids=[drawer_id],
            metadatas=[meta],
        )
        return {"drawer_id": drawer_id, "status": "updated"}

    async def tool_delete_drawer(self, drawer_id: str) -> Dict[str, Any]:
        await self.col.a_delete(ids=[drawer_id])
        return {"deleted": drawer_id}

    async def tool_delete_drawers(self, drawer_ids: List[str]) -> Dict[str, Any]:
        await self.col.a_delete(ids=drawer_ids)
        return {"deleted_count": len(drawer_ids), "drawer_ids": drawer_ids}

    async def tool_delete_by_source(self, source_file: str) -> Dict[str, Any]:
        ids = await self.reg.find_ids_by_source(source_file)
        if ids:
            await self.col.a_delete(ids=ids)
            await self.reg.delete_drawers(ids)
        return {"source_file": source_file, "deleted_count": len(ids), "deleted_ids": ids}

    async def tool_diary_write(self, content: str, day: Optional[str] = None) -> Dict[str, Any]:
        d = day or datetime.now().strftime("%Y-%m-%d")
        return await self.tool_add_drawer(wing="system", room=f"diary_{d}", content=content)

    async def tool_diary_read(self, limit: int = 7) -> List[Dict[str, Any]]:
        drawers = await self.reg.list_drawers(wing="system", limit=limit)
        return [d for d in drawers if str(d.get("room", "")).startswith("diary_")]

    async def tool_memories_filed_away(self) -> Dict[str, Any]:
        drawers = await self.reg.list_drawers(limit=10)
        return {"recent_drawers": drawers, "count": len(drawers)}

    async def tool_kg_query(
        self,
        entity: str,
        as_of: Optional[str] = None,
        direction: str = "both",
    ) -> List[Dict[str, Any]]:
        return await self.kg.query_entity(entity_name=entity, direction=direction, as_of=as_of)

    async def tool_kg_add(
        self,
        subject: str,
        predicate: str,
        object: str,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        confidence: float = 1.0,
    ) -> Dict[str, Any]:
        tid = await self.kg.add_triple(
            subject=subject,
            predicate=predicate,
            obj=object,
            valid_from=valid_from,
            valid_to=valid_to,
            confidence=confidence,
        )
        return {"triple_id": tid, "status": "stored"}

    async def tool_kg_invalidate(
        self,
        subject: str,
        predicate: str,
        object: str,
        ended: Optional[str] = None,
    ) -> Dict[str, Any]:
        res = await self.kg.invalidate(subject, predicate, object, ended=ended)
        return {"invalidated": res}

    async def tool_kg_supersede(
        self,
        old_subject: str,
        old_predicate: str,
        old_object: str,
        new_subject: str,
        new_predicate: str,
        new_object: str,
        boundary: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = await self.kg.supersede(
            old_subject=old_subject,
            old_predicate=old_predicate,
            old_obj=old_object,
            new_subject=new_subject,
            new_predicate=new_predicate,
            new_obj=new_object,
            boundary=boundary,
        )
        return {"superseded": True, "new_triple_id": tid}

    async def tool_kg_timeline(
        self,
        entity: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        clamped_limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        safe_offset = max(0, int(offset))
        events = await self.kg.timeline(entity=entity, limit=clamped_limit, offset=safe_offset)
        total = await self.kg.timeline_total(entity=entity)
        return {"events": events, "total": total, "limit": clamped_limit, "offset": safe_offset}

    async def tool_kg_stats(self) -> Dict[str, Any]:
        return await self.kg.stats()
