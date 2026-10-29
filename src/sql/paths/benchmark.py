"""Measure the route search and the bridge floor (auto-schema Phase 10, part 1).

Phase 8 scores every multi-join route; the prompt planner (``route_index.py``) keeps only routes above its own floor. Those
floors were chosen, not measured. This module measures them, and changes nothing: no thresholds are touched, no file is
written, no database is opened.

Two measurements:

* ``run_route_case`` - on a catalog that declares its foreign keys (a free answer sheet). The routes found from the declared
  keys alone are the truth. Then every key is hidden, the Phase 6 rules infer edges from names and types, Phase 8 searches
  routes over those inferred edges, and each truth route is looked up: is the same route found, and with what score? Counting
  the truth routes whose score clears each floor shows what a floor costs in recall. Routes found between pairs that have no
  truth route are counted as extras.
* ``live_floor_table`` - on a real ``join_paths.json``: for each route length, how many stored routes clear each floor. This is
  the number that says how many routes the planner's floor throws away on a database that declares no keys at all.

Pure and offline. Numbers measured on small public catalogs show that the rules generalise; they cannot calibrate hub damping
or the work caps (almost no table in a small schema has more than ``hub_free`` links, and ``hubs`` in the report says so).

Origin: new (no previous file)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from src.sql.paths.links import build_links, collect_joins, select_nodes
from src.sql.paths.search import Params, find_paths
from src.sql.relationships.benchmark import filter_tables, hide_foreign_keys
from src.sql.relationships.context import prepare
from src.sql.relationships.detectors import detect
from src.sql.relationships.gates import resolve
from src.sql.relationships.model import CatalogGraph, graph_from_documents
from src.sql.relationships.writer import HIGH_THRESHOLD, inferred_document, select_applied

DEFAULT_FLOORS = (0.30, 0.40, 0.45, 0.50, 0.60)
DEFAULT_EDGE_THRESHOLDS = (0.45, 0.60, 0.70)  # which inferred edges Phase 8 may use: 0.70 is what the apply flag publishes
HOPS = (2, 3, 4, 5)  # joins in a route; a pair that joins directly (1) needs no route

Scored = dict[tuple[str, str], tuple[tuple[str, ...], float]]  # pair -> (best route's tables, its score)


def routes_for(graph: CatalogGraph, *, inferred_doc: dict[str, Any] | None = None, params: Params | None = None) -> tuple[Scored, int]:
    """Best route per pair that Phase 8 would store, and how many tables have more links than ``hub_free``."""
    p = params or Params()
    inputs = select_nodes(graph, None)
    adj = build_links(collect_joins(graph, inputs, relationships_doc=None, inferred_doc=inferred_doc))
    pairs, _stats = find_paths(adj, inputs.blocked_as_bridge, p)
    hubs = sum(1 for neighbours in adj.values() if len(neighbours) > p.hub_free)
    return {key: (routes[0].tables, routes[0].score) for key, routes in pairs.items() if routes}, hubs


def _hops(tables: tuple[str, ...]) -> int:
    return len(tables) - 1


@dataclass
class FloorRow:
    """What one floor does to one route length."""

    floor: float
    hops: int
    truth: int = 0  # truth routes of this length
    exact: int = 0  # ... found with the same tables at or above the floor
    same_pair: int = 0  # ... whose pair is found at or above the floor by any route
    extra: int = 0  # routes found at or above the floor between pairs that have no truth route

    @property
    def recall(self) -> float:
        return self.exact / self.truth if self.truth else 1.0


@dataclass
class RouteCase:
    name: str
    tables: int
    truth: Scored
    found: Scored
    hubs_truth: int = 0
    hubs_found: int = 0
    params: Params = field(default_factory=Params)
    edge_threshold: float = HIGH_THRESHOLD
    edges_admitted: int = 0  # inferred edges at or above ``edge_threshold`` that Phase 8 could use

    def at(self, floor: float) -> list[FloorRow]:
        rows = {h: FloorRow(floor, h) for h in HOPS}
        for pair, (tables, _score) in self.truth.items():
            row = rows.get(_hops(tables))
            if row is None:
                continue
            row.truth += 1
            got = self.found.get(pair)
            if got is not None and got[1] >= floor:
                row.same_pair += 1
                if got[0] == tables:
                    row.exact += 1
        for pair, (tables, score) in self.found.items():
            row = rows.get(_hops(tables))
            if row is not None and pair not in self.truth and score >= floor:
                row.extra += 1
        return [rows[h] for h in HOPS]

    def exact_scores(self, hops: int) -> list[float]:
        """Scores, over the inferred edges, of truth routes of this length that were found with the same tables."""
        out = []
        for pair, (tables, _s) in self.truth.items():
            got = self.found.get(pair)
            if _hops(tables) == hops and got is not None and got[0] == tables:
                out.append(got[1])
        return sorted(out)


def run_route_case(
    name: str,
    schema_doc: dict[str, Any],
    inventory_doc: dict[str, Any] | None = None,
    *,
    exclude: str | None = None,
    params: Params | None = None,
    edge_threshold: float = HIGH_THRESHOLD,
) -> RouteCase:
    """Truth routes from the declared keys; found routes from edges the Phase 6 rules infer once those keys are hidden.

    ``edge_threshold`` is the score an inferred edge needs before Phase 8 may use it (the apply flag publishes at 0.70). On the
    public catalogs the rules alone never reach 0.70, so at that value routes come only from evidence beyond names and types.
    """
    p = params or Params()
    doc = filter_tables(schema_doc, exclude)
    truth_graph = graph_from_documents(doc, inventory_doc, None, db_id=name)
    hidden_graph = graph_from_documents(hide_foreign_keys(doc), inventory_doc, None, db_id=name)
    if truth_graph is None or hidden_graph is None:
        raise ValueError(f"{name}: schema document has no tables")
    truth, hubs_truth = routes_for(truth_graph, params=p)

    ctx = prepare(hidden_graph)
    edges, report = resolve(ctx, detect(ctx))
    applied, _held = select_applied(hidden_graph, edges, threshold=edge_threshold)
    inferred, _dropped = inferred_document(hidden_graph, edges, report.as_counts(), "benchmark", edge_threshold, {e.key for e in applied})
    found, hubs_found = routes_for(hidden_graph, inferred_doc=inferred, params=p)
    return RouteCase(name, len(ctx.tables), truth, found, hubs_truth, hubs_found, p, edge_threshold, len(applied))


# ---------------------------------------------------------------- a real join_paths.json


def live_floor_table(paths_doc: dict[str, Any], floors: Iterable[float] = DEFAULT_FLOORS) -> dict[str, Any]:
    """For each route length: routes stored, and how many clear each floor. Reads the best route of every pair."""
    floors = tuple(floors)
    by_hops: dict[int, list[float]] = {h: [] for h in HOPS}
    shapes: dict[str, int] = {}
    entries = paths_doc.get("pairs") if isinstance(paths_doc, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        routes = entry.get("routes") if isinstance(entry, dict) else None
        best = routes[0] if isinstance(routes, list) and routes and isinstance(routes[0], dict) else None
        tables = best.get("tables") if best else None
        if not isinstance(tables, list) or len(tables) < 3:
            continue
        try:
            score = float(best.get("score", entry.get("score", 0.0)))
        except (TypeError, ValueError):
            continue
        shape = str(best.get("shape") or "unknown").lower()
        shapes[shape] = shapes.get(shape, 0) + 1
        if len(tables) - 1 in by_hops:
            by_hops[len(tables) - 1].append(score)
    rows = []
    for hops, scores in by_hops.items():
        rows.append({
            "hops": hops,
            "routes": len(scores),
            "median_score": round(median(scores), 3) if scores else None,
            "kept": {f"{f:.2f}": sum(1 for s in scores if s >= f) for f in floors},
        })
    return {"floors": list(floors), "rows": rows, "shapes": dict(sorted(shapes.items()))}


# ---------------------------------------------------------------- text report


def format_route_report(cases: list[RouteCase], floors: Iterable[float] = DEFAULT_FLOORS, *, details: bool = False) -> str:
    """One block per inferred-edge threshold present in ``cases``; within it, per catalog (with ``details``) and in total."""
    floors = tuple(floors)
    lines: list[str] = []
    for threshold in sorted({c.edge_threshold for c in cases}):
        group = [c for c in cases if c.edge_threshold == threshold]
        admitted = sum(c.edges_admitted for c in group)
        lines.append(f"=== inferred edges admitted at score >= {threshold:.2f}: {admitted} edges over {len(group)} catalogs ===")
        for case in group:
            lines.append(f"{case.name}: {case.tables} tables, {len(case.truth)} truth routes (2-5 joins), {len(case.found)} found, {case.edges_admitted} edges admitted")
            if details:
                lines.append(f"  tables with more than {case.params.hub_free} links: truth {case.hubs_truth}, inferred {case.hubs_found}")
                lines.append("  floor  joins  truth  same-route  same-pair  extra  recall")
                for floor in floors:
                    for row in case.at(floor):
                        if row.truth or row.extra:
                            lines.append(f"  {floor:.2f}   {row.hops}      {row.truth:<5d}  {row.exact:<10d}  {row.same_pair:<9d}  {row.extra:<5d}  {row.recall:.2f}")
                for hops in HOPS:
                    scores = case.exact_scores(hops)
                    if scores:
                        lines.append(f"  scores of same-route matches, {hops} joins: min {scores[0]:.2f}  median {median(scores):.2f}  max {scores[-1]:.2f}  (n={len(scores)})")
        total = {f: [FloorRow(f, h) for h in HOPS] for f in floors}
        for case in group:
            for f in floors:
                for acc, row in zip(total[f], case.at(f), strict=True):
                    acc.truth += row.truth
                    acc.exact += row.exact
                    acc.same_pair += row.same_pair
                    acc.extra += row.extra
        lines.append("same-route recall by floor and route length, all catalogs  (found/truth)")
        lines.append("  floor  " + "  ".join(f"{h} joins".rjust(12) for h in HOPS) + "   extras")
        for f in floors:
            cells = [(f"{r.recall:.2f} ({r.exact}/{r.truth})" if r.truth else "-").rjust(12) for r in total[f]]
            lines.append(f"  {f:.2f}   " + "  ".join(cells) + f"   {sum(r.extra for r in total[f])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_live_table(table: dict[str, Any]) -> str:
    floors = [f"{f:.2f}" for f in table["floors"]]
    lines = ["routes stored in join_paths.json, and how many clear each floor", "  joins  routes  median  " + "  ".join(f">={f}" for f in floors)]
    for row in table["rows"]:
        kept = "  ".join(f"{row['kept'][f]:>5d}" for f in floors)
        med = f"{row['median_score']:.2f}" if row["median_score"] is not None else "  - "
        lines.append(f"  {row['hops']}      {row['routes']:<6d}  {med:<6s}  {kept}")
    lines.append("shapes: " + (", ".join(f"{k} {v}" for k, v in table["shapes"].items()) or "none"))
    return "\n".join(lines)


__all__ = [
    "DEFAULT_EDGE_THRESHOLDS",
    "DEFAULT_FLOORS",
    "FloorRow",
    "RouteCase",
    "format_live_table",
    "format_route_report",
    "live_floor_table",
    "routes_for",
    "run_route_case",
]
