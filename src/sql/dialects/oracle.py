"""Oracle dialect profile.

Introspection reads the ALL_* catalog views for the CURRENT schema of the session
(``SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')``): the connected user's own objects plus
anything that schema owns that the user may see. Identifiers come back uppercase, as
stored; unquoted Oracle identifiers are case-insensitive so generated SQL still works.
Queries touch catalog metadata only.

Status: the SQL parses and round-trips through sqlglot (tests/sql/test_dialect_profiles_mssql_oracle.py)
but has not yet been run against a live Oracle instance. Requires Oracle 12c+ for
``FETCH FIRST`` (added by the executor's row cap).
"""

from __future__ import annotations

from src.sql.dialects.base import SQLDialectProfile

ORACLE_PROFILE = SQLDialectProfile(
    key="oracle",
    name="Oracle",
    sqlglot_dialect="oracle",
    # One row per column, same shape as the MySQL/PostgreSQL profiles. Oracle's connector
    # lower-cases result column names, so the aliases below match the other engines.
    # ALL_TAB_COLUMNS omits hidden columns; BIN$ objects are recycle-bin leftovers.
    schema_query="""
        SELECT c.table_name AS table_name,
               c.column_name AS column_name,
               c.data_type AS data_type,
               cc.comments AS column_comment
        FROM all_tab_columns c
        LEFT JOIN all_col_comments cc
          ON cc.owner = c.owner AND cc.table_name = c.table_name AND cc.column_name = c.column_name
        WHERE c.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
          AND c.table_name NOT LIKE 'BIN$%'
        ORDER BY c.table_name, c.column_id
    """,
    # Child column joined to the parent column at the same constraint position, so composite
    # keys pair up column-for-column. The parent table may live in another schema.
    fk_query="""
        SELECT ac.table_name AS table_name,
               acc.column_name AS column_name,
               pk.table_name AS referenced_table_name,
               pkc.column_name AS referenced_column_name
        FROM all_constraints ac
        JOIN all_cons_columns acc
          ON acc.owner = ac.owner AND acc.constraint_name = ac.constraint_name
        JOIN all_constraints pk
          ON pk.owner = ac.r_owner AND pk.constraint_name = ac.r_constraint_name
        JOIN all_cons_columns pkc
          ON pkc.owner = pk.owner AND pkc.constraint_name = pk.constraint_name
         AND pkc.position = acc.position
        WHERE ac.constraint_type = 'R'
          AND ac.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
        ORDER BY ac.table_name, ac.constraint_name, acc.position
    """,
    # Catalog extras (auto-schema Phase 3), same current-schema scope as schema_query.
    table_query="""
        SELECT tc.table_name AS table_name, tc.table_type AS table_type, tc.comments AS table_comment
        FROM all_tab_comments tc
        WHERE tc.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
          AND tc.table_name NOT LIKE 'BIN$%'
        ORDER BY tc.table_name
    """,
    key_query="""
        SELECT ac.table_name AS table_name,
               CASE ac.constraint_type WHEN 'P' THEN 'PRIMARY KEY' ELSE 'UNIQUE' END AS constraint_type,
               ac.constraint_name AS constraint_name,
               acc.column_name AS column_name,
               acc.position AS ordinal_position
        FROM all_constraints ac
        JOIN all_cons_columns acc ON acc.owner = ac.owner AND acc.constraint_name = ac.constraint_name
        WHERE ac.constraint_type IN ('P', 'U') AND ac.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
        ORDER BY ac.table_name, ac.constraint_name, acc.position
    """,
    # num_rows comes from optimizer statistics and is NULL until statistics are gathered.
    row_estimate_query="""
        SELECT t.table_name AS table_name, t.num_rows AS row_estimate
        FROM all_tables t
        WHERE t.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
    """,
    date_functions="""
    Today: TRUNC(SYSDATE)
    This month: TO_CHAR(SYSDATE, 'YYYY-MM')
    Last N days: TRUNC(SYSDATE) - N
    Last month: ADD_MONTHS(TRUNC(SYSDATE), -1)
    This year: EXTRACT(YEAR FROM SYSDATE)
    Between two dates: column BETWEEN ADD_MONTHS(TRUNC(SYSDATE), -1) AND TRUNC(SYSDATE)
    Limit rows: FETCH FIRST n ROWS ONLY (Oracle has no LIMIT clause)
    """,
)


def get_profile() -> SQLDialectProfile:
    """Return the Oracle dialect profile."""
    return ORACLE_PROFILE
