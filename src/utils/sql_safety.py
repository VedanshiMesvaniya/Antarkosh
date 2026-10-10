"""Backward-compatibility shim for src.utils.sql_safety.

Re-exports all symbols from src.sql.safety.sql_safety.
"""

from __future__ import annotations

from src.sql.safety.sql_safety import (
    ALLOWED_METRIC_WORDS,
    COLUMN_GLOSSARY_PATH,
    DANGEROUS_FUNCTIONS,
    DESTRUCTIVE_TYPES,
    GLOSSARY_PATH,
    MSSQL_DANGEROUS_FUNCTIONS,
    MYSQL_DANGEROUS_FUNCTIONS,
    ORACLE_DANGEROUS_PACKAGE_PREFIXES,
    PER_ENGINE_DANGEROUS_FUNCTIONS,
    POSTGRESQL_DANGEROUS_FUNCTIONS,
    REPO_ROOT,
    SQLITE_DANGEROUS_FUNCTIONS,
    _ORACLE_DANGEROUS_PREFIX_RE,
    _clean_ident,
    _normalize_schema_context,
    check_cartesian_explosion,
    check_dangerous_patterns,
    clamp_cartesian_limits,
    get_allowed_glossary_concepts,
    get_dangerous_functions_for_engine,
    has_dangerous_qualified_call,
    is_allowed_column_concept,
    is_destructive_sql,
    parse_sql,
    validate_sql_safety,
    validate_tables_and_columns,
)

__all__ = [
    "ALLOWED_METRIC_WORDS",
    "COLUMN_GLOSSARY_PATH",
    "DANGEROUS_FUNCTIONS",
    "DESTRUCTIVE_TYPES",
    "GLOSSARY_PATH",
    "MSSQL_DANGEROUS_FUNCTIONS",
    "MYSQL_DANGEROUS_FUNCTIONS",
    "ORACLE_DANGEROUS_PACKAGE_PREFIXES",
    "PER_ENGINE_DANGEROUS_FUNCTIONS",
    "POSTGRESQL_DANGEROUS_FUNCTIONS",
    "REPO_ROOT",
    "SQLITE_DANGEROUS_FUNCTIONS",
    "_ORACLE_DANGEROUS_PREFIX_RE",
    "_clean_ident",
    "_normalize_schema_context",
    "check_cartesian_explosion",
    "check_dangerous_patterns",
    "clamp_cartesian_limits",
    "get_allowed_glossary_concepts",
    "get_dangerous_functions_for_engine",
    "has_dangerous_qualified_call",
    "is_allowed_column_concept",
    "is_destructive_sql",
    "parse_sql",
    "validate_sql_safety",
    "validate_tables_and_columns",
]
