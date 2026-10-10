"""Tests for src.sql.soft_delete and re-exports in src.stages.s12b_sql_retrieval."""

import pytest

from src.sql.soft_delete import (
    _SOFT_DELETE_TABLES_CACHE as DIRECT_CACHE,
)
from src.sql.soft_delete import (
    FALLBACK_SOFT_DELETE_TABLES as DIRECT_FALLBACK,
)
from src.sql.soft_delete import (
    _clear_soft_delete_tables_cache as direct_clear_cache,
)
from src.sql.soft_delete import (
    _get_tables_with_soft_delete as direct_get_tables,
)
from src.sql.soft_delete import (
    detect_soft_delete_intent as direct_detect_intent,
)
from src.sql.soft_delete import (
    enforce_soft_delete_filter as direct_enforce_filter,
)
from src.stages.s12b_sql_retrieval import (
    _SOFT_DELETE_TABLES_CACHE as REEXPORT_CACHE,
)
from src.stages.s12b_sql_retrieval import (
    FALLBACK_SOFT_DELETE_TABLES as REEXPORT_FALLBACK,
)
from src.stages.s12b_sql_retrieval import (
    _clear_soft_delete_tables_cache as reexport_clear_cache,
)
from src.stages.s12b_sql_retrieval import (
    _get_tables_with_soft_delete as reexport_get_tables,
)
from src.stages.s12b_sql_retrieval import (
    detect_soft_delete_intent as reexport_detect_intent,
)
from src.stages.s12b_sql_retrieval import (
    enforce_soft_delete_filter as reexport_enforce_filter,
)


def test_reexports_identity():
    """Verify that s12b re-exports the exact objects from src.sql.soft_delete."""
    assert direct_detect_intent is reexport_detect_intent
    assert direct_get_tables is reexport_get_tables
    assert direct_clear_cache is reexport_clear_cache
    assert direct_enforce_filter is reexport_enforce_filter
    assert DIRECT_CACHE is REEXPORT_CACHE
    assert DIRECT_FALLBACK is REEXPORT_FALLBACK


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
    """Verify golden outputs across both modules."""
    assert direct_detect_intent(query) == expected
    assert reexport_detect_intent(query) == expected


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
    assert reexport_get_tables("erp_main") is tables

    # Cache clear
    reexport_get_tables.cache_clear("erp_main")
    tables2 = reexport_get_tables("erp_main")
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
    res_reexport = reexport_enforce_filter(sql_in, intent)
    assert res_direct == res_reexport
    assert expected_fragment in res_direct
