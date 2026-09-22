"""Cloudflare D1 Knowledge Graph adapter.

Implements the temporal entity-relationship knowledge graph backed by Cloudflare D1.
Translates SQLite queries to Cloudflare D1 asynchronous calls: `env.DB.prepare(...).bind(...).all()`.
Preserves exact schema, temporal filters, and entity-triple semantics from upstream.
"""

from datetime import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

from ..config import sanitize_iso_temporal
from ..ids import make_triple_id


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


class D1KnowledgeGraph:
    """Temporal entity-relationship knowledge graph stored in Cloudflare D1."""

    def __init__(self, db_binding: Any):
        self.db = db_binding

    async def _query_raw(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
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

        if isinstance(raw, dict):
            return raw.get("results", [])
        elif hasattr(raw, "results"):
            return raw.results
        elif isinstance(raw, list):
            return raw
        return []

    async def _first_raw(self, sql: str, params: Optional[List[Any]] = None) -> Optional[Dict[str, Any]]:
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

    def _entity_id(self, name: str) -> str:
        return name.lower().replace(" ", "_").replace("'", "")

    async def add_entity(self, name: str, entity_type: str = "unknown", properties: Optional[dict] = None) -> str:
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
        await self.invalidate(old_subject, old_predicate, old_obj, ended=bound)
        return await self.add_triple(
            new_subject,
            new_predicate,
            new_obj,
            valid_from=bound,
            confidence=confidence,
            source_closet=source_closet,
            source_file=source_file,
            source_drawer_id=source_drawer_id,
            adapter_name=adapter_name,
        )

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
