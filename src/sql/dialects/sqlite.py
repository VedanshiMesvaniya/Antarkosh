"""SQLite dialect profile."""

from __future__ import annotations

from src.sql.dialects.base import SQLDialectProfile

SQLITE_PROFILE = SQLDialectProfile(
    key="sqlite",
    name="SQLite",
    sqlglot_dialect="sqlite",
    schema_query="SELECT name, sql FROM sqlite_master WHERE type='table';",
    date_functions="""
    Today: date('now')
    This month: strftime('%Y-%m', 'now')
    Last N days: date('now', '-N days')
    Last month: date('now', '-1 month')
    This year: strftime('%Y', 'now')
    Between two dates: column BETWEEN date('now','-1 month') AND date('now')
    """,
    fk_query="",
)


def get_profile() -> SQLDialectProfile:
    """Return the SQLite dialect profile."""
    return SQLITE_PROFILE
