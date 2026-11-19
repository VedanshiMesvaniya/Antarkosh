"""Deterministic SQL syntax normalisation: the target database's own spelling of a statement, without asking a model.

Models borrow syntax from other databases: ``LIMIT 10`` for SQL Server, backticks for PostgreSQL, ``::`` casts for T-SQL,
``ILIKE`` for Oracle. ``normalize_sql`` writes a parseable statement the way the target dialect spells it, so validation and
repair see correct text; ``dialect_syntax_fix`` is the repair for a syntax ERROR that needs no model call: it re-reads the failed
statement in the dialects models borrow from and writes it in the target one.

Both are pure and fail safe: anything that does not parse as one statement is returned unchanged (or ``None``). The executor still
parses and serialises every statement itself (defence in depth), so executed SQL is the same either way.

Origin: new (no previous file)
"""

from __future__ import annotations

import sqlglot

_BORROWED_FROM = ("mysql", "postgres", "sqlite", "tsql")


def normalize_sql(sql: str, dialect: str | None) -> str:
    """``sql`` as the target dialect writes it, or unchanged if it is not exactly one parseable statement."""
    try:
        out = sqlglot.parse_one(sql, read=dialect or None).sql(dialect=dialect or None)
        return out if out.strip() else sql
    except Exception:  # noqa: BLE001 - not parseable: leave it to the existing guards
        return sql


def dialect_syntax_fix(sql: str, dialect: str | None) -> str | None:
    """A rewrite of a statement that failed with a syntax error, or ``None`` when no reading yields a different valid statement."""
    target = dialect or None
    for reader in dict.fromkeys((target, *_BORROWED_FROM)):
        try:
            fixed = sqlglot.parse_one(sql, read=reader).sql(dialect=target)
            if fixed.strip() and fixed.strip() != sql.strip():
                sqlglot.parse_one(fixed, read=target)  # the rewrite must itself be valid in the target dialect
                return fixed
        except Exception:  # noqa: BLE001 - try the next reading
            continue
    return None


__all__ = ["dialect_syntax_fix", "normalize_sql"]
