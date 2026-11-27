"""Read bounded, static column-value policy definitions from local procedure scripts.

Some databases keep useful application knowledge in a procedure such as
``Policy_GetColumnValueMappings``.  A sync already reads local ``procedures/*.sql``
files as *text*, but it must never execute them: a policy script can also contain
administrative procedures that query live rows.  This module recognizes only the
literal ``FROM (VALUES ...)`` mapping table in that specifically named local policy
procedure.  It never opens a database connection and never evaluates SQL.

The resulting small metadata records are validated against the generated catalog and
are saved beside the existing generated column hints.  The prompt builder can then
use a query-relevant subset as SQL-generation guidance.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.sql.mining.catalog_index import CatalogIndex
from src.sql.sa.routines import Routine

POLICY_MAPPINGS_KEY = "policy_mappings"
POLICY_PROCEDURE = "policy_getcolumnvaluemappings"
MAX_POLICY_ROWS = 500
MAX_FIELD_CHARS = 1_200
_VALUES_START = re.compile(r"\bFROM\s*\(\s*VALUES\b", re.IGNORECASE)
_POLICY_NAME = re.compile(r"[^a-z0-9]+")
_CONFIDENCE = frozenset({"CONFIRMED", "INFERRED", "UNRESOLVED"})


def _normalized_name(value: str) -> str:
    return _POLICY_NAME.sub("", value.lower())


def _skip_quoted(text: str, start: int) -> int:
    """Return the character after a T-SQL single-quoted literal, including ``''`` escapes."""
    index = start + 1
    while index < len(text):
        if text[index] != "'":
            index += 1
            continue
        if index + 1 < len(text) and text[index + 1] == "'":
            index += 2
            continue
        return index + 1
    return len(text)


def _values_bodies(definition: str) -> Iterable[str]:
    """Yield the contents of every literal ``FROM (VALUES ...)`` table in text.

    This is intentionally a tiny delimiter scanner rather than a SQL executor or a
    general SQL parser.  It understands only parentheses and SQL string literals,
    which is enough to preserve commas and parentheses inside policy prose.
    """
    for match in _VALUES_START.finditer(definition):
        outer_open = definition.find("(", match.start(), match.end())
        if outer_open < 0:
            continue
        depth = 1
        index = outer_open + 1
        while index < len(definition):
            if definition[index] == "'":
                index = _skip_quoted(definition, index)
                continue
            if definition[index] == "(":
                depth += 1
            elif definition[index] == ")":
                depth -= 1
                if depth == 0:
                    yield definition[outer_open + 1:index]
                    break
            index += 1


def _top_level_rows(body: str) -> Iterable[str]:
    """Yield parenthesized rows from a ``VALUES`` body without interpreting SQL."""
    depth = 0
    start: int | None = None
    index = 0
    while index < len(body):
        char = body[index]
        if char == "'":
            index = _skip_quoted(body, index)
            continue
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield body[start:index]
                start = None
        index += 1


def _row_fields(row: str) -> list[str] | None:
    """Parse the seven simple string literals that make up one policy row."""
    fields: list[str] = []
    token_start = 0
    index = 0
    while index <= len(row):
        if index == len(row) or row[index] == ",":
            token = row[token_start:index].strip()
            match = re.fullmatch(r"(?:N)?'((?:''|[^'])*)'", token, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                return None
            value = match.group(1).replace("''", "'")
            if not value or len(value) > MAX_FIELD_CHARS:
                return None
            fields.append(value)
            token_start = index + 1
        elif row[index] == "'":
            index = _skip_quoted(row, index) - 1
        index += 1
    return fields if len(fields) == 7 else None


def _is_mapping_policy(routine: Routine) -> bool:
    if routine.source != "folder" or not routine.definition:
        return False
    members = tuple(_normalized_name(member) for member in routine.members)
    target = _normalized_name(POLICY_PROCEDURE)
    if any(target in member for member in members):
        return True
    return target in _normalized_name(routine.definition)


def extract_policy_mappings(routines: Iterable[Routine], catalog: CatalogIndex | None) -> list[dict[str, str]]:
    """Extract catalog-backed records from the static local policy mapping procedure.

    ``UNRESOLVED`` rows are retained in the sync artifact for transparency but are
    deliberately excluded later when a prompt is built.  Unknown tables/columns and
    malformed rows are ignored rather than guessed.
    """
    if catalog is None:
        return []
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for routine in routines:
        if not _is_mapping_policy(routine) or not routine.definition:
            continue
        for body in _values_bodies(routine.definition):
            for raw_row in _top_level_rows(body):
                fields = _row_fields(raw_row)
                if fields is None:
                    continue
                table_name, column, stored_value, meaning, confidence, evidence, query_hint = fields
                confidence = confidence.upper()
                table = catalog.resolve_table(table_name)
                if confidence not in _CONFIDENCE or table is None or not catalog.has_column(table, column):
                    continue
                normalized_column = column.lower()
                item = {
                    "table": table,
                    "column": normalized_column,
                    "stored_value": stored_value,
                    "meaning": meaning,
                    "confidence": confidence,
                    "evidence": evidence,
                    "query_hint": query_hint,
                    "source": routine.name,
                }
                key = tuple(item[field].lower() for field in ("table", "column", "stored_value", "meaning", "confidence", "query_hint"))
                if key not in seen:
                    seen.add(key)
                    rows.append(item)
                if len(rows) >= MAX_POLICY_ROWS:
                    return rows
    return rows


__all__ = ["MAX_POLICY_ROWS", "POLICY_MAPPINGS_KEY", "POLICY_PROCEDURE", "extract_policy_mappings"]
