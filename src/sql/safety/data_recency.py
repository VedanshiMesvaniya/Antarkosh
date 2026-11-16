"""Tell the user when an empty result comes from data that ends before the requested window.

Typical case: "events in the last 90 days" compiles to ``date_col >= DATEADD(DAY, -90, GETDATE())``. When the newest row of
that table is months or years old (an archive, a table that stopped being written, a snapshot) the query is correct and still
returns nothing, and "no data found" tells the user nothing about why or what to ask instead.

Only for a result that is ALREADY empty, and only one cheap read-only ``SELECT MAX(<column>)`` per compared column (at most
two). The answer is not changed and nothing is retried: a short factual note is appended to the empty result so the answer
can say where the data really ends. Every problem (unparsable SQL, a column that cannot be read, a slow database, an odd value)
gives no note; it can never fail a query.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

import sqlglot
from sqlglot import exp

from src.core.db_client import run_readonly_query

logger = logging.getLogger(__name__)

MAX_COLUMNS = 2
MIN_DAYS_BEHIND = 1  # data from today or yesterday is not "behind"
QUERY_TIMEOUT_SECONDS = 5.0

_NOW_FUNCTIONS = frozenset(
    {"now", "getdate", "getutcdate", "sysdate", "sysdatetime", "systimestamp", "curdate", "current_date", "current_timestamp", "localtimestamp"}
)
_SQLITE_DATE_FUNCTIONS = frozenset({"date", "datetime", "strftime", "julianday"})
_COMPARISONS = (exp.GT, exp.GTE, exp.LT, exp.LTE)


def _mentions_now(node: exp.Expression) -> bool:
    if node.find(exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentDatetime, exp.CurrentTime) is not None:
        return True
    for fn in node.find_all(exp.Anonymous):
        name = str(fn.name).lower()
        if name in _NOW_FUNCTIONS:
            return True
        if name in _SQLITE_DATE_FUNCTIONS and any(isinstance(a, exp.Literal) and str(a.this).lower() == "now" for a in fn.expressions):
            return True
    for fn in node.find_all(exp.Date, exp.Datetime, exp.TimeToStr):
        if any(isinstance(a, exp.Literal) and str(a.this).lower() == "now" for a in fn.walk()):
            return True
    return False


def relative_date_columns(sql: str, dialect: str | None = None) -> list[tuple[exp.Table, str]]:
    """``(table node, column name)`` for each column the WHERE clause compares with an expression built from the current date."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001 - not parsable: no note
        return []
    if tree is None or tree.find(exp.Where) is None:
        return []
    tables = list(tree.find_all(exp.Table))
    by_alias = {(t.alias or t.name).lower(): t for t in tables}
    by_alias.update({t.name.lower(): t for t in tables})
    sole = tables[0] if len({t.name.lower() for t in tables}) == 1 else None

    pairs: list[tuple[exp.Table, str]] = []

    def add(col: exp.Expression | None) -> None:
        if col is None or not isinstance(col, exp.Column) or not col.name:
            return
        tbl = by_alias.get(col.table.lower()) if col.table else sole
        if tbl is None:
            return
        key = (tbl.name.lower(), col.name.lower())
        if key not in {(t.name.lower(), c.lower()) for t, c in pairs}:
            pairs.append((tbl, col.name))

    for where in tree.find_all(exp.Where):
        for node in where.find_all(*_COMPARISONS):
            left, right = node.this, node.expression
            if left is None or right is None:
                continue
            if _mentions_now(right) and not _mentions_now(left):
                add(left.find(exp.Column))
            elif _mentions_now(left) and not _mentions_now(right):
                add(right.find(exp.Column))
        for node in where.find_all(exp.Between):
            bounds = [node.args.get("low"), node.args.get("high")]
            if any(b is not None and _mentions_now(b) for b in bounds) and not _mentions_now(node.this):
                add(node.this.find(exp.Column))
    return pairs[:MAX_COLUMNS]


def _max_query(table: exp.Table, column: str, dialect: str | None) -> str:
    bare = exp.Table(this=table.this.copy(), db=table.args.get("db").copy() if table.args.get("db") else None)
    return exp.select(exp.Max(this=exp.column(column, quoted=True))).from_(bare).sql(dialect=dialect or None)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()[:19].replace("T", " ")).date()
        except ValueError:
            return None
    return None


async def data_recency_note(
    sql: str,
    dialect: str | None,
    db_id: str,
    *,
    today: date | None = None,
    runner: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    timeout: float = QUERY_TIMEOUT_SECONDS,
) -> str:
    """A short markdown note naming how far behind today the data of the compared date column(s) ends, or ``""``."""
    try:
        pairs = relative_date_columns(sql, dialect)
        if not pairs:
            return ""
        run = runner or run_readonly_query
        today = today or datetime.now(UTC).date()
        notes: list[str] = []
        for table, column in pairs:
            try:
                rows = await asyncio.wait_for(run(_max_query(table, column, dialect), db_id=db_id), timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - one unreadable column does not stop the other
                logger.debug("Data recency check skipped for %s.%s: %s", table.name, column, exc)
                continue
            if not rows:
                continue
            latest = _as_date(next(iter(rows[0].values()), None))
            if latest is None:
                continue
            behind = (today - latest).days
            if behind >= MIN_DAYS_BEHIND:
                notes.append(f"the newest `{column}` value in `{table.name}` is {latest.isoformat()} ({behind} days before today)")
        if not notes:
            return ""
        return (
            "_Data recency: " + "; ".join(notes) + ". A period counted back from today holds no records when the data ends "
            "earlier; say so and offer the period that ends on that date._"
        )
    except Exception as exc:  # noqa: BLE001 - never fails a query
        logger.debug("Data recency note skipped: %s", exc)
        return ""


__all__ = ["data_recency_note", "relative_date_columns"]
