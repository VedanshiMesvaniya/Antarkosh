"""Tests for src.sql.soft_delete and re-exports in src.stages.s12b_sql_retrieval."""

import pytest

from src.sql.soft_delete import (
    FALLBACK_SOFT_DELETE_TABLES as DIRECT_FALLBACK,
    _SOFT_DELETE_TABLES_CACHE as DIRECT_CACHE,
    _clear_soft_delete_tables_cache as direct_clear_cache,
    _get_tables_with_soft_delete as direct_get_tables,
    detect_soft_delete_intent as direct_detect_intent,
    enforce_soft_delete_filter as direct_enforce_filter,
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Show me deleted orders", "DELETED_ONLY"),
        ("List active products", "ACTIVE_ONLY"),
        ("Show deleted and active customers", "INCLUDE_ARCHIVED"),
        ("Show sales history", "INCLUDE_ARCHIVED"),
        ("Audit log of changes", "INCLUDE_ARCHIVED"),
        ("Orders from the past 30 days", "ACTIVE_ONLY"),
        ("Past 6 months revenue", "ACTIVE_ONLY"),
        ("Find all quotations", "ACTIVE_ONLY"),
        ("List removed inventory", "DELETED_ONLY"),
        ("Historical purchases", "INCLUDE_ARCHIVED"),
    ],
)
def test_detect_soft_delete_intent_golden(query: str, expected: str):
    """Verify golden outputs across queries."""
    assert direct_detect_intent(query) == expected


def test_get_tables_with_soft_delete_golden():
    """Verify fallback tables match golden snapshot."""
    direct_clear_cache()
    tables = direct_get_tables("erp_main")
    assert len(tables) == 40
    assert tables == DIRECT_FALLBACK
    assert "sales_order" in tables
    assert "party" in tables
    assert "product" in tables
    assert "warehouse" in tables

    # Test cache behavior
    assert direct_get_tables("erp_main") is tables

    # Cache clear
    direct_get_tables.cache_clear("erp_main")
    tables2 = direct_get_tables("erp_main")
    assert tables2 == tables


@pytest.mark.parametrize(
    ("sql_in", "intent", "expected_fragment"),
    [
        (
            "SELECT * FROM sales_order",
            "ACTIVE_ONLY",
            "deleted_at IS NULL",
        ),
        (
            "SELECT * FROM sales_order WHERE id = 1",
            "ACTIVE_ONLY",
            "WHERE id = 1 AND deleted_at IS NULL",
        ),
        (
            "SELECT * FROM sales_order",
            "DELETED_ONLY",
            "deleted_at IS NOT NULL",
        ),
        (
            "SELECT * FROM sales_order WHERE deleted_at IS NULL",
            "DELETED_ONLY",
            "deleted_at IS NOT NULL",
        ),
        (
            "SELECT * FROM sales_order",
            "INCLUDE_ARCHIVED",
            "SELECT * FROM sales_order",
        ),
        (
            "SELECT so.id FROM sales_order so JOIN party p ON so.party_id = p.id",
            "ACTIVE_ONLY",
            "so.deleted_at IS NULL AND p.deleted_at IS NULL",
        ),
    ],
)
def test_enforce_soft_delete_filter_golden(sql_in: str, intent: str, expected_fragment: str):
    res_direct = direct_enforce_filter(sql_in, intent)
    assert expected_fragment in res_direct
