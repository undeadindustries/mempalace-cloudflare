"""Cloudflare D1 Knowledge Graph adapter.

Implements the temporal entity-relationship knowledge graph backed by Cloudflare D1.
Translates SQLite queries to Cloudflare D1 asynchronous calls: `env.DB.prepare(...).bind(...).all()`.
Preserves exact schema, temporal filters, and entity-triple semantics from upstream.
"""

from datetime import date, datetime
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

_ISO_DATE_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")
_ISO_UTC_DATETIME_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:Z|\+00:00)$"
)


def _validate_iso_temporal_calendar(value: str) -> None:
    if _ISO_DATE_RE.match(value):
        date.fromisoformat(value)
        return
    if _ISO_UTC_DATETIME_RE.match(value):
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return
    raise ValueError


def sanitize_iso_temporal(value: Any, field_name: str = "date") -> Optional[str]:
    """Validate an ISO-8601 date or canonical UTC datetime string."""
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    value = value.strip()
    try:
        _validate_iso_temporal_calendar(value)
    except ValueError:
        raise ValueError(
            f"{field_name}={value!r} is not a valid ISO-8601 date or UTC datetime "
            "(expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)"
        ) from None
    if value.endswith("+00:00"):
        value = f"{value[:-6]}Z"
    return value


def make_triple_id(
    sub_id: str, predicate: str, obj_id: str, valid_from: str, recorded_at: str
) -> str:
    """Triple ID matching upstream ids.make_triple_id contract."""
    # str() matches upstream ids._delimited_sha256: valid_from=None hashes
    # as the literal "None" instead of raising TypeError.
    key = "".join(f"{len(part)}:{part}" for part in map(str, (valid_from, recorded_at))).encode()
    hash12 = hashlib.sha256(key).hexdigest()[:12]
    return f"t_{sub_id}_{predicate}_{obj_id}_{hash12}"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_date_only_temporal(value: str) -> bool:
    return isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-"


def _temporal_start_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if _is_date_only_temporal(value):
        return f"{value}T00:00:00Z"
    return value


def _temporal_end_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if _is_date_only_temporal(value):
        return f"{value}T23:59:59Z"
    return value


def _sql_temporal_start_expr(column: str) -> str:
    return (
        f"CASE WHEN length({column}) = 10 "
        f"AND substr({column}, 5, 1) = '-' "
        f"AND substr({column}, 8, 1) = '-' "
        f"THEN {column} || 'T00:00:00Z' ELSE {column} END"
    )


def _sql_temporal_end_expr(column: str) -> str:
    return (
        f"CASE WHEN length({column}) = 10 "
        f"AND substr({column}, 5, 1) = '-' "
        f"AND substr({column}, 8, 1) = '-' "
        f"THEN {column} || 'T23:59:59Z' ELSE {column} END"
    )


def _temporal_filter_sql(as_of: str) -> Tuple[str, List[str]]:
    as_of_key = _temporal_start_key(as_of)
    valid_from_expr = _sql_temporal_start_expr("t.valid_from")
    valid_to_expr = _sql_temporal_end_expr("t.valid_to")

    return (
        f" AND (t.valid_from IS NULL OR {valid_from_expr} <= ?) "
        f"AND (t.valid_to IS NULL OR {valid_to_expr} > ?)",
        [as_of_key, as_of_key],
    )


def _to_py_dict(obj: Any) -> Any:
    """Convert JsProxy object or dict to Python native types."""
    if obj is None:
        return None
    if hasattr(obj, "to_py"):
        try:
            return obj.to_py()
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    try:
        import js

        entries = js.Object.entries(obj)
        res_dict = {}
        for entry in entries:
            k = entry[0]
            v = entry[1]
            if hasattr(v, "to_py"):
                v = v.to_py()
            res_dict[k] = v
        return res_dict
    except Exception:
        pass
    return obj


