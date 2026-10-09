"""Base SQL dialect profile definition."""

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
