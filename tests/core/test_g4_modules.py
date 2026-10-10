"""Unit tests for Step G4 module moves and backward-compatibility shims.

Verifies:
1. sql_safety: src/utils/sql_safety.py -> src/sql/safety/sql_safety.py
2. query_classifier: src/utils/query_classifier.py -> src/sql/query_classifier.py
3. ingestion_registry: src/core/ingestion_registry.py -> src/rag/ingestion_registry.py
"""

from __future__ import annotations

import pytest

# 1. SQL Safety
import src.sql.safety.sql_safety as new_safety
import src.utils.sql_safety as shim_safety

# 2. Query Classifier
import src.sql.query_classifier as new_classifier
import src.utils.query_classifier as shim_classifier

# 3. Ingestion Registry
import src.rag.ingestion_registry as new_registry
import src.core.ingestion_registry as shim_registry


def test_sql_safety_shim_object_identity() -> None:
    """All public and private symbols in src.utils.sql_safety must match src.sql.safety.sql_safety."""
    assert shim_safety.validate_sql_safety is new_safety.validate_sql_safety
    assert shim_safety.is_destructive_sql is new_safety.is_destructive_sql
    assert shim_safety.check_cartesian_explosion is new_safety.check_cartesian_explosion
    assert shim_safety.check_dangerous_patterns is new_safety.check_dangerous_patterns
    assert shim_safety.clamp_cartesian_limits is new_safety.clamp_cartesian_limits
    assert shim_safety.parse_sql is new_safety.parse_sql
    assert shim_safety.validate_tables_and_columns is new_safety.validate_tables_and_columns
    assert shim_safety.has_dangerous_qualified_call is new_safety.has_dangerous_qualified_call
    assert shim_safety.DANGEROUS_FUNCTIONS is new_safety.DANGEROUS_FUNCTIONS
    assert shim_safety.DESTRUCTIVE_TYPES is new_safety.DESTRUCTIVE_TYPES
    assert shim_safety.ALLOWED_METRIC_WORDS is new_safety.ALLOWED_METRIC_WORDS
    assert shim_safety.SQLITE_DANGEROUS_FUNCTIONS is new_safety.SQLITE_DANGEROUS_FUNCTIONS
    assert shim_safety.MYSQL_DANGEROUS_FUNCTIONS is new_safety.MYSQL_DANGEROUS_FUNCTIONS
    assert shim_safety.POSTGRESQL_DANGEROUS_FUNCTIONS is new_safety.POSTGRESQL_DANGEROUS_FUNCTIONS
    assert shim_safety.MSSQL_DANGEROUS_FUNCTIONS is new_safety.MSSQL_DANGEROUS_FUNCTIONS
    assert shim_safety.PER_ENGINE_DANGEROUS_FUNCTIONS is new_safety.PER_ENGINE_DANGEROUS_FUNCTIONS


def test_sql_safety_per_engine_dangerous_lists() -> None:
    """Verify per-engine dangerous functions and union identity."""
    # Union must be completely identical to DANGEROUS_FUNCTIONS
    combined = (
        new_safety.SQLITE_DANGEROUS_FUNCTIONS
        | new_safety.MYSQL_DANGEROUS_FUNCTIONS
        | new_safety.POSTGRESQL_DANGEROUS_FUNCTIONS
        | new_safety.MSSQL_DANGEROUS_FUNCTIONS
    )
    assert combined == new_safety.DANGEROUS_FUNCTIONS
    assert len(new_safety.DANGEROUS_FUNCTIONS) == 44

    # Check engine-specific subsets
    assert "load_file" in new_safety.SQLITE_DANGEROUS_FUNCTIONS
    assert "sleep" in new_safety.MYSQL_DANGEROUS_FUNCTIONS
    assert "pg_sleep" in new_safety.POSTGRESQL_DANGEROUS_FUNCTIONS
    assert "openrowset" in new_safety.MSSQL_DANGEROUS_FUNCTIONS

    # Test helper
    assert new_safety.get_dangerous_functions_for_engine("sqlite") == new_safety.SQLITE_DANGEROUS_FUNCTIONS
    assert new_safety.get_dangerous_functions_for_engine("mysql") == new_safety.MYSQL_DANGEROUS_FUNCTIONS
    assert new_safety.get_dangerous_functions_for_engine("postgresql") == new_safety.POSTGRESQL_DANGEROUS_FUNCTIONS
    assert new_safety.get_dangerous_functions_for_engine("mssql") == new_safety.MSSQL_DANGEROUS_FUNCTIONS
    assert new_safety.get_dangerous_functions_for_engine(None) == new_safety.DANGEROUS_FUNCTIONS


