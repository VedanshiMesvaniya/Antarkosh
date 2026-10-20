"""Stored procedures and functions, read from the database's own catalog (auto-schema Phase 4).

One static, read-only catalog query per engine. Each returns the routine's SOURCE TEXT, never data
rows, and nothing here ever executes a routine. SQLite has no stored routines, so it is always
"basic" mode. Rows are normalised into :class:`Routine` objects with hard size limits, and a routine
whose text the account cannot see is recorded with a reason instead of failing the sync.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Hard limits, so one huge schema can never exhaust memory or disk.
MAX_ROUTINES = 5000
MAX_DEFINITION_CHARS = 200_000
MAX_TOTAL_CHARS = 20_000_000

# Every query returns: routine_schema, routine_name, routine_type, definition.
# Oracle stores source line by line, so its query returns one row per LINE (plus line_no) and the
# lines are joined in Python.
ROUTINE_SQL: dict[str, str] = {
    # prokind needs PostgreSQL 11+. Aggregates and window functions have no definition text.
    "postgresql": r"""
        SELECT n.nspname AS routine_schema,
               p.proname AS routine_name,
               CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS routine_type,
               pg_catalog.pg_get_functiondef(p.oid) AS definition
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
        WHERE p.prokind IN ('f', 'p')
          AND n.nspname <> 'information_schema'
          AND n.nspname NOT LIKE 'pg\_%'
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend AS d WHERE d.objid = p.oid AND d.deptype = 'e')
        ORDER BY n.nspname, p.proname, p.oid
    """,
    # A MySQL "schema" is the database itself, so only the connected database is read.
    # routine_definition is NULL when the account lacks the privilege to see the body.
    "mysql": """
        SELECT r.routine_schema AS routine_schema,
               r.routine_name AS routine_name,
               r.routine_type AS routine_type,
               r.routine_definition AS definition
        FROM information_schema.routines AS r
        WHERE r.routine_schema = DATABASE()
        ORDER BY r.routine_schema, r.routine_name
    """,
    # sys.sql_modules.definition is NULL for WITH ENCRYPTION objects or without VIEW DEFINITION.
    "mssql": """
        SELECT s.name AS routine_schema,
               o.name AS routine_name,
               CASE WHEN o.type = 'P' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS routine_type,
               m.definition AS definition
        FROM sys.objects AS o
        JOIN sys.schemas AS s ON s.schema_id = o.schema_id
        JOIN sys.sql_modules AS m ON m.object_id = o.object_id
        WHERE o.type IN ('P', 'FN', 'IF', 'TF') AND o.is_ms_shipped = 0
        ORDER BY s.name, o.name
    """,
    # all_users.oracle_maintained needs Oracle 12.2+. A package body holds many routines, so it is one unit.
    "oracle": """
        SELECT s.owner AS routine_schema,
               s.name AS routine_name,
               s.type AS routine_type,
               s.text AS definition,
               s.line AS line_no
        FROM all_source s
        WHERE s.type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE BODY')
          AND s.owner IN (SELECT u.username FROM all_users u WHERE u.oracle_maintained = 'N')
        ORDER BY s.owner, s.name, s.type, s.line
    """,
}

# What to tell the admin when routines exist but their text is hidden.
PERMISSION_HINT: dict[str, str] = {
    "postgresql": "grant the reading role access to the routines' schemas",
    "mysql": "grant the reading account SHOW_ROUTINE (MySQL 8) or SELECT on mysql.proc (MariaDB); EXECUTE alone shows names but not bodies",
    "mssql": "grant the reading login VIEW DEFINITION (encrypted routines can never be read)",
    "oracle": "grant the reading user access to ALL_SOURCE objects (wrapped routines can never be read)",
}


@dataclass(frozen=True)
class Routine:
    """One stored routine (or a script from the procedures folder). ``definition`` is None when it could not be read."""

    schema: str
    name: str
    kind: str  # PROCEDURE | FUNCTION | PACKAGE BODY | SCRIPT (a .sql file from the procedures folder)
    source: str  # "database" | "folder"
    definition: str | None
    unreadable: str | None = None  # None | "hidden" | "empty" | "wrapped" | "limit"
    truncated: bool = False
    members: tuple[str, ...] = ()  # a folder SCRIPT: the routine names it defines

    @property
    def sha256(self) -> str:
        return hashlib.sha256((self.definition or "").encode("utf-8")).hexdigest()[:16] if self.definition else ""


def _clean_kind(raw: Any) -> str:
    kind = str(raw or "").strip().upper()
    return kind if kind in ("PROCEDURE", "FUNCTION", "PACKAGE BODY") else "FUNCTION"


def _make(schema: Any, name: Any, kind: Any, text: Any, engine: str, spent: int) -> tuple[Routine, int]:
    """Build one Routine from raw text, applying the limits. Returns (routine, characters kept)."""
    sch, nm, kd = str(schema or ""), str(name or ""), _clean_kind(kind)
    if text is None or not str(text).strip():
        reason = "hidden" if text is None else "empty"
        return Routine(sch, nm, kd, "database", None, reason), 0
    body = str(text)
    if engine == "oracle" and body.lstrip().split("\n", 1)[0].lower().rstrip().endswith(" wrapped"):
        return Routine(sch, nm, kd, "database", None, "wrapped"), 0
    if spent >= MAX_TOTAL_CHARS:
        return Routine(sch, nm, kd, "database", None, "limit"), 0
    truncated = len(body) > MAX_DEFINITION_CHARS
    if truncated:
        body = body[:MAX_DEFINITION_CHARS]
    return Routine(sch, nm, kd, "database", body, None, truncated), len(body)


def normalize_rows(engine: str, rows: Iterable[dict[str, Any]]) -> tuple[list[Routine], bool]:
    """Turn catalog rows into Routines. Returns (routines, hit_count_limit)."""
    routines: list[Routine] = []
    spent = 0
    limited = False

    if engine == "oracle":  # rows are lines: join them per (owner, name, type)
        grouped: dict[tuple[str, str, str], list[str]] = {}
        for r in rows:
            grouped.setdefault((r["routine_schema"], r["routine_name"], r["routine_type"]), []).append(str(r["definition"] or ""))
        items = [(k[0], k[1], k[2], "".join(v)) for k, v in grouped.items()]
    else:
        items = [(r["routine_schema"], r["routine_name"], r["routine_type"], r["definition"]) for r in rows]

    for schema, name, kind, text in items:
        if len(routines) >= MAX_ROUTINES:
            limited = True
            break
        routine, used = _make(schema, name, kind, text, engine, spent)
        spent += used
        routines.append(routine)
    return routines, limited


__all__ = [
    "MAX_DEFINITION_CHARS",
    "MAX_ROUTINES",
    "MAX_TOTAL_CHARS",
    "PERMISSION_HINT",
    "ROUTINE_SQL",
    "Routine",
    "normalize_rows",
]
