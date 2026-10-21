"""Gates, direction and ambiguity (auto-schema Phase 6, step 6d: steps 16 to 22 of the 23).

Takes the candidates from 6c, groups the evidence for each column pair into an :class:`Edge`, and then:

  16  type gate        drop an edge whose column types cannot match (a declared foreign key is a fact and is exempt)
  17  parent-key gate  the target must be a primary / unique key, unless the database or a procedure vouches for it
  18  boost            the child column is indexed                                   (+0.05)
  19  boost            a column or table comment names the parent                    (+0.15)
  20  direction        when a detector could not tell parent from child: names first, then row counts
  21  cardinality      many_to_one, one_to_one or shared_key
  22  ambiguity        generic names never join alone; twin / backup tables lose; at most 3 parents per column

Nothing is scored, thresholded or written here (6e). Every drop is counted by reason in :class:`GateReport`, so
the phase report can say what was filtered and why. Declared foreign keys are never dropped by any step.

Origin: new (no previous file)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from src.sql.relationships.candidates import WEIGHTS, Candidate, Edge
from src.sql.relationships.context import Context
from src.sql.relationships.model import Table, types_compatible
from src.sql.relationships.names import normalized_column_name, parse_id_column, singular

MAX_PARENTS_PER_COLUMN = 3
_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class GateReport:
    edges_in: int = 0
    edges_out: int = 0
    dropped: Counter[str] = field(default_factory=Counter)  # reason -> edges
    direction_unknown: int = 0
    ambiguous: int = 0

    def as_counts(self) -> dict[str, int]:
        return {"edges_in": self.edges_in, "edges_out": self.edges_out, "direction_unknown": self.direction_unknown, "ambiguous": self.ambiguous, **{f"dropped_{k}": v for k, v in sorted(self.dropped.items())}}


# ---------------------------------------------------------------- grouping


def group(candidates: list[Candidate]) -> dict[tuple[str, str, str, str], Edge]:
    """One edge per directed column pair, holding the best weight of every rule that fired for it."""
    edges: dict[tuple[str, str, str, str], Edge] = {}
    for c in candidates:
        edge = edges.get(c.key)
        if edge is None:
            edge = edges[c.key] = Edge(c.child, c.child_column, c.parent, c.parent_column, symmetric_only=True)
        edge.add(c.rule, c.weight)
        if not c.symmetric:
            edge.symmetric_only = False
    return edges


def _sides(ctx: Context, e: Edge):
    child, parent = ctx.graph.tables.get(e.child), ctx.graph.tables.get(e.parent)
    if child is None or parent is None:
        return None
    cc, pc = child.column(e.child_column), parent.column(e.parent_column)
    return (child, cc, parent, pc) if cc is not None and pc is not None else None


# ---------------------------------------------------------------- 16, 17


def _type_gate(ctx: Context, edges: dict, report: GateReport) -> None:
    for key, e in list(edges.items()):
        s = _sides(ctx, e)
        if s is None:
            del edges[key]
            report.dropped["unknown_column"] += 1
        elif "declared" not in e.evidence and not types_compatible(s[1], s[3]):
            del edges[key]
            report.dropped["type"] += 1


def _parent_key_gate(ctx: Context, edges: dict, report: GateReport) -> None:
    for key, e in list(edges.items()):
        if e.has_hard_evidence:
            continue
        parent = ctx.graph.tables[e.parent]
        if parent.key_strength(e.parent_column) is None:
            del edges[key]
            report.dropped["parent_key"] += 1


# ---------------------------------------------------------------- 18, 19


def _words(text: str | None) -> set[str]:
    return {singular(w) for w in _WORD.findall((text or "").lower())}


def _boosts(ctx: Context, edges: dict) -> None:
    for e in edges.values():
        child, _cc, _parent, _pc = _sides(ctx, e)  # type: ignore[misc]
        if child.is_indexed(e.child_column):
            e.add("indexed", WEIGHTS["indexed"])
        stem = ctx.stems.get(e.parent)
        if not stem:
            continue
        col = child.column(e.child_column)
        if any(set(stem) <= _words(text) for text in (col.comment if col else None, child.comment)):
            e.add("comment", WEIGHTS["comment"])


# ---------------------------------------------------------------- 20, 21


def _points(ctx: Context, column: str, table: Table) -> bool:
    """Is the column named after this table (``customer_id`` after ``customer``)?"""
    p = parse_id_column(column)
    return bool(p and p.stem and p.stem == ctx.stems.get(table.qualified))


def _orient_symmetric(ctx: Context, edges: dict) -> dict:
    """Step 20. For edges whose detector could not tell parent from child: names first, then row counts, else unknown."""
    keep = {k: e for k, e in edges.items() if not e.symmetric_only}
    for key, e in sorted(edges.items()):
        if not e.symmetric_only:
            continue
        child, _cc, parent, _pc = _sides(ctx, e)  # type: ignore[misc]
        flip = None
        points_down = _points(ctx, e.child_column, parent)  # the child column is named after the parent table
        points_up = _points(ctx, e.parent_column, child)
        if points_down != points_up:
            flip = points_up
        else:
            rc, rp = ctx.rows(child), ctx.rows(parent)
            if rc is not None and rp is not None and rc != rp:
                flip = rp > rc  # the table with more rows is the "many" side, so the child
        if flip is None:
            e.direction_known = False
        if flip:
            e = Edge(e.parent, e.parent_column, e.child, e.child_column, dict(e.evidence), True, e.direction_known)
        existing = keep.get(e.key)
        if existing is None:
            keep[e.key] = e
        else:
            existing.merge(e)
    return keep


def _merge_opposites(edges: dict, report: GateReport) -> dict:
    """The same column pair found in both directions: the better supported one wins; a tie is dropped (no guessing)."""
    out = dict(edges)
    for key, e in sorted(edges.items()):
        rev = (e.parent, e.parent_column, e.child, e.child_column)
        other = out.get(rev)
        if key not in out or other is None or rev == key:
            continue
        if not e.direction_known and other.direction_known:
            other.merge(e)
            del out[key]
        elif not other.direction_known and e.direction_known:
            e.merge(other)
            del out[rev]
        elif e.score == other.score:
            if "declared" in e.evidence or "declared" in other.evidence:
                continue  # a declared foreign key is never dropped
            del out[key], out[rev]
            report.dropped["direction_conflict"] += 2
        else:
            loser = rev if e.score > other.score else key
            del out[loser]
            report.dropped["direction_conflict"] += 1
    return out


def _cardinality(ctx: Context, edges: dict) -> None:
    for e in edges.values():
        child = ctx.graph.tables[e.child]
        strength = child.key_strength(e.child_column)
        e.cardinality = "shared_key" if strength == "primary" else "one_to_one" if strength else "many_to_one"


# ---------------------------------------------------------------- 22


def _generic_gate(ctx: Context, edges: dict, report: GateReport) -> None:
    """Two columns called ``code`` or ``status`` are not a reference. Applies when the name is the only evidence.

    An index on the column is not independent evidence, so it does not rescue the edge; a comment, a junction or
    header-detail shape, a procedure or a declared key does.
    """
    for key, e in list(edges.items()):
        if set(e.evidence) <= {"same_name", "indexed"} and (normalized_column_name(e.child_column) in ctx.common or normalized_column_name(e.parent_column) in ctx.common):
            del edges[key]
            report.dropped["generic_name"] += 1


def _twin_gate(ctx: Context, edges: dict, report: GateReport) -> None:
    """When one column could point at ``product`` or ``products`` or ``product_bak``, keep the one that holds the real data."""
    by_col: dict[tuple[str, str], list[Edge]] = {}
    for e in edges.values():
        by_col.setdefault((e.child, e.child_column), []).append(e)
    for group_ in by_col.values():
        twins: dict[tuple[str, ...], list[Edge]] = {}
        for e in group_:
            twins.setdefault(ctx.twin[e.parent][0], []).append(e)
        for members in twins.values():
            if len({e.parent for e in members}) < 2:
                continue

            def signal(e: Edge) -> tuple:
                rows = ctx.rows(ctx.graph.tables[e.parent])
                return (e.has_hard_evidence, not ctx.twin[e.parent][1], rows if rows is not None else -1)

            ranked = sorted(members, key=signal, reverse=True)
            best = signal(ranked[0])
            winners = [e for e in ranked if signal(e) == best]
            for e in ranked:
                if signal(e) != best and "declared" not in e.evidence:
                    del edges[e.key]
                    report.dropped["twin"] += 1
            if len(winners) > 1:  # nothing tells these apart: keep them and say so
                for e in winners:
                    e.ambiguous = True


def _cap_parents(edges: dict, report: GateReport) -> None:
    by_col: dict[tuple[str, str], list[Edge]] = {}
    for e in edges.values():
        by_col.setdefault((e.child, e.child_column), []).append(e)
    for members in by_col.values():
        if len({e.parent for e in members}) > 1:
            for e in members:
                e.ambiguous = True
        soft = sorted((e for e in members if not e.has_hard_evidence), key=lambda e: (-e.score, e.parent, e.parent_column))
        hard = len(members) - len(soft)
        for e in soft[max(MAX_PARENTS_PER_COLUMN - hard, 0):]:
            del edges[e.key]
            report.dropped["over_cap"] += 1


# ---------------------------------------------------------------- entry point


def resolve(ctx: Context, candidates: list[Candidate]) -> tuple[list[Edge], GateReport]:
    """Run steps 16 to 22. Returns the surviving edges in a fixed order, and what was dropped and why."""
    edges = group(candidates)
    report = GateReport(edges_in=len(edges))
    _type_gate(ctx, edges, report)  # 16
    _parent_key_gate(ctx, edges, report)  # 17
    edges = _orient_symmetric(ctx, edges)  # 20 (runs before the boosts so they are read from the final child)
    _boosts(ctx, edges)  # 18, 19
    edges = _merge_opposites(edges, report)
    _cardinality(ctx, edges)  # 21
    _generic_gate(ctx, edges, report)  # 22
    _twin_gate(ctx, edges, report)
    _cap_parents(edges, report)
    out = sorted(edges.values(), key=lambda e: e.key)
    report.edges_out = len(out)
    report.direction_unknown = sum(1 for e in out if not e.direction_known)
    report.ambiguous = sum(1 for e in out if e.ambiguous)
    return out, report


__all__ = ["MAX_PARENTS_PER_COLUMN", "GateReport", "group", "resolve"]
