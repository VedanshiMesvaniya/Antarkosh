"""Base SQL dialect profile definition.

Origin: extracted from src/core/sql_dialects.py (SQLDialectProfile)
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

    # --- Optional catalog queries used by the auto-schema extractor (Phase 3). All read catalog
    # --- metadata only, never data rows. Empty string = this engine does not offer it; the
    # --- extractor then skips that part and says so in its report.
    column_query: str = ""
    """One row per column (table_name, column_name, data_type, column_comment), for engines whose
    ``schema_query`` does not already return that shape (SQLite). Empty = use ``schema_query`` rows."""

    table_query: str = ""
    """One row per table/view: table_name, table_type, table_comment."""

    key_query: str = ""
    """One row per key column: table_name, constraint_type ('PRIMARY KEY' | 'UNIQUE'), constraint_name, column_name, ordinal_position."""

    row_estimate_query: str = ""
    """table_name, row_estimate: the engine's own statistics, never a COUNT(*) over the data."""
