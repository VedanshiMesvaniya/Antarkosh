"""Relationship detectors (auto-schema Phase 6, step 6c: steps 4 to 15 of the 23).

One function per rule. Each takes the prepared :class:`Context` and returns :class:`Candidate` objects: "this
column may reference that column", with a rule code and a weight. A detector only proposes. It never drops a
guess for being wrong (that is the gates' job in 6d) and never scores (6e). Views are never a side of a candidate.

Rule       Step  Reads
declared    4    foreign keys declared in schema.json
proc        5    join edges mined from stored procedures (sp_evidence.json)
table_id    6    ``customer_id``  -> table ``customer`` (also CustomerID, tbl_customers, id_customer, fk_party)
same_name   7    a column named like a single-column key of another table (pk_name: the same, for a name found in many tables)
abbrev      8    ``cust_id``      -> table ``customer`` (only when exactly one table fits)
role_prefix 9    ``billing_address_id`` -> table ``address``
self_ref   10    ``parent_id``    -> the table's own key
code_like  11    ``po_number``    -> the unique column ``po_no`` of table ``po``
junction   14    a composite key whose parts are references (boosts the edges of step 6 to 11)
header_detail 15 a composite key made of a parent key plus a line number (boosts that edge)

Steps 12 and 13 (learned prefixes, case and plural variants) live in ``context.prepare``: every stem is built
with them, so every name rule gets them.

Origin: new (no previous file)
"""

from __future__ import annotations

from collections.abc import Callable

from src.sql.relationships.candidates import WEIGHTS, Candidate
from src.sql.relationships.context import Context
from src.sql.relationships.model import Column, Table
from src.sql.relationships.names import (
    ID_SUFFIXES,
    IdColumn,
    normalized_column_name,
    parse_id_column,
    split_identifier,
)

REF_SUFFIXES = frozenset({"id", "fk"})  # suffixes that mean "the key of another table"
CODE_SUFFIXES = ID_SUFFIXES - REF_SUFFIXES  # no, num, number, code, key, ref

# Generic English words that mean "the same kind of thing, one level up". These are the only fixed lists in 6c.
HIERARCHY_WORDS = frozenset({"parent", "ancestor", "superior", "root", "prev", "previous"})
ROLE_WORDS = frozenset({"manager", "supervisor", "boss", "lead"})  # a person table points at itself; a department table does not

MAX_KEY_HOLDERS = 5  # a name that is a key in more than this many tables is not a reference name
_LINE_TOKENS = frozenset({"line", "lines", "seq", "sequence", "sl", "sr", "sno", "slno", "srno", "row", "item", "pos", "position", "idx", "index", "serial", "no", "num", "number", "lineno", "itemno", "seqno"})
_KEY_SCORE = {"primary": 3, "unique": 2, "unique_index": 2}


# ---------------------------------------------------------------- helpers


def _ref_column(c: Column) -> IdColumn | None:
    """``customer_id`` -> IdColumn(stem=(customer,), suffix=id). None unless it names something and ends in id / fk."""
    p = parse_id_column(c.name)
    return p if p and p.stem and p.suffix in REF_SUFFIXES else None


def _role_targets(ctx: Context, table: Table, stem: tuple[str, ...]) -> list[Table]:
    """Tables the stem points at once leading role words are dropped: (billing, address) -> address. Longest tail wins."""
    for i in range(1, len(stem)):
        found = [x for x in ctx.names.exact(stem[i:]) if x.qualified != table.qualified]
        if found:
            return ctx.prefer_same_schema(table, found)
    return []


def _resolved_by_name(ctx: Context, table: Table, stem: tuple[str, ...]) -> bool:
    """Does a stricter name rule (6 or 9) already account for this column? Used so the loose rule 8 stays quiet."""
    if ctx.names.exact(stem):
        return True
    return any(ctx.names.exact(stem[i:]) for i in range(1, len(stem)))


def _name_points_at(ctx: Context, column: str, target: Table) -> bool:
    p = parse_id_column(column)
    return bool(p and p.stem and p.stem == ctx.stems.get(target.qualified))


