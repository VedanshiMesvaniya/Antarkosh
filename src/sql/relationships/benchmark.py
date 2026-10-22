"""Measure how well the rules recover relationships (auto-schema Phase 6, step 6i).

A database that declares its foreign keys is a free answer sheet: hide the keys, run the rules on what is
left (names, types, keys, indexes), and count how many of the hidden keys come back. Anything the rules
propose that the database does not declare is listed, so a person can judge it: an undeclared edge is not
automatically wrong (a partitioned parent or a legacy table often has no declared keys).

Pure and offline: it reads ``schema.json`` / ``table_inventory.json`` documents and never writes or connects.
The numbers it prints are what sets the thresholds in ``writer.py``; ``tests/sql/test_relationships_benchmark.py``
pins them so a rule change that loses recall or adds noise fails loudly.

Origin: new (no previous file)
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.sql.relationships.candidates import Edge
from src.sql.relationships.context import prepare
from src.sql.relationships.detectors import detect
from src.sql.relationships.gates import resolve
from src.sql.relationships.model import CatalogGraph, graph_from_documents, load_graph
from src.sql.relationships.writer import HIGH_THRESHOLD, MAX_EDGES_PER_TABLE, band, select_applied

Pair = tuple[str, str, str, str]  # (child table, child column, parent table, parent column), lower case

DEFAULT_THRESHOLDS = (0.45, 0.50, 0.60, 0.65, 0.70)


def _q(schema: str | None, table: str) -> str:
    return f"{schema}.{table}".lower() if schema else table.lower()


def filter_tables(schema_doc: dict[str, Any], exclude: str | None) -> dict[str, Any]:
    """Drop tables whose name matches ``exclude`` (for example monthly partitions) and any key that touches them."""
    if not exclude:
        return copy.deepcopy(schema_doc)
    rx = re.compile(exclude)
    doc = copy.deepcopy(schema_doc)
    doc["tables"] = [t for t in doc.get("tables", []) if not rx.search(t["name"])]
    doc["relationships"] = [r for r in doc.get("relationships", []) if not rx.search(r["from_table"]) and not rx.search(r["to_table"])]
    return doc


def truth_pairs(schema_doc: dict[str, Any]) -> set[Pair]:
    """The foreign keys the database declares."""
    return {
        (_q(r.get("from_schema"), r["from_table"]), r["from_column"].lower(), _q(r.get("to_schema"), r["to_table"]), r["to_column"].lower())
        for r in schema_doc.get("relationships", [])
    }


def hide_foreign_keys(schema_doc: dict[str, Any]) -> dict[str, Any]:
    """A copy of the catalog with every declared foreign key removed, as if the database had never declared one."""
    doc = copy.deepcopy(schema_doc)
    doc["relationships"] = []
    for t in doc.get("tables", []):
        for c in t.get("columns", []):
            c["is_foreign_key"] = False
    return doc


def edge_pair(graph: CatalogGraph, e: Edge) -> Pair:
    child, parent = graph.tables[e.child], graph.tables[e.parent]
    return (_q(child.schema, child.name), e.child_column.lower(), _q(parent.schema, parent.name), e.parent_column.lower())


@dataclass
class Metrics:
    threshold: float
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0


@dataclass
class CaseResult:
    name: str
    tables: int
    truth: set[Pair]
    predicted: dict[Pair, Edge]
    gate_counts: dict[str, int] = field(default_factory=dict)

    def at(self, threshold: float) -> Metrics:
        kept = {p for p, e in self.predicted.items() if e.score >= threshold}
        return Metrics(threshold, len(kept & self.truth), len(kept - self.truth), len(self.truth - kept))

    @property
    def missed(self) -> list[Pair]:
        return sorted(self.truth - set(self.predicted))

    @property
    def extra(self) -> list[Pair]:
        return sorted(set(self.predicted) - self.truth)

    def by_rule(self) -> dict[str, tuple[int, int]]:
        """For every rule that fired: (edges it took part in that the database declares, edges it took part in that it does not)."""
        out: dict[str, list[int]] = {}
        for pair, e in self.predicted.items():
            for rule in e.evidence:
                out.setdefault(rule, [0, 0])[0 if pair in self.truth else 1] += 1
        return {r: (a, b) for r, (a, b) in sorted(out.items())}


def run_case(
    name: str,
    schema_doc: dict[str, Any],
    inventory_doc: dict[str, Any] | None = None,
    *,
    exclude: str | None = None,
    hide: bool = True,
) -> CaseResult:
    """Run the rules on one catalog. With ``hide`` the declared keys are the answer sheet; without it they count as evidence."""
    doc = filter_tables(schema_doc, exclude)
    truth = truth_pairs(doc)
    graph = graph_from_documents(hide_foreign_keys(doc) if hide else doc, inventory_doc, None, db_id=name)
    if graph is None:
        raise ValueError(f"{name}: schema document has no tables")
    ctx = prepare(graph)
    edges, report = resolve(ctx, detect(ctx))
    return CaseResult(name, len(ctx.tables), truth, {edge_pair(graph, e): e for e in edges}, report.as_counts())


def live_check(
    db_id: str, databases_dir: Path | None = None, *, threshold: float = HIGH_THRESHOLD, max_per_table: int = MAX_EDGES_PER_TABLE
) -> dict[str, Any] | None:
    """What the phase would do for ``db_id`` right now, from the files on disk. Writes nothing. None if there is no ``schema.json``."""
    graph = load_graph(db_id, databases_dir)
    if graph is None:
        return None
    ctx = prepare(graph)
    edges, report = resolve(ctx, detect(ctx))
    chosen, held = select_applied(graph, edges, threshold=threshold, max_per_table=max_per_table)
    bands = Counter(band(e.score, threshold) for e in edges)
    return {
        "tables": len(ctx.tables),
        "declared_keys": len(graph.declared_fks),
        "procedure_joins": len(graph.proc_edges),
        "multi_schema": graph.multi_schema,
        "answering_scope_known": graph.scope_known,
        "edges": len(edges),
        "high": bands["high"], "medium": bands["medium"], "low": bands["low"],
        "publishable": len(chosen),
        "held_back": dict(sorted(held.items())),
        "direction_unknown": report.direction_unknown,
        "ambiguous": report.ambiguous,
        "dropped": dict(sorted(report.dropped.items())),
    }


def total(results: list[CaseResult], threshold: float) -> Metrics:
    ms = [r.at(threshold) for r in results]
    return Metrics(threshold, sum(m.tp for m in ms), sum(m.fp for m in ms), sum(m.fn for m in ms))


def format_report(results: list[CaseResult], thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS, *, details: bool = False) -> str:
    lines: list[str] = []
    for r in results:
        lines.append(f"\n{r.name}: {r.tables} tables, {len(r.truth)} declared keys hidden, {len(r.predicted)} edges proposed")
        lines.append("  threshold  recall  precision  found  not-declared  missed")
        for t in thresholds:
            m = r.at(t)
            lines.append(f"  {t:9.2f}  {m.recall:6.0%}  {m.precision:9.0%}  {m.tp:5d}  {m.fp:12d}  {m.fn:6d}")
        if details:
            lines += [f"  MISSED   {p[0]}.{p[1]} -> {p[2]}.{p[3]}" for p in r.missed]
            lines += [f"  PROPOSED {p[0]}.{p[1]} -> {p[2]}.{p[3]}  score={r.predicted[p].score:.3f}  {sorted(r.predicted[p].evidence)}" for p in r.extra]
    if len(results) > 1:
        lines.append("\nall databases together")
        lines.append("  threshold  recall  precision  found  not-declared  missed")
        for t in thresholds:
            m = total(results, t)
            lines.append(f"  {t:9.2f}  {m.recall:6.0%}  {m.precision:9.0%}  {m.tp:5d}  {m.fp:12d}  {m.fn:6d}")
    rules: Counter[str] = Counter()
    declared: Counter[str] = Counter()
    for r in results:
        for rule, (a, b) in r.by_rule().items():
            declared[rule] += a
            rules[rule] += a + b
    lines.append("\nper rule: edges the rule took part in, and how many of those the database declares")
    lines += [f"  {rule:14s} {rules[rule]:4d} edges, {declared[rule]:4d} declared" for rule in sorted(rules)]
    return "\n".join(lines)


__all__ = [
    "CaseResult", "DEFAULT_THRESHOLDS", "Metrics", "Pair", "edge_pair", "filter_tables", "format_report",
    "hide_foreign_keys", "live_check", "run_case", "total", "truth_pairs",
]
