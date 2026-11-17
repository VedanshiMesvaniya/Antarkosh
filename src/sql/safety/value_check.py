"""Prove that a filter value does not exist: ``WHERE t.active = '1'`` on a column that only holds 'Y' and 'N'.

A model cannot know the codes a database uses for a flag, and asking for 'active' it guesses 1, '1', true. The query is valid
and returns nothing, and the empty answer used to be accepted as real. For an ALREADY-empty result this reads the distinct values
of each column the WHERE clause compares with a literal (one cheap read-only query per column, at most three, 5 s timeout); when
the column has only a few values and the literal is not one of them, that is proof, not a guess. The note is always kept for the
answer. A repair is forced only when every such literal is a flag or number guess (``'1'``, ``true``, ``'Y'``): a value the user
named ("Boston") may simply not exist, which is a valid answer and must not cost repair calls. Any problem (unparsable SQL,
unreadable column, slow database) gives no note and changes nothing.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import sqlglot
from sqlglot import exp

from src.core.db_client import run_readonly_query

logger = logging.getLogger(__name__)

MAX_COLUMNS = 3
MAX_DOMAIN = 10  # a column with more distinct values than this is not a code list: nothing is proven
TIMEOUT_SECONDS = 5.0
_GUESSES = frozenset({"true", "false", "yes", "no", "y", "n", "t", "f"})


def _is_guess(literal: str) -> bool:
    """A boolean or number: what a model writes when it has to guess how a flag is stored."""
    lit = literal.strip().lower()
    return lit in _GUESSES or lit.lstrip("-").replace(".", "", 1).isdigit()


def literal_filters(sql: str, dialect: str | None = None) -> list[tuple[exp.Table, str, list[str]]]:
    """``(table node, column, literals)`` for each ``column = 'x'`` / ``column IN ('x', 'y')`` in the WHERE clause."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001 - not parsable: nothing to check
        return []
    if tree is None or tree.find(exp.Where) is None:
        return []
    tables = list(tree.find_all(exp.Table))
    by_alias = {(t.alias or t.name).lower(): t for t in tables} | {t.name.lower(): t for t in tables}
    sole = tables[0] if len({t.name.lower() for t in tables}) == 1 else None
    found: dict[tuple[str, str], tuple[exp.Table, str, list[str]]] = {}
    for where in tree.find_all(exp.Where):
        for node in where.find_all(exp.EQ, exp.In):
            col = node.this
            values = [node.expression] if isinstance(node, exp.EQ) else list(node.expressions)
            if not isinstance(col, exp.Column) or not values or not all(isinstance(v, exp.Literal) for v in values):
                continue
            tbl = by_alias.get(col.table.lower()) if col.table else sole
            if tbl is not None:
                found.setdefault((tbl.name.lower(), col.name.lower()), (tbl, col.name, [str(v.this) for v in values]))
    return list(found.values())[:MAX_COLUMNS]


def _distinct_query(table: exp.Table, column: str, dialect: str | None) -> str:
    bare = exp.Table(this=table.this.copy(), db=table.args["db"].copy() if table.args.get("db") else None)
    return exp.select(exp.column(column, quoted=True)).distinct().from_(bare).limit(MAX_DOMAIN + 1).sql(dialect=dialect or None)


async def value_mismatch_note(
    sql: str,
    dialect: str | None,
    db_id: str,
    *,
    runner: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> tuple[str, bool]:
    """``(note, repairable)``: the note names each filter whose literal is not among the column's few real values (``""`` if
    none); ``repairable`` is True when every such literal is a flag or number guess, so a repair is worth its cost."""
    try:
        run = runner or run_readonly_query
        notes: list[str] = []
        guesses: list[bool] = []
        for table, column, literals in literal_filters(sql, dialect):
            try:
                rows = await asyncio.wait_for(run(_distinct_query(table, column, dialect), db_id=db_id), timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - one unreadable column does not stop the others
                logger.debug("Value check skipped for %s.%s: %s", table.name, column, exc)
                continue
            domain = [str(v).strip() for r in rows for v in r.values() if v is not None]
            if not domain or len(rows) > MAX_DOMAIN:
                continue
            if not {lit.strip().casefold() for lit in literals} & {d.casefold() for d in domain}:
                shown = ", ".join(f"'{d}'" for d in sorted(domain))
                used = ", ".join(f"'{lit}'" for lit in literals)
                notes.append(f"`{table.name}.{column}` only holds {shown}; the query filtered it with {used}, which never occurs")
                guesses.append(all(_is_guess(lit) for lit in literals))
        if not notes:
            return "", False
        return "_Value check: " + "; ".join(notes) + ". Use a value that exists._", all(guesses)
    except Exception as exc:  # noqa: BLE001 - never fails a query
        logger.debug("Value check skipped: %s", exc)
        return "", False


__all__ = ["literal_filters", "value_mismatch_note"]
