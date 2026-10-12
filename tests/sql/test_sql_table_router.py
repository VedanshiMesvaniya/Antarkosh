"""Tests for src.sql.table_router and re-exports in src.stages.s12b_sql_retrieval."""

from pathlib import Path

import pytest

from src.sql.table_router import (
    _ROUTING_HINTS_CACHE as DIRECT_CACHE,
    _clear_routing_hints_cache as direct_clear_cache,
    load_routing_hints as direct_load_hints,
    route_anchor_tables as direct_route_anchors,
    route_tables_for_query as direct_route_query,
)


def test_no_hardcoded_table_names_in_router_code():
    """Verify that src/sql/table_router.py contains zero hardcoded database table names."""
    router_file = Path("src/sql/table_router.py")
    assert router_file.exists()
    content = router_file.read_text(encoding="utf-8")

    forbidden = [
        "sales_order",
        "party",
        "product",
        "warehouse",
        "purchase",
        "stock",
        "machine",
        "delivery_challan",
        "proforma",
        "lead",
        "category",
        "unit",
        "financial_year",
    ]
    for table_name in forbidden:
        assert table_name not in content, f"Hardcoded table '{table_name}' found in table_router.py"


@pytest.mark.parametrize(
    ("query", "expected_anchors", "expected_domain"),
    [
        (
            "What is the sales order due date for HM TOOLS?",
            [
                "delivery_challan",
                "delivery_challan_products",
                "financial_year",
                "party",
                "product",
                "sales_order",
                "sales_order_products",
            ],
            ["sales_order", "sales_order_products", "party", "product", "financial_year"],
        ),
        (
            "Purchase invoice GT/0091 party",
            [
                "financial_year",
                "party",
                "product",
                "proforma",
                "purchase",
                "purchase_products",
                "sales_order",
                "stock",
            ],
            ["purchase", "purchase_products", "party", "product", "financial_year", "proforma", "quotation", "sales_order"],
        ),
        (
            "How many products are in warehouse 1?",
            [
                "category",
                "packagings",
                "party",
                "product",
                "product_color",
                "product_type",
                "sales_order",
                "stock",
                "warehouse",
            ],
            ["stock", "product", "product_color", "category"],
        ),
        (
            "Stock-out adjustments on 2025-06-03",
            [
                "category",
                "packagings",
                "party",
                "product",
                "product_color",
                "product_type",
                "sales_order",
                "stock",
                "stock_adjustment",
                "unit",
                "warehouse",
            ],
            ["stock", "product", "product_color", "category"],
        ),
        (
            "Machine for CAP03 production",
            [
                "actual_production",
                "category",
                "financial_year",
                "machine",
                "product",
                "product_color",
                "product_type",
                "production",
            ],
            ["production", "actual_production", "machine", "product", "product_color"],
        ),
        (
            "Show active categories count",
            ["category", "product", "product_type"],
            ["category", "product", "product_type"],
        ),
        (
            "Lead followup history with party ABC",
            ["lead", "lead_history", "party", "users"],
            ["lead", "lead_history", "users", "party"],
        ),
        (
            "Carton 25053534 location and warehouse bin",
            [
                "category",
                "packagings",
                "party",
                "product",
                "product_color",
                "product_type",
                "sales_order",
                "stock",
                "warehouse",
            ],
            ["stock", "product", "product_color", "category"],
        ),
        (
            "Color of finished goods items",
            ["category", "product", "product_color", "product_type", "production", "stock"],
            [],
        ),
        (
            "Customer opening balance and credit ledger",
            [
                "financial_year",
                "party",
                "party_opening_balance",
                "product",
                "receipt",
                "sales_order",
                "sales_order_products",
            ],
            [
                "sales_order",
                "sales_order_products",
                "party",
                "product",
                "financial_year",
                "party_opening_balance",
                "receipt",
            ],
        ),
    ],
)
def test_table_routing_golden_parity(query: str, expected_anchors: list[str], expected_domain: list[str]):
    anchors_direct = sorted(direct_route_anchors(query))
    assert anchors_direct == expected_anchors

    domain_direct = direct_route_query(query, section="fallback_rules")
    assert domain_direct == expected_domain


def test_cache_and_clear_routing_hints():
    direct_clear_cache()
    hints1 = direct_load_hints("erp_main")
    assert "fallback_rules" in hints1
    assert "anchor_rules" in hints1

    # Cached
    assert direct_load_hints("erp_main") is hints1

    # Clear
    direct_load_hints.cache_clear("erp_main")
    hints2 = direct_load_hints("erp_main")
    assert hints2 == hints1
