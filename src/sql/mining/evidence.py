"""Aggregate the facts of all routines into ``schema/sp_evidence.json`` (auto-schema Phase 5, step 3).

The file holds FACTS only: table names with how many routines read or write them, join edges with how
often they were seen, and short string values seen for a column. It never holds statement or routine
text. A join seen in two or more routines (or one that is already a declared foreign key) is "high"
confidence; anything seen once is "low" and is stored but not used by later phases.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.mining.catalog_index import CatalogIndex
from src.sql.mining.extractor import RoutineFacts

GENERATED_BY = "auto_sp_mining"
EVIDENCE_REL = "schema/sp_evidence.json"
MAX_VALUES_PER_COLUMN = 20
MAX_EDGES = 5000
MAX_VALUE_COLUMNS = 2000
MAX_TABLES = 5000

Edge = tuple[tuple[str, str], tuple[str, str]]


class Evidence:
    """Collects per-routine facts. ``add_routine`` is called once per routine, in any order."""

    def __init__(self, catalog: CatalogIndex | None) -> None:
        self.catalog = catalog
        self.read_in: dict[str, int] = {}
        self.written_in: dict[str, int] = {}
        self.edges: dict[Edge, dict[str, int]] = {}
        self.values: dict[tuple[str, str], dict[str, Any]] = {}
        self.stats = {
            "routines_total": 0,
            "routines_parsed": 0,
            "routines_without_sql": 0,
            "statements_found": 0,
            "statements_parsed": 0,
            "dynamic_sql": 0,
            "unknown_tables_dropped": 0,
            "truncated": False,
        }

    def add_routine(self, found: int, parsed: int, dynamic: int, facts: RoutineFacts, *, truncated: bool = False) -> None:
        s = self.stats
        s["routines_total"] += 1
        s["statements_found"] += found
        s["statements_parsed"] += parsed
        s["dynamic_sql"] += dynamic
        s["unknown_tables_dropped"] += facts.unknown_tables
        s["truncated"] = bool(s["truncated"] or truncated)
        if parsed:
            s["routines_parsed"] += 1
        else:
            s["routines_without_sql"] += 1
        for table in facts.reads:
            self.read_in[table] = self.read_in.get(table, 0) + 1
        for table in facts.writes:
            self.written_in[table] = self.written_in.get(table, 0) + 1
        for edge in facts.joins:
            slot = self.edges.setdefault(edge, {"statements": 0, "routines": 0})
            slot["statements"] += facts.join_counts.get(edge, 1)
            slot["routines"] += 1
        for key, per_value in facts.values.items():
            slot = self.values.setdefault(key, {"routines": 0, "values": {}})
            slot["routines"] += 1
            for value, n in per_value.items():
                slot["values"][value] = slot["values"].get(value, 0) + n

    def document(self, db_id: str, engine: str) -> dict[str, Any]:
        cat = self.catalog
        edges = []
        for (a, b), seen in self.edges.items():
            declared = bool(cat and cat.is_declared_fk(a, b))
            edges.append({
                "a": {"table": a[0], "column": a[1]},
                "b": {"table": b[0], "column": b[1]},
                "statements": seen["statements"],
                "routines": seen["routines"],
                "confidence": "high" if (seen["routines"] >= 2 or declared) else "low",
                "declared_fk": declared,
            })
        edges.sort(key=lambda e: (-e["routines"], -e["statements"], e["a"]["table"], e["a"]["column"], e["b"]["table"], e["b"]["column"]))

        value_rows = []
        for (table, column), slot in self.values.items():
            if len(slot["values"]) > MAX_VALUES_PER_COLUMN:  # many distinct values = not an enumeration
                continue
            ranked = sorted(slot["values"].items(), key=lambda kv: (-kv[1], kv[0]))
            value_rows.append({
                "table": table,
                "column": column,
                "values": [{"value": v, "count": n} for v, n in ranked],
                "routines": slot["routines"],
            })
        value_rows.sort(key=lambda r: (-r["routines"], r["table"], r["column"]))

        names = sorted(set(self.read_in) | set(self.written_in))
        tables = {n: {"read_in": self.read_in.get(n, 0), "written_in": self.written_in.get(n, 0)} for n in names[:MAX_TABLES]}
        return {
            "generated_by": GENERATED_BY,
            "version": 1,
            "db_id": db_id,
            "engine": engine,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "validated_against_catalog": cat is not None,
            "stats": dict(self.stats),
            "tables": tables,
            "join_edges": edges[:MAX_EDGES],
            "enum_values": value_rows[:MAX_VALUE_COLUMNS],
        }


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    """Counts only. This is what reaches the sync API response."""
    edges = doc["join_edges"]
    return {
        **doc["stats"],
        "validated_against_catalog": doc["validated_against_catalog"],
        "tables": len(doc["tables"]),
        "joins_high": sum(1 for e in edges if e["confidence"] == "high"),
        "joins_low": sum(1 for e in edges if e["confidence"] == "low"),
        "value_columns": len(doc["enum_values"]),
    }


def write_evidence(db_id: str, doc: dict[str, Any], databases_dir: Path | None = None) -> dict[str, Any]:
    """Write into an EXISTING database folder. Never for erp_main; never over a file this phase did not generate."""
    validate_db_id(db_id)
    result: dict[str, Any] = {"written": [], "skipped": {}}
    if db_id == DEFAULT_DB_ID:
        result["skipped"]["all"] = "pinned database: curated files are never replaced"
        return result
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        result["skipped"]["all"] = "database folder does not exist"
        return result
    path = folder / EVIDENCE_REL
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if not (isinstance(current, dict) and current.get("generated_by") == GENERATED_BY):
            result["skipped"][EVIDENCE_REL] = "a file not generated by auto_sp_mining is present; left untouched"
            return result
    path.parent.mkdir(parents=True, exist_ok=True)
    registry.atomic_write_json(path, doc, mode=0o644)
    result["written"].append(EVIDENCE_REL)
    return result


__all__ = ["EVIDENCE_REL", "GENERATED_BY", "Evidence", "summary", "write_evidence"]