class D1KnowledgeGraph:
    """Temporal entity-relationship knowledge graph stored in Cloudflare D1."""

    def __init__(self, db_binding: Any):
        self.db = db_binding

    async def _query_raw(
        self, sql: str, params: Optional[List[Any]] = None
    ) -> List[Dict[str, Any]]:
        """Run a query against D1 and return list of result row dicts."""
        stmt = self.db.prepare(sql)
        if params:
            stmt = stmt.bind(*params)

        res = getattr(stmt, "all", None)
        if res is not None:
            raw = res()
            if hasattr(raw, "__await__"):
                raw = await raw
        else:
            raise RuntimeError("D1 statement does not support 'all' method")

        raw = _to_py_dict(raw)

        rows = []
        if isinstance(raw, dict):
            rows = raw.get("results", [])
        elif hasattr(raw, "results"):
            rows = raw.results
        elif isinstance(raw, list):
            rows = raw

        rows = _to_py_dict(rows)
        if isinstance(rows, list):
            return [_to_py_dict(r) for r in rows]
        return []

    async def _first_raw(
        self, sql: str, params: Optional[List[Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Run a query and return first row or None."""
        rows = await self._query_raw(sql, params)
        return rows[0] if rows else None

    async def _execute_raw(self, sql: str, params: Optional[List[Any]] = None) -> Any:
        """Execute a mutation against D1."""
        stmt = self.db.prepare(sql)
        if params:
            stmt = stmt.bind(*params)

        run_fn = getattr(stmt, "run", None)
        if run_fn is not None:
            res = run_fn()
            if hasattr(res, "__await__"):
                return await res
            return res

        # Fallback to all() if run() not present
        res = stmt.all()
        if hasattr(res, "__await__"):
            return await res
        return res

    async def _batch_execute(self, statements: List[tuple[str, List[Any]]]) -> Any:
        """Execute multiple SQL statements in a single batch transaction if supported."""
        if not statements:
            return []

        prepared_stmts = []
        for sql, params in statements:
            stmt = self.db.prepare(sql)
            if params:
                stmt = stmt.bind(*params)
            prepared_stmts.append(stmt)

        batch_fn = getattr(self.db, "batch", None)
        if batch_fn is not None:
            res = batch_fn(prepared_stmts)
            if hasattr(res, "__await__"):
                return await res
            return res

        # Fallback: sequential execution
        results = []
        for sql, params in statements:
            results.append(await self._execute_raw(sql, params))
        return results

    def _entity_id(self, name: str) -> str:
        return name.lower().replace(" ", "_").replace("'", "")

    async def add_entity(
        self, name: str, entity_type: str = "unknown", properties: Optional[dict] = None
    ) -> str:
        """Add or update an entity node."""
        eid = self._entity_id(name)
        props = json.dumps(properties or {})
        await self._execute_raw(
            "INSERT OR REPLACE INTO entities (id, name, type, properties) VALUES (?, ?, ?, ?)",
            [eid, name, entity_type, props],
        )
        return eid

    async def add_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        confidence: float = 1.0,
        source_closet: Optional[str] = None,
        source_file: Optional[str] = None,
        source_drawer_id: Optional[str] = None,
        adapter_name: Optional[str] = None,
    ) -> str:
        """Add a relationship triple: subject -> predicate -> object."""
        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        valid_to = sanitize_iso_temporal(valid_to, "valid_to")

        if (
            valid_from is not None
            and valid_to is not None
            and _temporal_end_key(valid_to) < _temporal_start_key(valid_from)
        ):
            raise ValueError(
                f"valid_to={valid_to!r} is before valid_from={valid_from!r}; inverted interval"
            )

        sub_id = self._entity_id(subject)
        obj_id = self._entity_id(obj)
        pred = predicate.lower().replace(" ", "_")

        await self._execute_raw(
            "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
            [sub_id, subject],
        )
        await self._execute_raw(
            "INSERT OR IGNORE INTO entities (id, name) VALUES (?, ?)",
            [obj_id, obj],
        )

        existing = await self._first_raw(
            "SELECT id FROM triples WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
            [sub_id, pred, obj_id],
        )
        if existing:
            return existing["id"]

        now_str = datetime.now().isoformat()
        triple_id = make_triple_id(sub_id, pred, obj_id, valid_from, now_str)

        await self._execute_raw(
            """INSERT INTO triples (
                id, subject, predicate, object, valid_from, valid_to,
                confidence, source_closet, source_file, source_drawer_id, adapter_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                triple_id,
                sub_id,
                pred,
                obj_id,
                valid_from,
                valid_to,
                confidence,
                source_closet,
                source_file,
                source_drawer_id,
                adapter_name,
            ],
        )
        return triple_id

    async def invalidate(
        self,
        subject: str,
        predicate: str,
        obj: str,
        ended: Optional[str] = None,
        valid_to: Optional[str] = None,
    ) -> bool:
        """Mark an existing open triple as no longer valid."""
        end_date = ended or valid_to or datetime.now().strftime("%Y-%m-%d")
        end_date = sanitize_iso_temporal(end_date, "ended")

        sub_id = self._entity_id(subject)
        obj_id = self._entity_id(obj)
        pred = predicate.lower().replace(" ", "_")

        rows = await self._query_raw(
            "SELECT id, valid_from FROM triples WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
            [sub_id, pred, obj_id],
        )
        if not rows:
            return False

        updated = False
        for row in rows:
            vf = row["valid_from"]
            if vf is not None and _temporal_end_key(end_date) < _temporal_start_key(vf):
                raise ValueError(
                    f"Ended date {end_date!r} cannot be earlier than valid_from {vf!r}"
                )
            await self._execute_raw(
                "UPDATE triples SET valid_to=? WHERE id=?",
                [end_date, row["id"]],
            )
            updated = True

        return updated

    async def supersede(
        self,
        old_subject: str,
        old_predicate: str,
        old_obj: str,
        new_subject: str,
        new_predicate: str,
        new_obj: str,
        boundary: Optional[str] = None,
        confidence: float = 1.0,
        source_closet: Optional[str] = None,
        source_file: Optional[str] = None,
        source_drawer_id: Optional[str] = None,
        adapter_name: Optional[str] = None,
    ) -> str:
        """Atomically invalidate an old triple and insert its successor."""
        bound = boundary or datetime.now().strftime("%Y-%m-%d")
        bound = sanitize_iso_temporal(bound, "boundary")

        old_sub_id = self._entity_id(old_subject)
        old_obj_id = self._entity_id(old_obj)
        old_pred = old_predicate.lower().replace(" ", "_")

        # Find existing active triple to validate date constraint
        rows = await self._query_raw(
            "SELECT id, valid_from FROM triples WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
            [old_sub_id, old_pred, old_obj_id],
        )
        batch_statements: List[tuple[str, List[Any]]] = []

        if rows:
            for row in rows:
                vf = row["valid_from"]
                if vf is not None and _temporal_end_key(bound) < _temporal_start_key(vf):
                    raise ValueError(
                        f"Ended date {bound!r} cannot be earlier than valid_from {vf!r}"
                    )
                batch_statements.append(
                    ("UPDATE triples SET valid_to=? WHERE id=?", [bound, row["id"]])
                )

        # Prepare new triple
        new_sub_id = self._entity_id(new_subject)
        new_obj_id = self._entity_id(new_obj)
        new_pred = new_predicate.lower().replace(" ", "_")

        now_str = datetime.now().isoformat()
        triple_id = make_triple_id(new_sub_id, new_pred, new_obj_id, bound, now_str)

        # Ensure entities exist
        props = json.dumps({})
        batch_statements.append(
            (
                "INSERT OR REPLACE INTO entities (id, name, type, properties) VALUES (?, ?, ?, ?)",
                [new_sub_id, new_subject, "unknown", props],
            )
        )
        batch_statements.append(
            (
                "INSERT OR REPLACE INTO entities (id, name, type, properties) VALUES (?, ?, ?, ?)",
                [new_obj_id, new_obj, "unknown", props],
            )
        )

        # Insert new triple
        batch_statements.append(
            (
                """INSERT INTO triples (
                id, subject, predicate, object, valid_from, valid_to,
                confidence, source_closet, source_file, source_drawer_id, adapter_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    triple_id,
                    new_sub_id,
                    new_pred,
                    new_obj_id,
                    bound,
                    None,
                    confidence,
                    source_closet,
                    source_file,
                    source_drawer_id,
                    adapter_name,
                ],
            )
        )

        # Execute batch atomically
        await self._batch_execute(batch_statements)
        return triple_id

    async def query_entity(
        self,
        entity_name: str,
        direction: str = "outgoing",
        as_of: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query all relationships for an entity, with optional temporal as-of filter."""
        eid = self._entity_id(entity_name)
        params: List[Any] = []
        temporal_clause = ""
        if as_of:
            temporal_clause, temporal_params = _temporal_filter_sql(as_of)

        if direction == "outgoing":
            sql = f"""
                SELECT t.*, s.name as subject_name, o.name as object_name
                FROM triples t
                JOIN entities s ON t.subject = s.id
                JOIN entities o ON t.object = o.id
                WHERE t.subject = ? {temporal_clause}
                ORDER BY t.valid_from ASC NULLS LAST
            """
            params = [eid] + (temporal_params if as_of else [])
        elif direction == "incoming":
            sql = f"""
                SELECT t.*, s.name as subject_name, o.name as object_name
                FROM triples t
                JOIN entities s ON t.subject = s.id
                JOIN entities o ON t.object = o.id
                WHERE t.object = ? {temporal_clause}
                ORDER BY t.valid_from ASC NULLS LAST
            """
            params = [eid] + (temporal_params if as_of else [])
        else:
            sql = f"""
                SELECT t.*, s.name as subject_name, o.name as object_name
                FROM triples t
                JOIN entities s ON t.subject = s.id
                JOIN entities o ON t.object = o.id
                WHERE (t.subject = ? OR t.object = ?) {temporal_clause}
                ORDER BY t.valid_from ASC NULLS LAST
            """
            params = [eid, eid] + (temporal_params if as_of else [])

        rows = await self._query_raw(sql, params)
        return rows

    async def find_entity_candidates(self, query: str, limit: int = 5) -> List[Dict[str, str]]:
        """Find entity candidates matching a name or prefix."""
        eid = self._entity_id(query)
        escaped = _escape_like(query)
        sql = """
            SELECT id, name, type FROM entities
            WHERE id = ? OR name = ? OR name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\'
            LIMIT ?
        """
        rows = await self._query_raw(sql, [eid, query, f"{escaped}%", f"{eid}%", limit])
        return [{"id": r["id"], "name": r["name"], "type": r.get("type", "unknown")} for r in rows]

    async def query_relationship(
        self,
        predicate: str,
        as_of: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query all triples for a given predicate."""
        pred = predicate.lower().replace(" ", "_")
        temporal_clause = ""
        params: List[Any] = [pred]
        if as_of:
            temporal_clause, temporal_params = _temporal_filter_sql(as_of)
            params.extend(temporal_params)

        sql = f"""
            SELECT t.*, s.name as subject_name, o.name as object_name
            FROM triples t
            JOIN entities s ON t.subject = s.id
            JOIN entities o ON t.object = o.id
            WHERE t.predicate = ? {temporal_clause}
            ORDER BY t.valid_from ASC NULLS LAST
        """
        return await self._query_raw(sql, params)

    async def timeline(
        self,
        entity: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Chronological timeline of triples, optionally filtered by entity."""
        params: List[Any] = []
        where_clause = ""
        if entity:
            eid = self._entity_id(entity)
            where_clause = "WHERE (t.subject = ? OR t.object = ?)"
            params = [eid, eid]

        sql = f"""
            SELECT t.*, s.name as subject_name, o.name as object_name
            FROM triples t
            JOIN entities s ON t.subject = s.id
            JOIN entities o ON t.object = o.id
            {where_clause}
            ORDER BY t.valid_from ASC NULLS LAST, t.id ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        return await self._query_raw(sql, params)

    async def timeline_total(self, entity: Optional[str] = None) -> int:
        """Count total triples in the timeline."""
        if entity:
            eid = self._entity_id(entity)
            sql = "SELECT COUNT(*) as count FROM triples WHERE subject = ? OR object = ?"
            row = await self._first_raw(sql, [eid, eid])
        else:
            sql = "SELECT COUNT(*) as count FROM triples"
            row = await self._first_raw(sql)
        return row["count"] if row else 0

    async def stats(self) -> Dict[str, Any]:
        """Summary statistics for the knowledge graph."""
        e_row = await self._first_raw("SELECT COUNT(*) as c FROM entities")
        t_row = await self._first_raw("SELECT COUNT(*) as c FROM triples")
        cur_row = await self._first_raw("SELECT COUNT(*) as c FROM triples WHERE valid_to IS NULL")
        preds = await self._query_raw("SELECT DISTINCT predicate FROM triples ORDER BY predicate")

        return {
            "entity_count": e_row["c"] if e_row else 0,
            "triple_count": t_row["c"] if t_row else 0,
            "current_facts": cur_row["c"] if cur_row else 0,
            "predicates": [p["predicate"] for p in preds],
        }