def test_sql_safety_functional() -> None:
    """Smoke test validation and destructive detection."""
    safe_sql = "SELECT id, name FROM customer WHERE id = 1"
    is_safe, err = new_safety.validate_sql_safety(safe_sql)
    assert is_safe
    assert err == ""

    # Destructive operations
    assert new_safety.is_destructive_sql("DROP TABLE customer")
    assert new_safety.is_destructive_sql("DELETE FROM customer WHERE id = 1")
    assert new_safety.is_destructive_sql("SELECT sleep(5)")
    assert new_safety.is_destructive_sql("SELECT pg_sleep(5)", dialect="postgres")
    assert new_safety.is_destructive_sql("SELECT * FROM OPENROWSET('provider', 'conn', 'table')", dialect="tsql")


def test_query_classifier_shim_object_identity() -> None:
    """All symbols in src.utils.query_classifier must match src.sql.query_classifier."""
    assert shim_classifier.QueryType is new_classifier.QueryType
    assert shim_classifier.TTL_BY_QUERY_TYPE is new_classifier.TTL_BY_QUERY_TYPE
    assert shim_classifier.classify_query is new_classifier.classify_query
    assert shim_classifier.classify_query_intent is new_classifier.classify_query_intent
    assert shim_classifier.LIST_QUERY is new_classifier.LIST_QUERY
    assert shim_classifier.AGGREGATE_QUERY is new_classifier.AGGREGATE_QUERY
    assert shim_classifier.EXPLANATION_QUERY is new_classifier.EXPLANATION_QUERY


def test_query_classifier_functional() -> None:
    """Smoke test deterministic query classification."""
    assert new_classifier.classify_query("How many orders were placed yesterday?") == new_classifier.QueryType.COUNT
    assert new_classifier.classify_query("Total sales for March 2026") == new_classifier.QueryType.SUM
    assert new_classifier.classify_query("Show all active customers") == new_classifier.QueryType.LIST
    assert new_classifier.classify_query("What is the company return policy?") == new_classifier.QueryType.POLICY
    assert new_classifier.classify_query("Random text query") == new_classifier.QueryType.OTHER


def test_ingestion_registry_shim_object_identity() -> None:
    """All symbols in src.core.ingestion_registry must match src.rag.ingestion_registry."""
    assert shim_registry.IngestionRegistry is new_registry.IngestionRegistry
    assert shim_registry.RegistryStatus is new_registry.RegistryStatus
    assert shim_registry.RegistryCheckResult is new_registry.RegistryCheckResult
    assert shim_registry._BUFFER_SIZE == new_registry._BUFFER_SIZE


def test_ingestion_registry_functional() -> None:
    """Smoke test IngestionRegistry methods and status values."""
    assert new_registry.RegistryStatus.NEW_FILE == "new_file"
    assert new_registry.RegistryStatus.ALREADY_INGESTED == "already_ingested"
    assert new_registry.RegistryStatus.STALE_EMBEDDING == "stale_embedding"

    check_res = new_registry.RegistryCheckResult(
        status=new_registry.RegistryStatus.NEW_FILE,
        sha256="abc123hash",
    )
    assert check_res.content_hash == "abc123hash"
    assert check_res.status == new_registry.RegistryStatus.NEW_FILE

    reg = new_registry.IngestionRegistry()
    assert hasattr(reg, "get_all")
    assert hasattr(reg, "get_active")
    assert hasattr(reg, "get_by_document_id")
