"""Per-database soft-delete knowledge, for every SQL dialect (extends ``soft_delete.py``).

``soft_delete.py`` only knew one marker: a column literally named ``deleted_at``, on the tables the behavioral atlas lists. A
database that marks deleted rows another way (``IsDeleted bit``, ``is_deleted char(1)`` with Y/N, ``deleted_on``...) was never
filtered, so the model could return rows the application had deleted.

This module works out, for each table, WHICH column marks a row as deleted and HOW, and builds the condition for "not deleted"
(and "deleted") as a sqlglot expression, so the SQL comes out right in MySQL, PostgreSQL, SQL Server (T-SQL), Oracle and SQLite.

Where the knowledge comes from, strongest first:

1. ``semantics/soft_delete.json`` (optional, curated): ``{"tables": {"orders": {"column": "is_removed", "kind": "char_flag"}},
   "ignore": ["audit_log"]}``. ``ignore`` removes a table from everything below; ``tables`` forces a column.
2. The behavioral atlas: any table with a ``deleted_at`` column (what ``soft_delete.py`` has always used).
3. ``db.yaml``'s ``soft_delete.column``: a column name every table that has it is filtered by.
4. Discovery from the catalog (``schema/schema.json``), by column NAME and TYPE together, never by name alone.

Kinds (what "deleted" looks like) and the "not deleted" condition:

* ``timestamp``  - a date/time, NULL while the row is live:            ``col IS NULL``
* ``boolean``    - a native boolean (PostgreSQL, SQLite):               ``col = FALSE OR col IS NULL``
* ``int_flag``   - bit / tinyint / smallint / int / NUMBER, 0 = live:   ``col = 0 OR col IS NULL``
* ``char_flag``  - a one-letter flag, 'N' = live (also NULL):           ``col IN ('N', 'n') OR col IS NULL``

Nothing here reads data rows: soft-delete filtering is mandatory and always active across all databases.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot.expressions as exp

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)

KINDS = ("timestamp", "boolean", "int_flag", "char_flag")
CURATED_REL = "semantics/soft_delete.json"
MAX_PROMPT_TABLES = 15


@dataclass(frozen=True)
class SoftDeleteSpec:
    table: str  # lower-case
    column: str  # as the catalog spells it
    kind: str
    source: str  # curated | atlas | declared | discovered

    def describe(self) -> str:
        return f"{self.table}.{self.column} ({self.kind}, from {self.source})"


# --------------------------------------------------------------------------- classification

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# Names are compared without underscores, spaces or case: "IsDeleted", "is_deleted" and "IS DELETED" are one name.
_TIMESTAMP_NAMES = frozenset(
    _norm(n)
    for n in (
        "deleted_at", "deleted_on", "deleted_date", "date_deleted", "deletion_date", "deleted_time", "deleted_datetime",
        "deleted_ts", "delete_date", "delete_time", "del_date", "removed_at", "removed_on", "removed_date",
    )
)
_STRONG_FLAG_NAMES = frozenset(
    _norm(n)
    for n in (
        "is_deleted", "is_removed", "is_soft_deleted", "soft_deleted", "deleted_flag", "delete_flag", "del_flag", "is_del",
        "removed_flag", "deleteflag",
    )
)
_WEAK_FLAG_NAMES = frozenset(_norm(n) for n in ("deleted", "removed", "delete"))  # also a count or a name elsewhere: type must fit

_INT_TYPES = ("int", "integer", "smallint", "tinyint", "bigint", "bit", "number", "numeric", "decimal", "mediumint")
_CHAR_FLAG_TYPE = re.compile(r"^(?:n?char|character|bpchar|n?varchar2?|character varying)\s*(?:\(\s*1\s*\))?$", re.IGNORECASE)
_BARE_CHAR = re.compile(r"^(?:n?char|character|bpchar)$", re.IGNORECASE)  # fixed-width: a flag, where varchar could be anything


def _strip_default(raw: Any) -> str:
    return re.sub(r"[\s()'\"]", "", str(raw or "")).upper()


def _type_family(type_text: str) -> str:
    t = str(type_text or "").strip().lower()
    if not t:
        return "unknown"
    if "bool" in t:
        return "boolean"
    if t.startswith("bit"):
        return "int"
    if t.startswith(("tinyint", "smallint")):
        return "smallint"
    if t in _INT_TYPES or t.startswith(tuple(f"{k}{sep}" for k in _INT_TYPES for sep in ("(", " "))):
        return "int"
    if "date" in t or "time" in t:  # datetime, datetime2, timestamp [with time zone], date, smalldatetime, datetimeoffset, TIMESTAMP(6)
        return "timestamp"
    if _CHAR_FLAG_TYPE.match(t):
        return "char"
    return "other"


def classify_column(name: str, type_text: str, *, nullable: bool | None, default: Any = None, enum_values: Any = None) -> str | None:
    """The soft-delete kind a column looks like, or None. Needs a recognised NAME and a type that fits that name."""
    n = _norm(name)
    fam = _type_family(type_text)
    if n in _TIMESTAMP_NAMES:
        # NOT NULL cannot mean "NULL while live"; an integer or a string named deleted_at is something else
        return "timestamp" if fam == "timestamp" and nullable is not False else None
    strong, weak = n in _STRONG_FLAG_NAMES, n in _WEAK_FLAG_NAMES
    if not (strong or weak):
        return None
    if fam == "boolean":
        return "boolean"
    if fam in ("int", "smallint"):
        return "int_flag" if strong or fam == "smallint" or str(type_text).lower().startswith(("bit", "tinyint")) else None
    if fam == "char":
        t = str(type_text).strip()
        if _BARE_CHAR.match(t) or re.search(r"\(\s*1\s*\)", t):
            return "char_flag"
    vals = {str(v).strip().upper() for v in (enum_values or [])}
    if vals and vals <= {"Y", "N"}:
        return "char_flag"  # an enum('Y','N')
    if fam in ("char", "other") and _strip_default(default) in {"N", "0N"}:
        return "char_flag"  # a varchar that defaults to 'N'
    return None


_PRIORITY = {"timestamp": 0, "boolean": 1, "int_flag": 2, "char_flag": 3}


def discover_from_schema(doc: Any) -> tuple[dict[str, SoftDeleteSpec], list[str]]:
    """``({table: spec}, notes)`` from a catalog ``schema.json`` document (one spec per table)."""
    specs: dict[str, SoftDeleteSpec] = {}
    notes: list[str] = []
    tables = doc.get("tables") if isinstance(doc, dict) else None
    if not isinstance(tables, list):
        return specs, notes
    for table in tables:
        if not isinstance(table, dict) or not table.get("name"):
            continue
        t_name = str(table["name"]).lower()
        pk = {str(c).lower() for c in table.get("primary_key") or []}
        found: list[SoftDeleteSpec] = []
        for col in table.get("columns") or []:
            if not isinstance(col, dict) or not col.get("name"):
                continue
            if str(col["name"]).lower() in pk or col.get("is_foreign_key"):
                continue
            kind = classify_column(
                col["name"], col.get("type", ""), nullable=col.get("nullable"), default=col.get("default"), enum_values=col.get("enum_values")
            )
            if kind:
                found.append(SoftDeleteSpec(t_name, str(col["name"]), kind, "discovered"))
        if not found:
            continue
        found.sort(key=lambda s: _PRIORITY[s.kind])
        specs[t_name] = found[0]
        if len(found) > 1:
            notes.append(f"{t_name}: also has {', '.join(f.column for f in found[1:])}; using {found[0].column}")
    return specs, notes


# --------------------------------------------------------------------------- loading

_CACHE: dict[str, tuple[tuple, dict[str, SoftDeleteSpec]]] = {}
_LOCK = threading.Lock()


def clear_spec_cache(db_id: str | None = None) -> None:
    with _LOCK:
        if db_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(db_id, None)


def _stamp(paths: list[Path]) -> tuple:
    out = []
    for p in paths:
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _declared_column(db_id: str) -> str | None:
    try:
        sd = registry.get_database(db_id).get("soft_delete")
    except Exception:  # noqa: BLE001
        return None
    col = sd.get("column") if isinstance(sd, dict) else sd
    return str(col).strip() if isinstance(col, str) and col.strip() else None


def load_specs(db_id: str = DEFAULT_DB_ID, *, databases_dir: Path | None = None) -> dict[str, SoftDeleteSpec]:
    """``{lower-case table name: spec}`` for one database (cached until a source file changes). Never raises."""
    try:
        validate_db_id(db_id)
        root = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
        paths = [root / "schema" / "schema.json", root / "semantics" / "behavioral_atlas.json", root / "db.yaml", root / CURATED_REL]
        stamp = _stamp(paths)
        key = f"{root}"
        with _LOCK:
            cached = _CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        specs = _build(db_id, root, paths)
        with _LOCK:
            _CACHE[key] = (stamp, specs)
        return specs
    except Exception as exc:  # noqa: BLE001 - without knowledge there is simply no extra filtering
        logger.warning("Soft-delete knowledge unavailable for %s: %s: %s", db_id, type(exc).__name__, str(exc)[:200])
        return {}


def _build(db_id: str, root: Path, paths: list[Path]) -> dict[str, SoftDeleteSpec]:
    schema_doc = _read_json(paths[0])
    specs, _notes = discover_from_schema(schema_doc)

    declared = _declared_column(db_id)
    if declared and isinstance(schema_doc, dict):
        for table in schema_doc.get("tables") or []:
            if not isinstance(table, dict):
                continue
            for col in table.get("columns") or []:
                if isinstance(col, dict) and str(col.get("name", "")).lower() == declared.lower():
                    # The name was declared by a person, so trust it: judge only whether the TYPE can carry a soft-delete marker.
                    probe = "deleted_at" if _type_family(col.get("type", "")) == "timestamp" else "is_deleted"
                    kind = classify_column(probe, col.get("type", ""), nullable=col.get("nullable"), default=col.get("default"), enum_values=col.get("enum_values"))
                    if kind:
                        specs[str(table["name"]).lower()] = SoftDeleteSpec(str(table["name"]).lower(), str(col["name"]), kind, "declared")

    atlas = _read_json(paths[1])
    atlas_tables = atlas.get("tables") if isinstance(atlas, dict) else None
    if isinstance(atlas_tables, dict):
        for t_name, meta in atlas_tables.items():
            cols = meta.get("columns", {}) if isinstance(meta, dict) else {}
            if isinstance(cols, dict) and any(str(c).lower() == "deleted_at" for c in cols):
                specs[str(t_name).lower()] = SoftDeleteSpec(str(t_name).lower(), "deleted_at", "timestamp", "atlas")

    curated = _read_json(paths[3])
    if isinstance(curated, dict):
        for t_name, entry in (curated.get("tables") or {}).items():
            if isinstance(entry, dict) and entry.get("column") and entry.get("kind") in KINDS:
                specs[str(t_name).lower()] = SoftDeleteSpec(str(t_name).lower(), str(entry["column"]), str(entry["kind"]), "curated")
        for t_name in curated.get("ignore") or []:
            specs.pop(str(t_name).lower(), None)
    return specs


# --------------------------------------------------------------------------- the switch


def soft_delete_mode(db_id: str = "") -> str:
    """Soft-delete filtering is mandatory across all databases; always returns 'apply'."""
    return "apply"


# --------------------------------------------------------------------------- conditions


def _col(spec: SoftDeleteSpec, qualifier: str) -> exp.Column:
    return exp.column(spec.column, table=qualifier or None)


def _lit_str(*values: str) -> list[exp.Expression]:
    return [exp.Literal.string(v) for v in values]


def active_condition(spec: SoftDeleteSpec, qualifier: str = "") -> exp.Expression:
    """The condition for "this row is NOT deleted" (a NULL marker counts as live)."""
    col = _col(spec, qualifier)
    is_null = exp.Is(this=_col(spec, qualifier), expression=exp.Null())
    if spec.kind == "timestamp":
        return is_null
    if spec.kind == "boolean":
        return exp.Paren(this=exp.Or(this=exp.EQ(this=col, expression=exp.false()), expression=is_null))
    if spec.kind == "int_flag":
        return exp.Paren(this=exp.Or(this=exp.EQ(this=col, expression=exp.Literal.number(0)), expression=is_null))
    return exp.Paren(this=exp.Or(this=exp.In(this=col, expressions=_lit_str("N", "n")), expression=is_null))


def deleted_condition(spec: SoftDeleteSpec, qualifier: str = "") -> exp.Expression:
    """The condition for "this row IS deleted"."""
    col = _col(spec, qualifier)
    if spec.kind == "timestamp":
        return exp.Not(this=exp.Is(this=col, expression=exp.Null()))
    if spec.kind == "boolean":
        return exp.EQ(this=col, expression=exp.true())
    if spec.kind == "int_flag":
        return exp.NEQ(this=col, expression=exp.Literal.number(0))
    return exp.In(this=col, expressions=_lit_str("Y", "y"))


_QUOTED = r"""(?:[A-Za-z_][\w$#]*|"[^"]+"|\[[^\]]+\]|`[^`]+`)"""
_NOT_IS_NULL = re.compile(rf"\bNOT\s+({_QUOTED}(?:\.{_QUOTED})*)\s+IS\s+NULL\b", re.IGNORECASE)


def tidy(sql: str) -> str:
    """Write ``NOT x IS NULL`` as ``x IS NOT NULL`` (same meaning; the way a person writes it)."""
    return _NOT_IS_NULL.sub(r"\1 IS NOT NULL", sql)


def and_condition(existing: exp.Expression, cond: exp.Expression) -> exp.Expression:
    """``existing AND cond`` where an OR (or XOR) in ``existing`` is parenthesised, because a bare ``a OR b AND cond`` reads
    ``a OR (b AND cond)`` and would let rows matching ``a`` skip the condition. Anything else is joined exactly as before."""
    if isinstance(existing, (exp.Or, getattr(exp, "Xor", exp.Or))):
        existing = exp.Paren(this=existing)
    return exp.And(this=existing, expression=cond)


def render(expression: exp.Expression, dialect: str) -> str:
    """The condition as SQL text in ``dialect`` (a ``NOT x IS NULL`` is written ``x IS NOT NULL``)."""
    return tidy(expression.sql(dialect=dialect))


# --------------------------------------------------------------------------- recognising what the model already wrote

_ACTIVE_LITS = {"0", "false", "n"}
_DELETED_LITS = {"1", "true", "y"}


def _lit_value(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Boolean):
        return "true" if node.this else "false"
    if isinstance(node, exp.Literal):
        return str(node.this).strip().lower()
    return None


def _is_marker(node: exp.Expression, spec: SoftDeleteSpec, quals: set[str]) -> bool:
    return isinstance(node, exp.Column) and node.name.lower() == spec.column.lower() and (not node.table or node.table.lower() in quals)


def classify_conjunct(node: exp.Expression, spec: SoftDeleteSpec, quals: set[str]) -> str | None:
    """``"active"``, ``"deleted"`` or None: what ONE condition already written says about this table's marker column."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Or):
        a, b = classify_conjunct(node.this, spec, quals), classify_conjunct(node.expression, spec, quals)
        return "active" if a == b == "active" else None
    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null) and _is_marker(inner.this, spec, quals):
            return "deleted"
        if _is_marker(inner, spec, quals):  # NOT is_deleted
            return "active"
        return None
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null) and _is_marker(node.this, spec, quals):
        return "active"
    if isinstance(node, (exp.EQ, exp.NEQ)):
        for marker, lit in ((node.this, node.expression), (node.expression, node.this)):
            if _is_marker(marker, spec, quals):
                v = _lit_value(lit)
                if v is None:
                    return None
                if isinstance(node, exp.EQ):
                    return "active" if v in _ACTIVE_LITS else "deleted" if v in _DELETED_LITS else None
                return "deleted" if v in _ACTIVE_LITS else "active" if v in _DELETED_LITS else None
    if isinstance(node, exp.In) and _is_marker(node.this, spec, quals):
        vals = {_lit_value(e) for e in node.expressions}
        return "active" if vals and vals <= _ACTIVE_LITS else "deleted" if vals and vals <= _DELETED_LITS else None
    return None


def conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    """The AND-ed parts of a condition."""
    if node is None:
        return []
    while isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
        node = node.this
    if isinstance(node, exp.And):
        return conjuncts(node.this) + conjuncts(node.expression)
    return [node]


# --------------------------------------------------------------------------- enforcing it on generated SQL


def _direct_tables(sel: exp.Select) -> list[tuple[exp.Table, exp.Join | None]]:
    out: list[tuple[exp.Table, exp.Join | None]] = []
    from_ = sel.args.get("from_")
    if from_ is not None and isinstance(from_.this, exp.Table):
        out.append((from_.this, None))
    for join in sel.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            out.append((join.this, join))
    return out


def _add_to_where(sel: exp.Select, cond: exp.Expression) -> None:
    where = sel.args.get("where")
    sel.set("where", exp.Where(this=and_condition(where.this, cond) if where is not None else cond))


def enforce(sql: str, intent: str, dialect: str, specs: dict[str, SoftDeleteSpec]) -> tuple[str, list[str]]:
    """``(sql, added)``: ``sql`` with the soft-delete condition of every table that has a spec, ``added`` the conditions written.

    ACTIVE_ONLY adds "not deleted"; DELETED_ONLY adds "deleted" (and swaps a "not deleted" the model wrote for it);
    INCLUDE_ARCHIVED changes nothing. A condition the model already wrote is recognised and not repeated. The condition of a
    table on the preserved-NULL side of a LEFT JOIN goes in that join's ON clause, so a deleted match becomes "no match" instead of
    removing the other table's row. Raises if ``sql`` cannot be parsed (the caller keeps the original).
    """
    import sqlglot

    parsed = sqlglot.parse_one(sql, read=dialect)
    added: list[str] = []
    if intent == "INCLUDE_ARCHIVED" or not specs:
        return tidy(parsed.sql(dialect=dialect)), added

    for sel in parsed.find_all(exp.Select):
        direct = _direct_tables(sel)
        targets = [(t, j) for t, j in direct if t.name.lower() in specs]
        if not targets:
            continue
        multi = len(direct) > 1
        for tbl, join in targets:
            spec = specs[tbl.name.lower()]
            alias = tbl.alias or ""
            qualifier = alias or (tbl.name if multi else "")
            quals = {q.lower() for q in (alias, tbl.name) if q} | (set() if multi else {""})

            where = sel.args.get("where")
            on = join.args.get("on") if join is not None else None
            existing = conjuncts(where.this if where is not None else None) + conjuncts(on)
            states = [(classify_conjunct(c, spec, quals), c) for c in existing]

            if intent == "DELETED_ONLY":
                handled = False
                for state, node in states:
                    if state == "deleted":
                        handled = True
                    elif state == "active":  # contradicts what was asked: swap it
                        node.replace(deleted_condition(spec, qualifier))
                        handled = True
                        added.append(f"{spec.table}: {render(deleted_condition(spec, qualifier), dialect)} (replaced)")
                if not handled:
                    cond = deleted_condition(spec, qualifier)
                    _add_to_where(sel, cond)
                    added.append(f"{spec.table}: {render(cond, dialect)}")
                continue

            if any(state == "active" for state, _ in states):
                continue
            cond = active_condition(spec, qualifier)
            side = str(join.args.get("side") or "").upper() if join is not None else ""
            if join is not None and side == "LEFT" and on is not None:
                join.set("on", and_condition(on, cond))
            else:
                _add_to_where(sel, cond)
            added.append(f"{spec.table}: {render(cond, dialect)}")
    return tidy(parsed.sql(dialect=dialect)), added


def prompt_block(specs: dict[str, SoftDeleteSpec], shown_tables: Any, intent: str, dialect: str) -> str:
    """The prompt text naming the exact soft-delete condition of each shown table (apply mode), or "" when none applies."""
    if intent == "INCLUDE_ARCHIVED":
        mine = sorted(t for t in {str(x).lower() for x in shown_tables} if t in specs)
        if not mine:
            return ""
        return "\n\nSoft-delete (AUDIT / HISTORY QUERY): return deleted AND live rows; add no soft-delete filter on: " + ", ".join(mine[:MAX_PROMPT_TABLES]) + "."
    mine = sorted(t for t in {str(x).lower() for x in shown_tables} if t in specs)[:MAX_PROMPT_TABLES]
    if not mine:
        return ""
    make = deleted_condition if intent == "DELETED_ONLY" else active_condition
    head = (
        "Soft-delete conditions (the user asks for DELETED rows: use these, not the default rule above):"
        if intent == "DELETED_ONLY"
        else "Soft-delete conditions (rows marked deleted must NEVER be returned). Use EXACTLY these columns, not `deleted_at`, "
        "on every table below that you query, with its alias; put the condition of a LEFT JOINed table in its ON clause:"
    )
    lines = [f"- {t}: {render(make(specs[t], 'alias'), dialect)}" for t in mine]
    return "\n\n" + head + "\n" + "\n".join(lines)


__all__ = [
    "CURATED_REL",
    "KINDS",
    "MAX_PROMPT_TABLES",
    "SoftDeleteSpec",
    "active_condition",
    "and_condition",
    "classify_column",
    "classify_conjunct",
    "clear_spec_cache",
    "conjuncts",
    "deleted_condition",
    "discover_from_schema",
    "enforce",
    "load_specs",
    "prompt_block",
    "render",
    "soft_delete_mode",
    "tidy",
]
