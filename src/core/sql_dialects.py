"""SQL dialect facts for the Text-to-SQL retrieval layer.

This intentionally stays data, not an interface: SQLite, MySQL and PostgreSQL differ mainly
in four facts (what to call the dialect in the NL2SQL prompt, which sqlglot
dialect to parse/serialize with, how to introspect the schema, and which date
syntax examples to show in prompt hints). Adding a new engine means adding one
entry here.

Connection handling is NOT unified here — the aiosqlite/aiomysql driver APIs
differ enough (context-managed connection vs. explicit cursor) that forcing a
shared interface would just be boilerplate around a branch. That branch lives
directly in db_client.py, where it's one `if` on `settings.db_engine`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SQLDialectProfile:
    """Facts about one SQL engine, used to drive prompting, parsing, and introspection."""

    key: str
    """Registry key for this profile (matches settings.db_engine values, e.g. "mysql")."""

    name: str
    """Human-readable name interpolated into the NL2SQL system prompt (e.g. "MySQL expert")."""

    sqlglot_dialect: str
    """Dialect string passed to sqlglot.parse_one()/tree.sql() for parsing and re-serialization."""

    schema_query: str
    """Read-only query used to introspect available tables/columns for this engine."""

    date_functions: str
    """Dialect-specific date/time examples appended to the NL2SQL prompt."""

    fk_query: str
    """Read-only query to introspect FK constraints, where a single query suffices (MySQL). Empty for engines that need per-table introspection (SQLite) — see s12b's _fetch_sqlite_foreign_keys."""


DIALECTS: dict[str, SQLDialectProfile] = {
    "sqlite": SQLDialectProfile(
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
    ),
    "mysql": SQLDialectProfile(
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
    ),
    "postgresql": SQLDialectProfile(
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
        date_functions="""
        Today: CURRENT_DATE
        This month: TO_CHAR(CURRENT_DATE, 'YYYY-MM')
        Last N days: CURRENT_DATE - INTERVAL 'N days'
        Last month: CURRENT_DATE - INTERVAL '1 month'
        This year: EXTRACT(YEAR FROM CURRENT_DATE)
        Between two dates: column BETWEEN CURRENT_DATE - INTERVAL '1 month' AND CURRENT_DATE
        """,
    ),
    # SQL Server, if/when needed: add an entry here (sqlglot_dialect="tsql",
    # schema_query against INFORMATION_SCHEMA.COLUMNS or sys.columns). Not
    # added speculatively — there's no instance to validate the LIMIT->TOP
    # transpilation or a read-only login against yet.
}


def get_dialect_profile(engine: str) -> SQLDialectProfile:
    """Look up the dialect profile for the configured engine.

    Raises:
        ValueError: If the engine isn't in DIALECTS (fails fast at startup-adjacent
            code paths rather than producing confusing downstream sqlglot errors).
    """
    try:
        return DIALECTS[engine]
    except KeyError:
        raise ValueError(
            f"Unsupported db_engine {engine!r}. Supported engines: {list(DIALECTS)}"
        ) from None
