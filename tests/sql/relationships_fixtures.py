"""Build small synthetic catalogs for the Phase 6 relationship tests (no database needed)."""

from __future__ import annotations

from typing import Any

from src.sql.relationships.candidates import Candidate
from src.sql.relationships.context import Context, prepare
from src.sql.relationships.model import CatalogGraph, graph_from_documents


def table(
    name: str,
    columns: dict[str, Any],
    pk: tuple[str, ...] = (),
    unique: tuple[tuple[str, ...], ...] = (),
    indexes: tuple[tuple[str, ...], ...] = (),
    schema: str | None = None,
    kind: str = "table",
    comment: str | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    """``columns`` maps a name to a type, or to ``(type, comment)``."""
    cols = []
    for cname, spec in columns.items():
        ctype, ccomment = spec if isinstance(spec, tuple) else (spec, None)
        cols.append({"name": cname, "type": ctype, "is_primary_key": cname in pk, "comment": ccomment})
    return {
        "name": name, "schema": schema, "qualified_name": f"{schema}.{name}" if schema else name, "kind": kind,
        "primary_key": list(pk), "unique_keys": [list(k) for k in unique],
        "indexes": [{"name": f"ix_{name}_{i}", "columns": list(c), "unique": False} for i, c in enumerate(indexes)],
        "comment": comment, "columns": cols, "_rows": rows,
    }


def build(*tables: dict[str, Any], fks: tuple[tuple[str, str, str, str], ...] = (), proc: tuple[dict[str, Any], ...] = (), answering_schema: str | None = None) -> CatalogGraph:
    """``fks`` are (from_table, from_column, to_table, to_column). ``proc`` are raw join_edges rows of sp_evidence.json."""
    schema_doc = {
        "engine": "postgresql", "default_schema": "public", "multi_schema": False,
        "tables": [{k: v for k, v in t.items() if k != "_rows"} for t in tables],
        "relationships": [
            {"from_schema": None, "from_table": a, "from_column": b, "to_schema": None, "to_table": c, "to_column": d} for a, b, c, d in fks
        ],
    }
    inventory = {"tables": [{"qualified_name": t["qualified_name"], "row_estimate": t["_rows"]} for t in tables if t["_rows"] is not None]}
    if answering_schema:
        inventory["answering_scope"] = {"schema": answering_schema, "tables_outside_scope": 0}
    graph = graph_from_documents(schema_doc, inventory, {"join_edges": list(proc)} if proc else None, db_id="test")
    assert graph is not None
    return graph


def proc_edge(a_table: str, a_col: str, b_table: str, b_col: str, confidence: str = "high", routines: int = 2) -> dict[str, Any]:
    return {"a": {"table": a_table, "column": a_col}, "b": {"table": b_table, "column": b_col}, "statements": routines, "routines": routines, "confidence": confidence, "declared_fk": False}


def ctx_of(*tables: dict[str, Any], **kw: Any) -> Context:
    return prepare(build(*tables, **kw))


def triples(cands: list[Candidate], rule: str | None = None) -> set[tuple[str, str, str, str]]:
    """(child.column, parent.column) pairs as 4-tuples, optionally for one rule."""
    return {c.key for c in cands if rule is None or c.rule == rule}