def _names_one_primary_key(holders: list[tuple[Table, Column]]) -> bool:
    """Is this name the single-column primary key of exactly one table, and does it read like an id or a code?"""
    if len(holders) != 1:
        return False
    table, column = holders[0]
    p = parse_id_column(column.name)
    return table.key_strength(column.name) == "primary" and p is not None and bool(p.stem)


def _short(s: str, f: str) -> bool:
    """Is ``s`` a short form of ``f``? cust/customer, hdr/header, emp/employee. Needs 3+ letters and the same first letter."""
    if len(s) < 3 or len(s) >= len(f) or s[0] != f[0]:
        return False
    if f.startswith(s):
        return True
    it = iter(f)
    return all(ch in it for ch in s)  # letters of s appear in order inside f


def _abbreviates(short: tuple[str, ...], full: tuple[str, ...]) -> bool:
    if len(short) != len(full):
        return False
    proper = False
    for s, f in zip(short, full, strict=True):
        if s == f:
            continue
        if not _short(s, f):
            return False
        proper = True
    return proper


# ---------------------------------------------------------------- step 4


def declared_fks(ctx: Context) -> list[Candidate]:
    """Step 4. A foreign key the database declares is a fact."""
    w = WEIGHTS["declared"]
    # ctx.stems holds tables only: a key that names a view or a table the catalog did not list is ignored, as for every other rule
    return [
        Candidate(fk.from_table, fk.from_column, fk.to_table, fk.to_column, "declared", w)
        for fk in ctx.graph.declared_fks
        if fk.from_table in ctx.stems and fk.to_table in ctx.stems
    ]


# ---------------------------------------------------------------- step 5


def _orient(ctx: Context, a: tuple[Table, str], b: tuple[Table, str]) -> tuple[tuple[Table, str], tuple[Table, str], bool]:
    """Which side of a join is the parent? Returns (child, parent, symmetric). A key beats a non-key; then the names decide."""
    sa = _KEY_SCORE.get(a[0].key_strength(a[1]), 0)
    sb = _KEY_SCORE.get(b[0].key_strength(b[1]), 0)
    if sa > sb:
        return b, a, False
    if sb > sa:
        return a, b, False
    a_points, b_points = _name_points_at(ctx, a[1], b[0]), _name_points_at(ctx, b[1], a[0])
    if a_points and not b_points:
        return a, b, False
    if b_points and not a_points:
        return b, a, False
    first, second = sorted((a, b), key=lambda s: (s[0].qualified.lower(), s[1].lower()))
    return first, second, True


def proc_edges(ctx: Context) -> list[Candidate]:
    """Step 5. A join written inside a stored procedure. Seen in 2+ routines is strong; seen once is weak."""
    out: list[Candidate] = []
    for e in ctx.graph.proc_edges:
        ta, tb = ctx.graph.tables.get(e.a_table), ctx.graph.tables.get(e.b_table)
        if ta is None or tb is None or ta.is_view or tb.is_view:
            continue
        if ta.qualified == tb.qualified and e.a_column.lower() == e.b_column.lower():
            continue
        child, parent, symmetric = _orient(ctx, (ta, e.a_column), (tb, e.b_column))
        weight = WEIGHTS["proc_high"] if e.confidence == "high" else WEIGHTS["proc_low"]
        out.append(Candidate(child[0].qualified, child[1], parent[0].qualified, parent[1], "proc", weight, symmetric))
    return out


# ---------------------------------------------------------------- steps 6 to 11 (names)


def table_id_columns(ctx: Context) -> list[Candidate]:
    """Step 6. ``<table>_id`` points at that table's key."""
    out: list[Candidate] = []
    for t in ctx.tables:
        for c in t.columns:
            p = _ref_column(c)
            if p is None:
                continue
            targets = ctx.prefer_same_schema(t, [x for x in ctx.names.exact(p.stem) if x.qualified != t.qualified])
            for parent in targets:
                pc = ctx.parent_column(parent, c.name)
                if pc is not None:
                    out.append(Candidate(t.qualified, c.name, parent.qualified, pc.name, "table_id", WEIGHTS["table_id"]))
    return out


