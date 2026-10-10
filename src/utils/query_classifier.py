"""Backward-compatibility shim for src.utils.query_classifier.

Re-exports all symbols from src.sql.query_classifier.
"""

from __future__ import annotations

from src.sql.query_classifier import (
    AGGREGATE_PATTERNS,
    AGGREGATE_QUERY,
    EXPLANATION_PATTERNS,
    EXPLANATION_QUERY,
    LIST_PATTERNS,
    LIST_QUERY,
    TTL_BY_QUERY_TYPE,
    QueryType,
    classify_query,
    classify_query_intent,
)

__all__ = [
    "AGGREGATE_PATTERNS",
    "AGGREGATE_QUERY",
    "EXPLANATION_PATTERNS",
    "EXPLANATION_QUERY",
    "LIST_PATTERNS",
    "LIST_QUERY",
    "TTL_BY_QUERY_TYPE",
    "QueryType",
    "classify_query",
    "classify_query_intent",
]
