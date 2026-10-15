"""PostgreSQL dialect profile.

Origin: extracted from src/core/sql_dialects.py (POSTGRESQL_PROFILE, get_profile)
"""

from __future__ import annotations

from src.sql.dialects.base import SQLDialectProfile

POSTGRESQL_PROFILE = SQLDialectProfile(
    key="postgresql",
    name="PostgreSQL",
    sqlglot_dialect="postgres",
    # Same column-per-row shape as MySQL so the shared formatter/splitter apply.
    # information_schema only lists columns the connected role may access;
    # comments come from pg_description. current_schema() is normally "public".
    schema_query="""
        SELECT c.table_name, c.column_name,
               CASE c.data_type
                   WHEN 'ARRAY' THEN substr(c.udt_name, 2) || '[]'
                   WHEN 'USER-DEFINED' THEN c.udt_name
                   ELSE c.data_type
               END AS data_type,
               pg_catalog.col_description(
                   (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass,
                   c.ordinal_position
               ) AS column_comment
        FROM information_schema.columns AS c
        WHERE c.table_schema = current_schema()
        ORDER BY c.table_name, c.ordinal_position;
    """,
    # pg_constraint (not information_schema) so composite foreign keys pair up
    # column-for-column instead of producing a cross product. JOIN LATERAL ... ON TRUE,
    # not CROSS JOIN: the Cartesian guard in db_client would clamp the result to 100 rows.
    fk_query="""
        SELECT cl.relname AS table_name,
               a.attname  AS column_name,
               fcl.relname AS referenced_table_name,
               fa.attname AS referenced_column_name
        FROM pg_catalog.pg_constraint AS c
        JOIN pg_catalog.pg_class AS cl ON cl.oid = c.conrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = cl.relnamespace
        JOIN pg_catalog.pg_class AS fcl ON fcl.oid = c.confrelid
        JOIN LATERAL unnest(c.conkey, c.confkey) AS k(attnum, fattnum) ON TRUE
        JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        JOIN pg_catalog.pg_attribute AS fa ON fa.attrelid = c.confrelid AND fa.attnum = k.fattnum
        WHERE c.contype = 'f' AND n.nspname = current_schema()
        ORDER BY cl.relname, a.attname;
    """,
    # Catalog extras (auto-schema Phase 3). Same scope as schema_query: current_schema().
    # Materialized views are not listed: information_schema.columns (schema_query) cannot see them.
    table_query="""
        SELECT c.relname AS table_name,
               CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'BASE TABLE' END AS table_type,
               pg_catalog.obj_description(c.oid, 'pg_class') AS table_comment
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'f')
        ORDER BY c.relname;
    """,
    # pg_constraint + WITH ORDINALITY so composite keys keep their column order. JOIN LATERAL ... ON TRUE
    # (not CROSS JOIN) so db_client's Cartesian guard does not clamp the result to 100 rows.
    key_query="""
        SELECT cl.relname AS table_name,
               CASE c.contype WHEN 'p' THEN 'PRIMARY KEY' ELSE 'UNIQUE' END AS constraint_type,
               c.conname AS constraint_name,
               a.attname AS column_name,
               k.ord AS ordinal_position
        FROM pg_catalog.pg_constraint AS c
        JOIN pg_catalog.pg_class AS cl ON cl.oid = c.conrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = cl.relnamespace
        JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype IN ('p', 'u') AND n.nspname = current_schema()
        ORDER BY cl.relname, c.conname, k.ord;
    """,
    # reltuples is the planner's estimate (-1 before the first ANALYZE on PostgreSQL 14+).
    row_estimate_query="""
        SELECT c.relname AS table_name, c.reltuples::bigint AS row_estimate
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'f');
    """,
    date_functions="""
    Today: CURRENT_DATE
    This month: TO_CHAR(CURRENT_DATE, 'YYYY-MM')
    Last N days: CURRENT_DATE - INTERVAL 'N days'
    Last month: CURRENT_DATE - INTERVAL '1 month'
    This year: EXTRACT(YEAR FROM CURRENT_DATE)
    Between two dates: column BETWEEN CURRENT_DATE - INTERVAL '1 month' AND CURRENT_DATE
    """,
)


def get_profile() -> SQLDialectProfile:
    """Return the PostgreSQL dialect profile."""
    return POSTGRESQL_PROFILE