def same_column_name(ctx: Context) -> list[Candidate]:
    """Step 7. A column called like a single-column key of another table. Generic names never count (learned list)."""
    out: list[Candidate] = []
    for t in ctx.tables:
        for c in t.columns:
            norm = normalized_column_name(c.name)
            holders = ctx.keyed.get(norm, [])
            if not norm or not holders or len(holders) > MAX_KEY_HOLDERS:
                continue
            rule = "same_name"
            if norm in ctx.common:
                # A name found in many tables is usually generic (status, code). It is not when it is the sole primary
                # key of exactly one table and reads like an id (emp_no in employees, company_id in company): that is
                # what a foreign key column looks like, and the reason it is common is that many tables point there.
                if not _names_one_primary_key(holders):
                    continue
                rule = "pk_name"
            own_key = t.key_strength(c.name) is not None
            id_like = parse_id_column(c.name) is not None
            for parent, pc in ctx.prefer_same_schema(t, holders, lambda h: h[0]):
                if parent.qualified == t.qualified:
                    continue
                if own_key and (not id_like or t.qualified.lower() > parent.qualified.lower()):
                    # both sides are keys: a shared key or a one-to-one. Say it once, and only for id-like names.
                    continue
                out.append(Candidate(t.qualified, c.name, parent.qualified, pc.name, rule, WEIGHTS[rule], symmetric=own_key))
    return out


def abbreviated_stem(ctx: Context) -> list[Candidate]:
    """Step 8. ``cust_id`` -> ``customer``. Only when no stricter name rule fits and exactly one table fits."""
    out: list[Candidate] = []
    for t in ctx.tables:
        for c in t.columns:
            p = _ref_column(c)
            if p is None or _resolved_by_name(ctx, t, p.stem):
                continue
            pool = ctx.shapes.get((len(p.stem), p.stem[0][0]), [])
            hits = [x for x in pool if x.qualified != t.qualified and _abbreviates(p.stem, ctx.stems[x.qualified])]
            hits = ctx.prefer_same_schema(t, hits)
            if len(hits) != 1:
                continue
            pc = ctx.parent_column(hits[0], c.name)
            if pc is not None:
                out.append(Candidate(t.qualified, c.name, hits[0].qualified, pc.name, "abbrev", WEIGHTS["abbrev"]))
    return out


def role_prefix(ctx: Context) -> list[Candidate]:
    """Step 9. ``billing_address_id`` -> ``address``: a role word in front of a table name."""
    out: list[Candidate] = []
    for t in ctx.tables:
        for c in t.columns:
            p = _ref_column(c)
            if p is None or len(p.stem) < 2 or ctx.names.exact(p.stem):
                continue
            for parent in _role_targets(ctx, t, p.stem):
                pc = ctx.parent_column(parent, c.name)
                if pc is not None:
                    out.append(Candidate(t.qualified, c.name, parent.qualified, pc.name, "role_prefix", WEIGHTS["role_prefix"]))
    return out


def self_reference(ctx: Context) -> list[Candidate]:
    """Step 10. ``parent_id`` points at the table's own key. A role word like ``manager`` is a weaker sign."""
    out: list[Candidate] = []
    for t in ctx.tables:
        pk = ctx.sole_pk(t)
        if pk is None:
            continue
        own = ctx.stems[t.qualified]
        for c in t.columns:
            p = _ref_column(c)
            if p is None or c.name.lower() == pk.name.lower():
                continue
            word, rest = p.stem[0], p.stem[1:]
            if word not in HIERARCHY_WORDS and word not in ROLE_WORDS:
                continue
            if rest and rest != own:  # parent_category_id in category is a self reference; parent_region_id in category is not
                continue
            weak = word in ROLE_WORDS and not rest
            out.append(Candidate(t.qualified, c.name, t.qualified, pk.name, "self_ref", WEIGHTS["self_ref_role" if weak else "self_ref"]))
    return out


