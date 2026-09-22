"""Cloudflare D1 drawer registry adapter.

Provides structured metadata indexing, enumeration, taxonomy, exact-duplicate
checking, and count queries for MemPalace drawers stored in Cloudflare D1.
Complements Vectorize (ANN) and R2 (verbatim bodies).
"""

import json
from typing import Any, Dict, List, Optional


class D1DrawerRegistry:
    """Manages drawer metadata, taxonomy, and exact-duplicate indexing in D1."""

    def __init__(self, db_binding: Any):
        self.db = db_binding

    async def _query_raw(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
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
        rows = await self._query_raw(sql, params)
        return rows[0] if rows else None

    async def _execute_raw(self, sql: str, params: Optional[List[Any]] = None) -> Any:
        stmt = self.db.prepare(sql)
        if params:
            stmt = stmt.bind(*params)

        run_fn = getattr(stmt, "run", None)
        if run_fn is not None:
            res = run_fn()
            if hasattr(res, "__await__"):
                return await res
            return res

        res = stmt.all()
        if hasattr(res, "__await__"):
            return await res
        return res

    async def upsert_drawer(
        self,
        drawer_id: str,
        wing: str,
        room: str,
        r2_key: str,
        content_hash: str,
        metadata: Optional[Dict[str, Any]] = None,
        source_file: Optional[str] = None,
    ) -> None:
        """Upsert a drawer record into the D1 registry."""
        meta_json = json.dumps(metadata or {})
        sql = """
            INSERT INTO drawers (id, wing, room, r2_key, content_hash, metadata_json, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                wing = excluded.wing,
                room = excluded.room,
                r2_key = excluded.r2_key,
                content_hash = excluded.content_hash,
                metadata_json = excluded.metadata_json,
                source_file = excluded.source_file
        """
        await self._execute_raw(
            sql,
            [drawer_id, wing, room, r2_key, content_hash, meta_json, source_file],
        )

    async def get_drawer(self, drawer_id: str) -> Optional[Dict[str, Any]]:
        """Fetch metadata for a single drawer."""
        sql = "SELECT * FROM drawers WHERE id = ?"
        row = await self._first_raw(sql, [drawer_id])
        if not row:
            return None
        return self._format_drawer_row(row)

    async def get_drawers(self, drawer_ids: List[str]) -> List[Dict[str, Any]]:
        """Fetch metadata for multiple drawers."""
        if not drawer_ids:
            return []
        placeholders = ",".join(["?"] * len(drawer_ids))
        sql = f"SELECT * FROM drawers WHERE id IN ({placeholders})"
        rows = await self._query_raw(sql, drawer_ids)
        return [self._format_drawer_row(r) for r in rows]

    async def delete_drawers(self, drawer_ids: List[str]) -> None:
        """Remove drawers from the registry."""
        if not drawer_ids:
            return
        placeholders = ",".join(["?"] * len(drawer_ids))
        sql = f"DELETE FROM drawers WHERE id IN ({placeholders})"
        await self._execute_raw(sql, drawer_ids)

    async def find_ids_by_source(self, source_file: str) -> List[str]:
        """Find all drawer IDs originating from a given source_file without deleting them."""
        find_sql = "SELECT id FROM drawers WHERE source_file = ?"
        rows = await self._query_raw(find_sql, [source_file])
        return [r["id"] for r in rows]

    async def delete_by_source(self, source_file: str) -> List[str]:
        """Delete all drawers originating from a given source_file and return their IDs."""
        ids = await self.find_ids_by_source(source_file)
        if ids:
            await self.delete_drawers(ids)
        return ids

    async def check_duplicate(
        self,
        content_hash: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
    ) -> Optional[str]:
        """Check if identical content hash exists; returns existing drawer_id or None."""
        params: List[Any] = [content_hash]
        where_clauses: List[str] = ["content_hash = ?"]
        if wing:
            where_clauses.append("wing = ?")
            params.append(wing)
        if room:
            where_clauses.append("room = ?")
            params.append(room)

        where = " AND ".join(where_clauses)
        sql = f"SELECT id FROM drawers WHERE {where} LIMIT 1"
        row = await self._first_raw(sql, params)
        return row["id"] if row else None

    async def count(self, wing: Optional[str] = None, room: Optional[str] = None) -> int:
        """Count total drawers, optionally filtered by wing and/or room."""
        params: List[Any] = []
        where_clauses: List[str] = []
        if wing:
            where_clauses.append("wing = ?")
            params.append(wing)
        if room:
            where_clauses.append("room = ?")
            params.append(room)

        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = f"SELECT COUNT(*) as count FROM drawers {where}"
        row = await self._first_raw(sql, params)
        return row["count"] if row else 0

    async def list_drawers(
        self,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List drawers ordered by creation time descending."""
        params: List[Any] = []
        where_clauses: List[str] = []
        if wing:
            where_clauses.append("wing = ?")
            params.append(wing)
        if room:
            where_clauses.append("room = ?")
            params.append(room)

        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = f"SELECT * FROM drawers {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self._query_raw(sql, params)
        return [self._format_drawer_row(r) for r in rows]

    async def list_wings(self) -> List[Dict[str, Any]]:
        """List wings with drawer counts."""
        sql = "SELECT wing, COUNT(*) as drawer_count FROM drawers GROUP BY wing ORDER BY wing ASC"
        rows = await self._query_raw(sql)
        return [{"wing": r["wing"], "drawer_count": r["drawer_count"]} for r in rows]

    async def list_rooms(self, wing: Optional[str] = None) -> List[Dict[str, Any]]:
        """List rooms with drawer counts, optionally for a specific wing."""
        params: List[Any] = []
        where = ""
        if wing:
            where = "WHERE wing = ?"
            params.append(wing)

        sql = f"""
            SELECT wing, room, COUNT(*) as drawer_count
            FROM drawers
            {where}
            GROUP BY wing, room
            ORDER BY wing ASC, room ASC
        """
        rows = await self._query_raw(sql, params)
        return [{"wing": r["wing"], "room": r["room"], "drawer_count": r["drawer_count"]} for r in rows]

    async def get_taxonomy(self) -> Dict[str, Dict[str, int]]:
        """Return full wing -> room -> drawer_count tree."""
        rows = await self.list_rooms()
        taxonomy: Dict[str, Dict[str, int]] = {}
        for r in rows:
            w = r["wing"]
            rm = r["room"]
            cnt = r["drawer_count"]
            if w not in taxonomy:
                taxonomy[w] = {}
            taxonomy[w][rm] = cnt
        return taxonomy

    async def get_all_metadata(self) -> List[Dict[str, Any]]:
        """Return all drawer metadata rows."""
        sql = "SELECT * FROM drawers ORDER BY created_at DESC"
        rows = await self._query_raw(sql)
        return [self._format_drawer_row(r) for r in rows]

    def _format_drawer_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        meta = {}
        if row.get("metadata_json"):
            try:
                meta = json.loads(row["metadata_json"])
            except Exception:
                meta = {}
        meta["wing"] = row["wing"]
        meta["room"] = row["room"]
        if row.get("source_file"):
            meta["source_file"] = row["source_file"]

        return {
            "id": row["id"],
            "wing": row["wing"],
            "room": row["room"],
            "r2_key": row.get("r2_key"),
            "content_hash": row.get("content_hash"),
            "created_at": row.get("created_at"),
            "source_file": row.get("source_file"),
            "metadata": meta,
        }
