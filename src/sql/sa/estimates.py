"""Row estimates for every schema at once, from the engine's own statistics (never COUNT(*)).

One catalog query per engine, returning (table_schema, table_name, row_estimate). SQLite has no
cheap estimate. Run through the same SQLAlchemy connection as the reflection; any failure just
means "row estimates unavailable".

Origin: new (no previous file)
"""

from __future__ import annotations

ROW_ESTIMATE_SQL: dict[str, str] = {
    # reltuples is the planner's estimate (-1 before the first ANALYZE on PostgreSQL 14+).
    "postgresql": """
        SELECT n.nspname AS table_schema, c.relname AS table_name, c.reltuples::bigint AS row_estimate
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'f') AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
    """,
    "mysql": """
        SELECT table_schema AS table_schema, table_name AS table_name, table_rows AS row_estimate
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
    """,
    # sys.partitions.rows is approximate and needs only metadata visibility.
    "mssql": """
        SELECT s.name AS table_schema, t.name AS table_name, SUM(p.rows) AS row_estimate
        FROM sys.tables AS t
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        JOIN sys.partitions AS p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        WHERE t.is_ms_shipped = 0
        GROUP BY s.name, t.name
    """,
    # num_rows comes from optimizer statistics and is NULL until statistics are gathered.
    "oracle": """
        SELECT t.owner AS table_schema, t.table_name AS table_name, t.num_rows AS row_estimate
        FROM all_tables t
    """,
}

__all__ = ["ROW_ESTIMATE_SQL"]
