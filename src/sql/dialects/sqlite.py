"""SQLite dialect profile.

Origin: extracted from src/core/sql_dialects.py (SQLITE_PROFILE, get_profile)
"""

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
    # Catalog extras (auto-schema Phase 3). pragma_table_info() is the table-valued form of PRAGMA
    # table_info; "ON 1 = 1" is a lateral join (a bare comma/CROSS JOIN trips db_client's Cartesian guard).
    # SQLite has no table comments and no cheap row estimate, so those stay empty.
    column_query="""
        SELECT m.name AS table_name, p.name AS column_name, p.type AS data_type, NULL AS column_comment
        FROM sqlite_master AS m
        JOIN pragma_table_info(m.name) AS p ON 1 = 1
        WHERE m.type IN ('table', 'view') AND substr(m.name, 1, 7) <> 'sqlite_'
        ORDER BY m.name, p.cid;
    """,
    table_query="""
        SELECT name AS table_name,
               CASE type WHEN 'view' THEN 'VIEW' ELSE 'BASE TABLE' END AS table_type,
               NULL AS table_comment
        FROM sqlite_master
        WHERE type IN ('table', 'view') AND substr(name, 1, 7) <> 'sqlite_'
        ORDER BY name;
    """,
    key_query="""
        SELECT m.name AS table_name, 'PRIMARY KEY' AS constraint_type, 'pk_' || m.name AS constraint_name,
               p.name AS column_name, p.pk AS ordinal_position
        FROM sqlite_master AS m
        JOIN pragma_table_info(m.name) AS p ON 1 = 1
        WHERE m.type = 'table' AND p.pk > 0 AND substr(m.name, 1, 7) <> 'sqlite_'
        ORDER BY m.name, p.pk;
    """,
)


def get_profile() -> SQLDialectProfile:
    """Return the SQLite dialect profile."""
    return SQLITE_PROFILE
