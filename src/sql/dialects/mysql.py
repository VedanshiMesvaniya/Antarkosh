"""MySQL dialect profile.

Origin: extracted from src/core/sql_dialects.py (MYSQL_PROFILE, get_profile)
"""

from __future__ import annotations

from src.sql.dialects.base import SQLDialectProfile

MYSQL_PROFILE = SQLDialectProfile(
    key="mysql",
    name="MySQL",
    sqlglot_dialect="mysql",
    schema_query="""
        SELECT table_name, column_name, data_type, column_comment
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
        ORDER BY table_name, ordinal_position;
    """,
    fk_query="""
        SELECT table_name, column_name, referenced_table_name, referenced_column_name
        FROM information_schema.key_column_usage
        WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL;
    """,
    # Catalog extras (auto-schema Phase 3). Every column is aliased: MySQL 8 returns
    # information_schema columns in UPPER case unless aliased.
    table_query="""
        SELECT table_name AS table_name, table_type AS table_type, table_comment AS table_comment
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
        ORDER BY table_name;
    """,
    key_query="""
        SELECT tc.table_name AS table_name, tc.constraint_type AS constraint_type,
               tc.constraint_name AS constraint_name, kcu.column_name AS column_name,
               kcu.ordinal_position AS ordinal_position
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON kcu.constraint_schema = tc.constraint_schema
         AND kcu.constraint_name = tc.constraint_name
         AND kcu.table_name = tc.table_name
        WHERE tc.table_schema = DATABASE() AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
        ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position;
    """,
    row_estimate_query="""
        SELECT table_name AS table_name, table_rows AS row_estimate
        FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE';
    """,
    date_functions="""
    Today: CURDATE()
    This month: DATE_FORMAT(CURDATE(), '%Y-%m')
    Last N days: CURDATE() - INTERVAL N DAY
    Last month: CURDATE() - INTERVAL 1 MONTH
    This year: YEAR(CURDATE())
    Between two dates: column BETWEEN CURDATE() - INTERVAL 1 MONTH AND CURDATE()
    """,
)


def get_profile() -> SQLDialectProfile:
    """Return the MySQL dialect profile."""
    return MYSQL_PROFILE
