"""Tests for src.sql.intent and re-exports in src.stages.s12b_sql_retrieval."""

import pytest

from src.sql.intent import extract_analytical_intent as direct_extract_intent
from src.stages.s12b_sql_retrieval import extract_analytical_intent as reexport_extract_intent


def test_reexport_identity():
    """Verify that s12b re-exports the exact object from src.sql.intent."""
    assert direct_extract_intent is reexport_extract_intent


@pytest.mark.parametrize(
    ("query", "expected_metrics", "expected_dims", "expected_scope", "expected_soft"),
    [
        (
            "What is the sales order due date for HM TOOLS?",
            ["sales value / revenue"],
            ["customer / party"],
            "ALL_TIME",
            "ACTIVE_ONLY",
        ),
        (
            "Top 5 customers by sales revenue last month",
            ["sales value / revenue"],
            ["customer / party", "month"],
            "SPECIFIC_PERIOD",
            "ACTIVE_ONLY",
        ),
        (
            "How many products are in warehouse 1?",
            [],
            ["product"],
            "ALL_TIME",
            "ACTIVE_ONLY",
        ),
        (
            "Total purchase expenditure from vendor ABC in the past 6 months",
            ["purchase expenditure"],
            ["supplier", "month"],
            "ALL_TIME",
            "ACTIVE_ONLY",
        ),
        (
            "Average quantity produced this financial year",
            ["quantity / units", "production quantity"],
            [],
            "CURRENT_YEAR",
            "ACTIVE_ONLY",
        ),
        (
            "Show me deleted invoices for party X",
            [],
            ["customer / party"],
            "ALL_TIME",
            "DELETED_ONLY",
        ),
        (
            "List inactive buyers who haven't ordered recently",
            [],
            ["customer / party"],
            "ALL_TIME",
            "ACTIVE_ONLY",
        ),
        (
            "Lowest unit price item among active inventory",
            ["stock on hand"],
            ["product"],
            "ALL_TIME",
            "ACTIVE_ONLY",
        ),
    ],
)
def test_extract_analytical_intent_golden(
    query: str,
    expected_metrics: list[str],
    expected_dims: list[str],
    expected_scope: str,
    expected_soft: str,
):
    direct_res = direct_extract_intent(query)
    reexport_res = reexport_extract_intent(query)
    assert direct_res == reexport_res

    assert direct_res["metrics"] == expected_metrics
    assert direct_res["dimensions"] == expected_dims
    assert direct_res["temporal_scope"] == expected_scope
    assert direct_res["soft_delete_intent"] == expected_soft


def test_extract_analytical_intent_details():
    res = direct_extract_intent("Top 5 customers by sales revenue last month")
    assert res["limit"] == 5
    assert res["sorting"] == "DESC"
    assert res["time_period"] == "last month"

    res_avg = direct_extract_intent("Average quantity produced this financial year")
    assert res_avg["aggregation"] == "AVG"
    assert res_avg["time_period"] == "this financial year"
