"""Where a missing column lives, for the Delta Repair prompt.

When the model uses a column on the wrong table (``sqts_cost_recovery.prinbr`` when only another table has ``prinbr``), the
validator says "Column 'prinbr' does not exist in table 'sqts_cost_recovery'". The repair step was shown only the tables the
failed SQL already used, so it had no way to find the table that really holds the column or the table that links to it. It
swapped in look-alike tables and invented joins.

This finds, in the live schema, the tables that have the missing column and ranks them by how many RARE columns they share with
the tables the query already uses (a column found in most tables, such as ``deleted``, counts for almost nothing; ``qn_number``
counts a lot). The result is a short block for the repair prompt::

    Where column 'prinbr' exists (it is not in sqts_cost_recovery):
    - sqts_main_lear: prinbr, qn_number (shares qn_number with sqts_cost_recovery)
    - gen_supplier_codes: prinbr, supplier_code (already in the query)

Safety rules, each tested:

* Off unless the database runs the Phase 12 prompt flags in apply mode (``auto_prompt_inject``); never for ``erp_main``. This
  is the same switch the other repair context uses.
* Only runs for a column error. Any other error, or an error it cannot read, gives an empty string and the repair prompt is
  exactly what it was.
* Metadata only: it reads the live schema text the retriever already cached. No database access, no data, no files written.
* No keyword lists and nothing specific to one database or question. Tables Phase 7 found to be copies of another table are
  not offered. A table that does not exist in the live schema can never be named.
* At most ``MAX_OWNERS`` tables and ``MAX_COLUMNS`` columns each, so the block stays small. Any failure returns an empty string.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

MAX_OWNERS = 4  # most tables the block may name
MAX_COLUMNS = 10  # most columns listed per table
MAX_SHARED_SHOWN = 2  # shared columns named in the "shares ..." remark

# "Column 'x' does not exist in table 't'" (schema validator), "... on table 't'" and "... not found in any of the query's
# tables" (column registry). Anything else is not a column error this module understands.
_MISSING_COLUMN = re.compile(
    r"Column '([^']+)' (?:does not exist (?:in|on) table '([^']+)'|not found in any of the query's tables)",
    re.IGNORECASE,
)


def parse_missing_column(error: str | None) -> tuple[str, str | None] | None:
    """``(column, table or None)`` from a column error, lower-cased, or None when it is not one."""
    match = _MISSING_COLUMN.search(error or "")
    if not match:
        return None
    column = match.group(1).strip().lower()
    table = (match.group(2) or "").strip().lower() or None
    return (column, table) if column else None


def _words(name: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", name.lower()) if w}


def _document_frequency(available: Mapping[str, list[str]]) -> dict[str, int]:
    freq: dict[str, int] = {}
    for cols in available.values():
        for c in set(cols):
            freq[c] = freq.get(c, 0) + 1
    return freq


def build_column_owner_block(
    error: str | None,
    available: Mapping[str, list[str]],
    used_tables: Iterable[str],
    *,
    skip_tables: Iterable[str] = (),
) -> str:
    """The repair-prompt block for a missing column, or ``""`` when there is nothing useful to say.

    ``available`` is ``{table: [columns]}`` for the whole live schema (lower-case). ``used_tables`` are the tables of the failed
    SQL. ``skip_tables`` are tables that must not be offered (copies of another table).
    """
    parsed = parse_missing_column(error)
    if parsed is None or not available:
        return ""
    column, failing = parsed
    used = [t for t in dict.fromkeys(str(t).lower() for t in used_tables if t) if t in available]
    used_set = set(used)
    skip = {str(t).lower() for t in skip_tables}

    owners = [t for t, cols in available.items() if column in cols and t not in skip]
    if not owners:
        return ""

    total = len(available)
    freq = _document_frequency(available)

    def idf(col: str) -> float:
        return math.log(total / max(freq.get(col, 1), 1))

    def common(col: str) -> bool:
        """In a quarter of all tables (and at least three): audit and housekeeping columns, never a link between tables."""
        return freq.get(col, 0) >= max(3, 0.25 * total)

    failing_cols = set(available.get(failing or "", []))
    used_cols: set[str] = set()
    for t in used:
        used_cols.update(available[t])
    failing_words = _words(failing or "")

    def shared_with(owner: str) -> list[tuple[float, str]]:
        """Columns the owner shares with the failed table (full weight) or other used tables (half), rarest first."""
        out: list[tuple[float, str]] = []
        for c in dict.fromkeys(available[owner]):
            if c == column or common(c):
                continue
            if c in failing_cols:
                out.append((idf(c), c))
            elif c in used_cols:
                out.append((idf(c) * 0.5, c))
        return sorted(out, key=lambda x: (-x[0], x[1]))

    def rank(owner: str) -> tuple[float, str]:
        score = sum(w for w, _c in shared_with(owner))
        overlap = len(_words(owner) & failing_words) / max(len(_words(owner) | failing_words), 1)
        return (-(score + 0.5 * overlap), owner)

    already = [t for t in owners if t in used_set and t != failing]
    others = sorted((t for t in owners if t not in used_set), key=rank)
    chosen = (already + others)[:MAX_OWNERS]
    if not chosen:
        return ""

    lines: list[str] = []
    for owner in chosen:
        shared = shared_with(owner)
        listed: list[str] = [column]
        for _w, c in shared:
            if c not in listed:
                listed.append(c)
        for c in dict.fromkeys(available[owner]):
            if len(listed) >= MAX_COLUMNS:
                break
            if c not in listed:
                listed.append(c)
        listed = listed[:MAX_COLUMNS]
        if owner in used_set:
            remark = " (already in the query)"
        else:
            names = [c for _w, c in shared if c in failing_cols][:MAX_SHARED_SHOWN]
            if not names:
                names = [c for _w, c in shared][:MAX_SHARED_SHOWN]
            remark = f" (shares {', '.join(names)} with {failing or 'the query'})" if names else ""
        lines.append(f"- {owner}: {', '.join(listed)}{remark}")

    head = f"Where column '{column}' exists" + (f" (it is not in {failing}):" if failing else ":")
    tail = "Join tables only through columns they really share. Do not invent tables or columns."
    return "\n".join([head, *lines, tail])


# ---------------------------------------------------------------- live-schema index (memoised per schema text)

_INDEX_CACHE: dict[str, tuple[str, dict[str, list[str]]]] = {}
_LOCK = threading.Lock()


def _live_index(db_id: str, schema_text: str) -> dict[str, list[str]]:
    from src.sql.repair.sql_repair import extract_visible_schema_context

    digest = hashlib.sha1(schema_text.encode("utf-8", "ignore")).hexdigest()
    with _LOCK:
        cached = _INDEX_CACHE.get(db_id)
    if cached is not None and cached[0] == digest:
        return cached[1]
    index = extract_visible_schema_context(schema_text)
    with _LOCK:
        _INDEX_CACHE[db_id] = (digest, index)
    return index


def _copy_tables(db_id: str, names: Iterable[str]) -> set[str]:
    """Tables Phase 7 decided are copies of another table (never offered as the home of a column)."""
    try:
        from src.sql.profiles.hints import load_authority_index

        index = load_authority_index(db_id)
        if index is None:
            return set()
        return {n for n in names if index.primary_for(n)}
    except Exception as exc:  # noqa: BLE001 - optional refinement
        logger.debug("Table authority unavailable for column owners (%s): %s", db_id, exc)
        return set()


async def column_owner_context(retriever: Any, error: str | None, used_tables: Iterable[str]) -> str:
    """Block for the repair prompt, or ``""`` (flags off, not a column error, nothing found, any failure). Never raises."""
    try:
        if parse_missing_column(error) is None:
            return ""
        from src.sql.schema_retrieval import _route_bridge_mode

        db_id = getattr(retriever, "db_id", None)
        if not db_id or _route_bridge_mode(db_id) != "apply":
            return ""
        schema_text = await retriever._fetch_full_schema()
        if not schema_text:
            return ""
        available = _live_index(db_id, schema_text)
        column = (parse_missing_column(error) or ("", None))[0]
        skip = _copy_tables(db_id, [t for t, cols in available.items() if column in cols])
        return build_column_owner_block(error, available, used_tables, skip_tables=skip)
    except Exception as exc:  # noqa: BLE001 - extra context only; the repair prompt is unchanged on any failure
        logger.debug("Column owner context unavailable: %s", exc)
        return ""


__all__ = ["MAX_COLUMNS", "MAX_OWNERS", "build_column_owner_block", "column_owner_context", "parse_missing_column"]
