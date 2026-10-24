"""The trusted join graph for Phase 8: which tables can be joined directly, and how sure we are.

Only edges a person could rely on become links:

* foreign keys the database declares (confidence 1.0),
* every entry of ``relationships.json``, curated or generated (its own ``confidence``, else 0.90 for a curated entry),
* high-confidence edges of ``inferred_relationships.json`` that Phase 6 marked publishable, and only if that file was
  made from these same inputs (a stale file contributes nothing),
* joins seen in two or more stored procedures (Phase 5 "high", 0.90).

Medium and low edges are never used. Self-references, views, tables outside the schema the prompts can see, and tables
Phase 7 found to be copies of another table are not nodes.

Pure functions over documents already on disk. No database, no data rows.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from src.sql.relationships.model import CatalogGraph, Table

CURATED_DEFAULT = 0.90
PROCEDURE_CONFIDENCE = 0.90
BLOCKED_AS_BRIDGE_TAGS = frozenset({"temp", "backup", "test"})
EDGE_COUNT_KEYS = frozenset({"self_reference", "not_a_node", "relationships_file_unresolved"})  # everything else in Inputs.counts counts tables


@dataclass(frozen=True)
class Join:
    """One column pair that joins two tables. ``child`` is the table holding the foreign key, or None if unknown."""

    a: str
    a_column: str
    b: str
    b_column: str
    confidence: float
    kinds: tuple[str, ...]
    child: str | None

    def column_of(self, table: str) -> str:
        return self.a_column if table == self.a else self.b_column


@dataclass(frozen=True)
class Link:
    """Two tables that join directly. ``join`` is the best column pair; ``alternates`` counts other pairs."""

    join: Join
    alternates: int

    @property
    def confidence(self) -> float:
        return self.join.confidence

    def step(self, from_table: str) -> str | None:
        """``up`` (many-to-one: from the table holding the key to the one it points at), ``down`` (one-to-many) or None."""
        if self.join.child is None:
            return None
        return "up" if self.join.child == from_table else "down"


@dataclass
class Inputs:
    nodes: set[str]
    blocked_as_bridge: set[str]
    counts: Counter[str]
    profiles_used: bool
    inferred_used: bool


# ---------------------------------------------------------------- nodes


def fresh_profiles(graph: CatalogGraph, profiles_doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(profiles_doc, dict) or profiles_doc.get("generated_by") != "auto_table_profiles":
        return None
    if profiles_doc.get("inputs_hash") != graph.inputs_hash:
        return None
    tables = profiles_doc.get("tables")
    return tables if isinstance(tables, dict) else None


def select_nodes(graph: CatalogGraph, profiles_doc: dict[str, Any] | None) -> Inputs:
    """The tables a path may use, and the ones that may end a path but never bridge two others."""
    counts: Counter[str] = Counter()
    nodes: set[str] = set()
    blocked: set[str] = set()
    if graph.multi_schema and not graph.scope_known:  # same rule as relationships.json: we cannot tell what the prompts see
        counts["scope_unknown"] = len(graph.tables)
        return Inputs(set(), set(), counts, False, False)
    profiles = fresh_profiles(graph, profiles_doc)
    for q, t in graph.tables.items():
        if t.is_view:
            counts["view"] += 1
            continue
        if not graph.in_answering_scope(t):
            counts["outside_scope"] += 1
            continue
        p = profiles.get(q) if profiles else None
        if isinstance(p, dict):
            if p.get("authority") == "secondary":
                counts["secondary_copy"] += 1
                continue
            if BLOCKED_AS_BRIDGE_TAGS & set(p.get("tags") or []):
                blocked.add(q)
        nodes.add(q)
    return Inputs(nodes, blocked, counts, profiles is not None, False)


# ---------------------------------------------------------------- joins


def _resolve(graph: CatalogGraph, schema: Any, name: Any) -> Table | None:
    if not isinstance(name, str) or not name:
        return None
    if isinstance(schema, str) and schema:
        t = graph.table(f"{schema}.{name}")
        if t is not None and t.schema and t.schema.lower() == schema.lower():
            return t
    return graph.table(name)


def _confidence(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(1.0, max(0.0, float(value)))


class _Collector:
    def __init__(self, nodes: set[str], counts: Counter[str]) -> None:
        self.nodes = nodes
        self.counts = counts
        self.joins: dict[tuple, dict[str, Any]] = {}

    def add(self, kind: str, child: str, child_col: str, parent: str, parent_col: str, confidence: float, *, directed: bool) -> None:
        if child == parent:
            self.counts["self_reference"] += 1
            return
        if child not in self.nodes or parent not in self.nodes:
            self.counts["not_a_node"] += 1
            return
        left, right = (child, child_col.lower()), (parent, parent_col.lower())
        key = (left, right) if left <= right else (right, left)
        rec = self.joins.get(key)
        if rec is None:
            first_is_child = key[0] == left
            a_col, b_col = (child_col, parent_col) if first_is_child else (parent_col, child_col)
            rec = {"a": key[0][0], "a_column": a_col, "b": key[1][0], "b_column": b_col, "confidence": confidence, "kinds": set(), "child": None}
            self.joins[key] = rec
        rec["confidence"] = max(rec["confidence"], confidence)
        rec["kinds"].add(kind)
        if directed and rec["child"] is None:
            rec["child"] = child


def collect_joins(
    graph: CatalogGraph,
    inputs: Inputs,
    *,
    relationships_doc: dict[str, Any] | None,
    inferred_doc: dict[str, Any] | None,
) -> list[Join]:
    """Every trusted column pair between two nodes, merged: the same pair seen twice keeps its best confidence."""
    c = _Collector(inputs.nodes, inputs.counts)
    for fk in graph.declared_fks:
        c.add("declared", fk.from_table, fk.from_column, fk.to_table, fk.to_column, 1.0, directed=True)

    rels = relationships_doc.get("relationships") if isinstance(relationships_doc, dict) else None
    for e in rels if isinstance(rels, list) else []:
        if not isinstance(e, dict):
            continue
        child, parent = _resolve(graph, e.get("from_schema"), e.get("from_table")), _resolve(graph, e.get("to_schema"), e.get("to_table"))
        if child is None or parent is None or not e.get("from_column") or not e.get("to_column"):
            inputs.counts["relationships_file_unresolved"] += 1
            continue
        c.add("relationships_file", child.qualified, str(e["from_column"]), parent.qualified, str(e["to_column"]),
              _confidence(e.get("confidence"), CURATED_DEFAULT), directed=True)

    if isinstance(inferred_doc, dict) and inferred_doc.get("generated_by") == "auto_relationships" and inferred_doc.get("inputs_hash") == graph.inputs_hash:
        for e in inferred_doc.get("relationships") or []:
            if not isinstance(e, dict) or e.get("band") != "high" or not e.get("publishable") or not e.get("direction_known"):
                continue
            child, parent = _resolve(graph, e.get("from_schema"), e.get("from_table")), _resolve(graph, e.get("to_schema"), e.get("to_table"))
            if child is None or parent is None or not e.get("from_column") or not e.get("to_column"):
                continue
            inputs.inferred_used = True
            c.add("inferred", child.qualified, str(e["from_column"]), parent.qualified, str(e["to_column"]),
                  _confidence(e.get("confidence"), 0.0), directed=True)

    for pe in graph.proc_edges:
        if pe.confidence == "high":
            c.add("procedure", pe.a_table, pe.a_column, pe.b_table, pe.b_column, PROCEDURE_CONFIDENCE, directed=False)

    return [
        Join(r["a"], r["a_column"], r["b"], r["b_column"], round(r["confidence"], 4), tuple(sorted(r["kinds"])), r["child"])
        for _, r in sorted(c.joins.items())
        if r["confidence"] > 0
    ]


def build_links(joins: list[Join]) -> dict[str, dict[str, Link]]:
    """Adjacency map ``table -> {neighbour: Link}``. Several column pairs between two tables become one link."""
    by_pair: dict[tuple[str, str], list[Join]] = {}
    for j in joins:
        by_pair.setdefault((j.a, j.b), []).append(j)
    adj: dict[str, dict[str, Link]] = {}
    for (a, b), group in sorted(by_pair.items()):
        best = min(group, key=lambda j: (-j.confidence, j.a_column.lower(), j.b_column.lower()))
        link = Link(best, len(group) - 1)
        adj.setdefault(a, {})[b] = link
        adj.setdefault(b, {})[a] = link
    return adj


def content_hash(doc: Any) -> str | None:
    """A short fingerprint of a file's meaningful content (timestamps ignored), for the writer's change detection."""
    if not isinstance(doc, dict):
        return None
    clean = {k: v for k, v in doc.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


__all__ = [
    "BLOCKED_AS_BRIDGE_TAGS", "CURATED_DEFAULT", "EDGE_COUNT_KEYS", "PROCEDURE_CONFIDENCE", "Inputs", "Join", "Link",
    "build_links", "collect_joins", "content_hash", "fresh_profiles", "select_nodes",
]
