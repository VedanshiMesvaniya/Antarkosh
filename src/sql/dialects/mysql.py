"""MySQL dialect profile."""

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
