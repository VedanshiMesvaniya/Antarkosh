"""Table authority and profiles (auto-schema Phase 7): which table holds the real data when similar tables exist.

For every table and view in the catalog this builds one profile: name-marker tags (``test``, ``backup``, ``temp``), the
twin group the table belongs to (``product`` / ``products`` / ``product_bak``), and, inside a group, whether the table
is the ``primary`` (real data), a ``secondary`` (copy), ``equal`` (a series such as ``sales_2023`` / ``sales_2024``) or
``undecided`` (the evidence does not separate them, so nothing is claimed).

Rules that keep this safe:

* Nothing is ever hidden or dropped. Every catalog table and view gets a profile (checked by :func:`check_complete`).
* Nothing is guessed. A primary is named only when it leads by a clear margin, and a table that carries a copy marker
  is never named primary while an unmarked sibling exists.
* Twins must look alike: members of a group need similar column names, so ``lab`` and ``lab_sample`` (different tables
  that share a word) are not twins.
* Metadata only. Signals come from names, keys, row estimates, procedure usage counts and relationship edges already
  on disk. No data row is read, no database is contacted, nothing is executed.

The twin grouping reuses Phase 6's stems and ``twin_key``, so the two phases agree on what a "twin" is.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.sql.relationships.context import COPY_MARKERS, Context, twin_key
from src.sql.relationships.model import CatalogGraph, Table
from src.sql.relationships.names import normalized_column_name, split_identifier

RULESET_VERSION = 2  # bump when a rule or weight changes: it forces a rewrite even if the catalog did not

# Phase 6's copy words plus a few common abbreviations. Built from COPY_MARKERS so additions there flow in here.
EXTRA_MARKERS = frozenset({"bk", "bck", "bckp", "bkup"})
MARKERS = COPY_MARKERS | EXTRA_MARKERS

TAG_OF: dict[str, str] = {
    **dict.fromkeys(("bak", "backup", "bkp", "bk", "bck", "bckp", "bkup", "old", "archive", "archived", "copy", "dup", "dupe"), "backup"),
    **dict.fromkeys(("test", "sample", "demo"), "test"),
    **dict.fromkeys(("tmp", "temp", "trash"), "temp"),
}

# Words that are also ordinary business words (a lab "sample" table, a blood "test" table, an "old" age benefit). They
# become a tag only when a look-alike table without the word exists next to it. The rest tag on the name alone.
WEAK_TAG_WORDS = frozenset({"test", "sample", "demo", "copy", "old", "archive", "archived", "trash"})

SIMILARITY_MIN = 0.9  # column-name overlap (Jaccard) two tables need to count as twins: a copy has (almost) the same columns

# Starting weights. Calibrated against real databases in Phase 10.
W_MARKER = -3.0
W_REFERENCED = {1: 1.0, 3: 2.0}  # distinct tables that point at this one: >=1 -> 1.0, >=3 -> 2.0
W_USED = {1: 1.0, 3: 1.5}  # stored procedures that read or write it: >=1 -> 1.0, >=3 -> 1.5
W_ROWS = 1.0  # clearly the largest member (>= ROWS_RATIO times every other known size)
ROWS_RATIO = 3
W_PK = 0.5
W_FK_OUT = 0.5
LEAD_HIGH = 3.0
LEAD_MEDIUM = 1.0


@dataclass(frozen=True)
class Signals:
    marker: bool
    referenced_by: int
    procedure_use: int
    rows: int | None
    has_primary_key: bool
    declared_fk_out: int


@dataclass(frozen=True)
class Profile:
    qualified: str
    name: str
    schema: str | None
    kind: str
    tags: tuple[str, ...]
    tag_basis: str | None  # "name" | "name+twin"
    group: str | None
    authority: str  # standalone | primary | secondary | equal | undecided
    preferred: str | None  # for a secondary: the qualified name of the primary
    confidence: str | None  # "high" | "medium" for a decided group, else None
    score: float | None
    signals: Signals | None


@dataclass(frozen=True)
class Group:
    id: str
    kind: str  # copy | variant | series
    members: tuple[str, ...]
    primary: str | None
    confidence: str  # high | medium | none
    lead: float | None


@dataclass
class ProfileSet:
    profiles: dict[str, Profile]
    groups: list[Group]
    inferred_used: bool


# ---------------------------------------------------------------- tags


def marker_tags(table: Table, prefixes: frozenset[str], *, backed: bool = False) -> tuple[str, ...]:
    """Tags from copy words in the name. A learned table prefix is the database's own convention, not a marker.

    ``backed`` says a look-alike table without the word exists. Without it, ambiguous words (``WEAK_TAG_WORDS``) are not
    tagged, so ``lab_sample`` (a table of lab samples) is never called a test table.
    """
    toks = split_identifier(table.name)
    if len(toks) < 2:  # a table called just "test" or "old" is left alone
        return ()
    if toks[0] in prefixes:
        toks = toks[1:]
        if len(toks) < 2:
            return ()
    return tuple(sorted({TAG_OF[t] for t in toks if t in TAG_OF and (backed or t not in WEAK_TAG_WORDS)}))


def _stem_flags(stem: tuple[str, ...]) -> tuple[bool, bool]:
    """(has a copy marker, has a digit or version token) for a table stem."""
    marker = len(stem) > 1 and any(t in MARKERS for t in stem)
    digit = any(t.isdigit() for t in stem) or any(t in ("v", "ver", "version") and i + 1 < len(stem) and stem[i + 1].isdigit() for i, t in enumerate(stem))
    return marker, digit


# ---------------------------------------------------------------- evidence from files


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def procedure_use(graph: CatalogGraph, evidence_doc: dict[str, Any] | None) -> dict[str, int]:
    """How many stored procedures read or write each table (``sp_evidence.json``), keyed by qualified name."""
    use: dict[str, int] = {}
    tables = (evidence_doc or {}).get("tables")
    if not isinstance(tables, dict):
        return use
    for name, counts in tables.items():
        table = graph.table(str(name))
        if table is None or not isinstance(counts, dict):
            continue
        try:
            n = int(counts.get("read_in") or 0) + int(counts.get("written_in") or 0)
        except (TypeError, ValueError):
            continue
        use[table.qualified] = use.get(table.qualified, 0) + n
    return use


def fresh_inferred_edges(graph: CatalogGraph, inferred_doc: dict[str, Any] | None) -> list[tuple[str, str]]:
    """(child, parent) qualified names of high-confidence inferred edges, but only if the file was made from these same
    inputs. A stale or foreign file contributes nothing."""
    if not isinstance(inferred_doc, dict) or inferred_doc.get("generated_by") != "auto_relationships":
        return []
    if inferred_doc.get("inputs_hash") != graph.inputs_hash:
        return []
    found: list[tuple[str, str]] = []
    for e in inferred_doc.get("relationships") or []:
        if not isinstance(e, dict) or e.get("band") != "high" or not e.get("direction_known") or e.get("ambiguous"):
            continue
        try:
            child = graph.table(f"{e['from_schema']}.{e['from_table']}" if e.get("from_schema") else e["from_table"])
            parent = graph.table(f"{e['to_schema']}.{e['to_table']}" if e.get("to_schema") else e["to_table"])
        except (KeyError, TypeError):
            continue
        if child is not None and parent is not None and child.qualified != parent.qualified:
            found.append((child.qualified, parent.qualified))
    return found


def referenced_by(graph: CatalogGraph, inferred: list[tuple[str, str]]) -> dict[str, int]:
    """Distinct other tables that point at each table: declared foreign keys plus fresh high-confidence inferred edges."""
    children: dict[str, set[str]] = {}
    for fk in graph.declared_fks:
        if fk.from_table != fk.to_table:
            children.setdefault(fk.to_table, set()).add(fk.from_table)
    for child, parent in inferred:
        children.setdefault(parent, set()).add(child)
    return {q: len(s) for q, s in children.items()}


# ---------------------------------------------------------------- twin groups


def _column_set(t: Table) -> frozenset[str]:
    return frozenset(normalized_column_name(c.name) for c in t.columns)


def _alike(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SIMILARITY_MIN


def _components(members: list[Table]) -> list[list[Table]]:
    """Split same-named tables into sets that also look alike. Each table is compared with the first table of every set
    so far (not with every other table), which keeps a huge series of partitions linear. Members arrive sorted, so the
    result is deterministic."""
    cols = {t.qualified: _column_set(t) for t in members}
    sets: list[list[Table]] = []
    for t in members:
        for group in sets:
            if _alike(cols[group[0].qualified], cols[t.qualified]):
                group.append(t)
                break
        else:
            sets.append([t])
    return [g for g in sets if len(g) >= 2]


def twin_sets(ctx: Context) -> list[tuple[tuple[str, ...], list[Table]]]:
    """Groups of 2+ tables that share a twin key and look alike. Tables only: a view is never a twin."""
    by_key: dict[tuple[str, ...], list[Table]] = {}
    for t in ctx.tables:  # already sorted by qualified name
        key, _ = twin_key(ctx.stems[t.qualified], MARKERS)
        by_key.setdefault(key, []).append(t)
    found: list[tuple[tuple[str, ...], list[Table]]] = []
    for key in sorted(by_key):
        members = by_key[key]
        if len(members) < 2:
            continue
        found.extend((key, comp) for comp in _components(members))
    return found


# ---------------------------------------------------------------- authority


def _step(table: dict[int, float], n: int) -> float:
    """The weight of the highest threshold that ``n`` reaches; 0 below the first."""
    best = 0.0
    for threshold in sorted(table):
        if n >= threshold:
            best = table[threshold]
    return best


def _signals(t: Table, stem: tuple[str, ...], refs: dict[str, int], used: dict[str, int], fk_out: dict[str, int], ctx: Context) -> Signals:
    return Signals(
        marker=_stem_flags(stem)[0],
        referenced_by=refs.get(t.qualified, 0),
        procedure_use=used.get(t.qualified, 0),
        rows=ctx.rows(t),
        has_primary_key=bool(t.primary_key),
        declared_fk_out=fk_out.get(t.qualified, 0),
    )


def _scores(members: list[Table], sig: dict[str, Signals]) -> dict[str, float]:
    known = {t.qualified: sig[t.qualified].rows for t in members if sig[t.qualified].rows is not None}
    biggest: str | None = None
    if len(known) >= 2:
        top = max(known, key=lambda q: (known[q], q))
        if all(known[top] >= ROWS_RATIO * v for q, v in known.items() if q != top):
            biggest = top
    out: dict[str, float] = {}
    for t in members:
        s = sig[t.qualified]
        score = 0.0
        score += W_MARKER if s.marker else 0.0
        score += _step(W_REFERENCED, s.referenced_by)
        score += _step(W_USED, s.procedure_use)
        score += W_ROWS if t.qualified == biggest else 0.0
        score += W_PK if s.has_primary_key else 0.0
        score += W_FK_OUT if s.declared_fk_out else 0.0
        out[t.qualified] = round(score, 2)
    return out


def _decide(members: list[Table], scores: dict[str, float], sig: dict[str, Signals]) -> tuple[str | None, str, float | None]:
    """(primary, confidence, lead). No primary unless one table clearly leads."""
    ranked = sorted(members, key=lambda t: (-scores[t.qualified], t.qualified))
    top, second = ranked[0], ranked[1]
    lead = round(scores[top.qualified] - scores[second.qualified], 2)
    if sig[top.qualified].marker and any(not sig[t.qualified].marker for t in members):
        return None, "none", lead  # a copy-marked table is never primary while an unmarked sibling exists
    if lead >= LEAD_HIGH:
        return top.qualified, "high", lead
    if lead >= LEAD_MEDIUM:
        return top.qualified, "medium", lead
    return None, "none", lead


def build_profiles(
    ctx: Context,
    *,
    evidence_doc: dict[str, Any] | None = None,
    inferred_doc: dict[str, Any] | None = None,
) -> ProfileSet:
    """One profile for every table and view of the catalog."""
    graph = ctx.graph
    used = procedure_use(graph, evidence_doc)
    inferred = fresh_inferred_edges(graph, inferred_doc)
    refs = referenced_by(graph, inferred)
    prefixes = ctx.names.prefixes
    fk_out: Counter[str] = Counter(fk.from_table for fk in graph.declared_fks if fk.from_table != fk.to_table)

    in_group: dict[str, tuple[str, str]] = {}  # qualified -> (group id, kind)
    groups: list[Group] = []
    group_of: dict[str, Group] = {}
    decided: dict[str, tuple[float, Signals]] = {}
    seen_ids: Counter[str] = Counter()
    unmarked: dict[str, int] = {}  # group id -> members without a copy marker

    for key, members in twin_sets(ctx):
        flags = [_stem_flags(ctx.stems[t.qualified]) for t in members]
        kind = "copy" if any(m for m, _ in flags) else "series" if any(d for _, d in flags) else "variant"
        base = "twin:" + "_".join(key)
        seen_ids[base] += 1
        gid = base if seen_ids[base] == 1 else f"{base}#{seen_ids[base]}"
        sig = {t.qualified: _signals(t, ctx.stems[t.qualified], refs, used, fk_out, ctx) for t in members}
        scores = _scores(members, sig)
        if kind == "series":
            primary, confidence, lead = None, "none", None
        else:
            primary, confidence, lead = _decide(members, scores, sig)
        group = Group(gid, kind, tuple(t.qualified for t in members), primary, confidence, lead)
        groups.append(group)
        unmarked[gid] = sum(1 for t in members if not sig[t.qualified].marker)
        for t in members:
            in_group[t.qualified] = (gid, kind)
            group_of[t.qualified] = group
            decided[t.qualified] = (scores[t.qualified], sig[t.qualified])

    profiles: dict[str, Profile] = {}
    for q in sorted(graph.tables):
        t = graph.tables[q]
        grouped = q in in_group
        backed = grouped and unmarked[group_of[q].id] > 0  # an unmarked look-alike exists: the copy word is backed by a real counterpart
        tags = marker_tags(t, prefixes, backed=backed)
        tag_basis = ("name+twin" if backed else "name") if tags else None
        if not grouped:
            profiles[q] = Profile(q, t.name, t.schema, t.kind, tags, tag_basis, None, "standalone", None, None, None, None)
            continue
        gid, kind = in_group[q]
        group = group_of[q]
        score, sig_q = decided[q]
        if kind == "series":
            authority, preferred, conf = "equal", None, None
        elif group.primary is None:
            authority, preferred, conf = "undecided", None, None
        elif group.primary == q:
            authority, preferred, conf = "primary", None, group.confidence
        else:
            authority, preferred, conf = "secondary", group.primary, group.confidence
        profiles[q] = Profile(q, t.name, t.schema, t.kind, tags, tag_basis, gid, authority, preferred, conf, score, sig_q)
    return ProfileSet(profiles=profiles, groups=groups, inferred_used=bool(inferred))


def check_complete(graph: CatalogGraph, result: ProfileSet) -> list[str]:
    """The never-hide gate: every table and view of the catalog has exactly one profile, and nothing else does."""
    problems: list[str] = []
    missing = sorted(set(graph.tables) - set(result.profiles))
    extra = sorted(set(result.profiles) - set(graph.tables))
    if missing:
        problems.append(f"{len(missing)} catalog tables have no profile")
    if extra:
        problems.append(f"{len(extra)} profiles are not in the catalog")
    for g in result.groups:
        if g.primary is not None and g.primary not in g.members:
            problems.append(f"group {g.id} names a primary that is not a member")
    return problems


def summary(result: ProfileSet) -> dict[str, Any]:
    """Counts only. This is what reaches the sync API response."""
    profiles = result.profiles.values()
    tags: Counter[str] = Counter(tag for p in profiles for tag in p.tags)
    kinds: Counter[str] = Counter(g.kind for g in result.groups)
    authority: Counter[str] = Counter(p.authority for p in profiles)
    return {
        "tables": sum(1 for p in profiles if p.kind.lower() not in ("view", "materialized view")),
        "views": sum(1 for p in profiles if p.kind.lower() in ("view", "materialized view")),
        "tagged": {k: tags[k] for k in sorted(tags)},
        "groups": len(result.groups),
        "group_kinds": {k: kinds[k] for k in sorted(kinds)},
        "decided": sum(1 for g in result.groups if g.primary is not None),
        "undecided": authority["undecided"],
        "secondary": authority["secondary"],
        "inferred_edges_used": result.inferred_used,
    }


__all__ = [
    "EXTRA_MARKERS", "MARKERS", "RULESET_VERSION", "TAG_OF", "WEAK_TAG_WORDS", "Group", "Profile", "ProfileSet", "Signals",
    "build_profiles", "check_complete", "fresh_inferred_edges", "marker_tags", "procedure_use", "referenced_by", "summary", "twin_sets",
]
