"""Pre-execution type checks: catch a comparison the database will reject, or silently never match, BEFORE it runs.

Examples seen in production on SQL Server: ``active = 1`` on a ``CHAR(1)`` holding 'Y'/'N' ("Conversion failed when converting the
varchar value 'Y' to data type int"), or a join of a ticket number to a plant code. Column types come from ``schema/schema.json``
(through ``SQLRequestContext``); nothing here reads the data and nothing here rewrites a query: it reports.

Rules (``kind``):

* ``number_vs_text``   a number/bit column compared with a text literal that is not numeric (a certain error on SQL Server,
  PostgreSQL and Oracle; coerced silently, so only a warning, elsewhere)
* ``bad_date``         a date column compared with text that holds no date at all (same severity rule)
* ``text_vs_number``   a text column compared with a number (works only if every value converts: warning)
* ``bad_flag``         a bit column compared with something other than 0/1, or a column with known allowed values compared with
  a literal outside them (never matches: warning)
* ``join_type``        a join/equality between columns of different families, text vs number or date vs either (warning)

Only a ``CRITICAL`` issue stops the query before it runs, so only a certain error is ever critical.

Origin: new (no previous file)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

STRICT_DIALECTS = frozenset({"tsql", "postgres", "oracle"})  # these raise on a bad conversion instead of coercing
_TEXT = re.compile(r"^(?:n?var)?char|^n?text|^character|^string|^clob|^nclob|^enum|^uuid|^uniqueidentifier", re.IGNORECASE)
_DATE = re.compile(r"^date|^time|^datetime|^smalldatetime|^timestamp", re.IGNORECASE)
_BIT = re.compile(r"^bit$|^boolean$|^bool$", re.IGNORECASE)
_NUMBER = re.compile(r"^(?:tiny|small|medium|big)?int|^integer|^numeric|^decimal|^number|^float|^double|^real|^money|^smallmoney", re.IGNORECASE)
_NUMERIC_TEXT = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*$")
_DATE_LIKE = re.compile(r"\d{4}[-/.]?\d{2}[-/.]?\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")


@dataclass(frozen=True)
class TypeIssue:
    kind: str
    column: str
    message: str
    critical: bool


def family(type_text: str) -> str | None:
    """``number`` / ``text`` / ``date`` / ``bit`` for a column type, ``None`` when unknown."""
    t = (type_text or "").strip()
    for name, pattern in (("bit", _BIT), ("date", _DATE), ("number", _NUMBER), ("text", _TEXT)):
        if pattern.match(t):
            return name
    return None


def _literal(node: exp.Expression) -> tuple[str, str] | None:
    """``("number" | "text", value)`` for a literal (a negative number counts), else ``None``."""
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        node = node.this
    if isinstance(node, exp.Literal):
        return ("text" if node.is_string else "number"), str(node.this)
    return None


def check_types(
    sql: str,
    column_types: dict[str, dict[str, str]],
    dialect: str | None,
    column_enums: dict[str, dict[str, list[str]]] | None = None,
) -> list[TypeIssue]:
    """The type problems of ``sql`` (empty if it parses badly or the types are unknown). Never raises."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001
        return []
    if tree is None or not column_types:
        return []
    strict = (dialect or "") in STRICT_DIALECTS
    tables = {str(t.alias or t.name).lower(): t.name.lower() for t in tree.find_all(exp.Table)}
    tables.update({t.name.lower(): t.name.lower() for t in tree.find_all(exp.Table)})
    owners = sorted({v for v in tables.values()})

    def resolve(col: exp.Expression) -> tuple[str, str, str] | None:
        """``(table, column, family)`` for a plain column of a known table."""
        if not isinstance(col, exp.Column) or not col.name:
            return None
        name = col.name.lower()
        if col.table:
            table = tables.get(col.table.lower())
        else:
            found = [t for t in owners if name in column_types.get(t, {})]
            table = found[0] if len(found) == 1 else None
        typ = column_types.get(table or "", {}).get(name)
        fam = family(typ) if typ else None
        return (table, name, fam) if table and fam else None

    issues: list[TypeIssue] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, label: str, message: str, critical: bool = False) -> None:
        if (kind, label) not in seen:
            seen.add((kind, label))
            issues.append(TypeIssue(kind, label, message, critical and strict))

    for node in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In):
        if isinstance(node, (exp.EQ, exp.NEQ)):  # column against column: a join or an equality between different families
            a, b = resolve(node.this), resolve(node.expression)
            if a and b and {a[2], b[2]} in ({"text", "number"}, {"text", "date"}, {"date", "number"}):
                add("join_type", f"{a[0]}.{a[1]}={b[0]}.{b[1]}", f"`{a[0]}.{a[1]}` ({a[2]}) is compared with `{b[0]}.{b[1]}` ({b[2]}): different types")
        col = resolve(node.this)
        if col is None:
            continue
        table, name, fam = col
        label, typ = f"{table}.{name}", column_types[table][name]
        allowed = (column_enums or {}).get(table, {}).get(name)
        rights = list(node.expressions) if isinstance(node, exp.In) else [node.expression]
        for kind_, value in filter(None, (_literal(r) for r in rights)):
            if allowed and value.strip().casefold() not in {a.strip().casefold() for a in allowed}:
                add("bad_flag", label, f"`{label}` only allows {', '.join(repr(v) for v in allowed)} but is compared with '{value}'")
            elif fam in ("number", "bit") and kind_ == "text" and not _NUMERIC_TEXT.match(value):
                add("number_vs_text", label, f"`{label}` is {typ} but is compared with the text '{value}'", True)
            elif fam == "text" and kind_ == "number":
                add("text_vs_number", label, f"`{label}` is {typ} (text) but is compared with the number {value}")
            elif fam == "date" and kind_ == "text" and not _DATE_LIKE.search(value):
                add("bad_date", label, f"`{label}` is a date but is compared with '{value}', which holds no date", True)
            elif fam == "bit" and value.strip().lower() not in {"0", "1", "true", "false"}:
                add("bad_flag", label, f"`{label}` is a bit/boolean flag but is compared with '{value}'")
    return issues


def issue_texts(issues: list[TypeIssue]) -> list[str]:
    return [i.message for i in issues]


def as_context(issue: TypeIssue) -> dict[str, Any]:
    return {"type_issue": issue.kind, "column": issue.column}


__all__ = ["STRICT_DIALECTS", "TypeIssue", "as_context", "check_types", "family", "issue_texts"]