def code_like_columns(ctx: Context) -> list[Candidate]:
    """Step 11. ``po_number`` -> the unique column ``po_no`` of table ``po``. The target must be a key and the names must differ (else step 7)."""
    out: list[Candidate] = []
    for t in ctx.tables:
        for c in t.columns:
            p = parse_id_column(c.name)
            if p is None or not p.stem or p.suffix not in CODE_SUFFIXES:
                continue
            norm = normalized_column_name(c.name)
            for parent in ctx.prefer_same_schema(t, [x for x in ctx.names.exact(p.stem) if x.qualified != t.qualified]):
                for q in parent.columns:
                    pq = parse_id_column(q.name)
                    if pq is None or pq.stem != p.stem or pq.suffix not in CODE_SUFFIXES:
                        continue
                    if parent.key_strength(q.name) is None or normalized_column_name(q.name) == norm:
                        continue
                    out.append(Candidate(t.qualified, c.name, parent.qualified, q.name, "code_like", WEIGHTS["code_like"]))
    return out


# ---------------------------------------------------------------- steps 14 and 15 (structure)


def _line_like(c: Column) -> bool:
    toks = split_identifier(c.name)
    return c.family in ("integer", "numeric") and any(t in _LINE_TOKENS or t.startswith(("line", "seq")) for t in toks)


def _key_parts(table: Table, prior: dict[tuple[str, str], list[Candidate]]) -> dict[str, list[Candidate]] | None:
    """For a composite primary key: each key column with the references already found for it."""
    if len(table.primary_key) < 2:
        return None
    return {k: prior.get((table.qualified, k.lower()), []) for k in table.primary_key}


def _index(prior: list[Candidate]) -> dict[tuple[str, str], list[Candidate]]:
    out: dict[tuple[str, str], list[Candidate]] = {}
    for c in prior:
        out.setdefault((c.child, c.child_column.lower()), []).append(c)
    return out


def junction_tables(ctx: Context, prior: list[Candidate]) -> list[Candidate]:
    """Step 14. A composite key where two or more parts are references is a link table; each part gets a small boost."""
    by_col = _index(prior)
    out: list[Candidate] = []
    for t in ctx.tables:
        parts = _key_parts(t, by_col)
        if not parts:
            continue
        referenced = [k for k, cands in parts.items() if cands]
        if len(referenced) < 2:
            continue
        for k in referenced:
            out += [Candidate(c.child, c.child_column, c.parent, c.parent_column, "junction", WEIGHTS["junction"]) for c in parts[k]]
    return out


def header_detail(ctx: Context, prior: list[Candidate]) -> list[Candidate]:
    """Step 15. A composite key made of exactly one reference to a parent's own key plus a line number column."""
    by_col = _index(prior)
    out: list[Candidate] = []
    for t in ctx.tables:
        parts = _key_parts(t, by_col)
        if not parts:
            continue
        referenced = [k for k, cands in parts.items() if cands]
        others = [t.column(k) for k in parts if k not in referenced]
        if len(referenced) != 1 or not others or not all(o is not None and _line_like(o) for o in others):
            continue
        for c in parts[referenced[0]]:
            parent = ctx.graph.tables.get(c.parent)
            if parent is not None and parent.key_strength(c.parent_column) == "primary":
                out.append(Candidate(c.child, c.child_column, c.parent, c.parent_column, "header_detail", WEIGHTS["header_detail"]))
    return out


# ---------------------------------------------------------------- all rules

_BASE_RULES: tuple[Callable[[Context], list[Candidate]], ...] = (
    declared_fks, proc_edges, table_id_columns, same_column_name, abbreviated_stem, role_prefix, self_reference, code_like_columns,
)


def detect(ctx: Context) -> list[Candidate]:
    """Run steps 4 to 15 and return every candidate, in a fixed order (the same input always gives the same list)."""
    base: list[Candidate] = []
    for rule in _BASE_RULES:
        base.extend(rule(ctx))
    everything = base + junction_tables(ctx, base) + header_detail(ctx, base)
    return sorted(set(everything), key=lambda c: (c.child, c.child_column, c.parent, c.parent_column, c.rule, c.weight, c.symmetric))


__all__ = [
    "CODE_SUFFIXES", "HIERARCHY_WORDS", "REF_SUFFIXES", "ROLE_WORDS",
    "abbreviated_stem", "code_like_columns", "declared_fks", "detect", "header_detail", "junction_tables",
    "proc_edges", "role_prefix", "same_column_name", "self_reference", "table_id_columns",
]
