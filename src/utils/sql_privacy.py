"""Compatibility shim for src.utils.sql_privacy -> src.sql.safety.sql_privacy."""

from __future__ import annotations

from src.sql.safety.sql_privacy import (
    _MAX_SQL_CHARS,
    _SQL_MARKER,
    _SQL_QUERY_RE,
    _is_sql_generated,
    sanitize_assistant_turn,
)

__all__ = [
    "_MAX_SQL_CHARS",
    "_SQL_MARKER",
    "_SQL_QUERY_RE",
    "_is_sql_generated",
    "sanitize_assistant_turn",
]
