"""Microsoft SQL Server (T-SQL) dialect profile.

Introspection reads the sys.* catalog views and covers the caller's DEFAULT schema
(``SCHEMA_ID()``), the same single-schema scope MySQL (``DATABASE()``) and
PostgreSQL (``current_schema()``) use. Queries touch catalog metadata only.

Status: the SQL parses and round-trips through sqlglot (tests/sql/test_dialect_profiles_mssql_oracle.py)
but has not yet been run against a live SQL Server instance.
"""

from __future__ import annotations

from src.sql.dialects.base import SQLDialectProfile

MSSQL_PROFILE = SQLDialectProfile(
    key="mssql",
    name="SQL Server (T-SQL)",
    sqlglot_dialect="tsql",
    # One row per column, same shape as the MySQL/PostgreSQL profiles so the shared
    # splitter and formatter apply. Column comments come from the MS_Description
    # extended property. Tables and views, excluding Microsoft-shipped objects.
    # No trailing semicolon: the executor re-serialises the statement anyway.
    schema_query="""
        SELECT t.name AS table_name,
               c.name AS column_name,
               ty.name AS data_type,
               CAST(ep.value AS NVARCHAR(4000)) AS column_comment
        FROM sys.columns AS c
        JOIN sys.objects AS t
          ON t.object_id = c.object_id AND t.type IN ('U', 'V') AND t.is_ms_shipped = 0
        JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.extended_properties AS ep
          ON ep.major_id = c.object_id AND ep.minor_id = c.column_id
         AND ep.name = 'MS_Description' AND ep.[class] = 1
        WHERE t.schema_id = SCHEMA_ID()
        ORDER BY t.name, c.column_id
    """,
    # sys.foreign_key_columns pairs composite keys column-for-column.
    fk_query="""
        SELECT pt.name AS table_name,
               pc.name AS column_name,
               rt.name AS referenced_table_name,
               rc.name AS referenced_column_name
        FROM sys.foreign_key_columns AS fkc
        JOIN sys.objects AS pt ON pt.object_id = fkc.parent_object_id
        JOIN sys.columns AS pc
          ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.objects AS rt ON rt.object_id = fkc.referenced_object_id
        JOIN sys.columns AS rc
          ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        WHERE pt.schema_id = SCHEMA_ID()
        ORDER BY pt.name, fkc.constraint_object_id, fkc.constraint_column_id
    """,
    # Catalog extras (auto-schema Phase 3), same default-schema scope as schema_query.
    table_query="""
        SELECT t.name AS table_name,
               CASE t.type WHEN 'V' THEN 'VIEW' ELSE 'BASE TABLE' END AS table_type,
               CAST(ep.value AS NVARCHAR(4000)) AS table_comment
        FROM sys.objects AS t
        LEFT JOIN sys.extended_properties AS ep
          ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description' AND ep.[class] = 1
        WHERE t.type IN ('U', 'V') AND t.is_ms_shipped = 0 AND t.schema_id = SCHEMA_ID()
        ORDER BY t.name
    """,
    key_query="""
        SELECT t.name AS table_name,
               CASE WHEN i.is_primary_key = 1 THEN 'PRIMARY KEY' ELSE 'UNIQUE' END AS constraint_type,
               i.name AS constraint_name,
               c.name AS column_name,
               ic.key_ordinal AS ordinal_position
        FROM sys.indexes AS i
        JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables AS t ON t.object_id = i.object_id
        WHERE (i.is_primary_key = 1 OR i.is_unique_constraint = 1)
          AND ic.is_included_column = 0 AND t.is_ms_shipped = 0 AND t.schema_id = SCHEMA_ID()
        ORDER BY t.name, i.name, ic.key_ordinal
    """,
    # sys.partitions.rows is approximate and needs only metadata visibility (unlike the DMVs).
    row_estimate_query="""
        SELECT t.name AS table_name, SUM(p.rows) AS row_estimate
        FROM sys.tables AS t
        JOIN sys.partitions AS p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        WHERE t.is_ms_shipped = 0 AND t.schema_id = SCHEMA_ID()
        GROUP BY t.name
    """,
    date_functions="""
    Today: CAST(GETDATE() AS DATE)
    This month: FORMAT(GETDATE(), 'yyyy-MM')
    Last N days: DATEADD(DAY, -N, CAST(GETDATE() AS DATE))
    Last month: DATEADD(MONTH, -1, CAST(GETDATE() AS DATE))
    This year: YEAR(GETDATE())
    Between two dates: column BETWEEN DATEADD(MONTH, -1, CAST(GETDATE() AS DATE)) AND CAST(GETDATE() AS DATE)
    Limit rows: SELECT TOP n ... (T-SQL has no LIMIT clause)
    """,
)


def get_profile() -> SQLDialectProfile:
    """Return the SQL Server dialect profile."""
    return MSSQL_PROFILE
