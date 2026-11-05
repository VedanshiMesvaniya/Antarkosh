"""The generic SQL prompt must not carry the ERP database's own rulebook into other databases (apply mode)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.sql import prompt_builder as pb
from src.sql import schema_retrieval as sr

ERP_MARKERS = ("financial_year", "pi_no", "production.qty", "Status flags", "CHANGABLE", "party.party_name", "stock_adjustment", "Delivery Challan", "packagings")
QUERIES = [
    "list the latest order number",
    "what is the po number of the last order",
    "summary report of everything by location",
    "total sales this year by customer",
    "which unit and which type of category",
    "show the colour of the product and the invoice",
    "how many machines produce batch apq",
]


def mode(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr(sr, "_route_bridge_mode", lambda db_id: value)


def dialect() -> MagicMock:
    d = MagicMock()
    d.name = "PostgreSQL"
    d.date_functions = "CURRENT_DATE"
    return d


@pytest.mark.parametrize("query", QUERIES)
def test_a_non_erp_database_in_apply_mode_gets_no_erp_rulebook(monkeypatch: pytest.MonkeyPatch, query: str):
    mode(monkeypatch, "apply")
    text = pb.get_scoped_readability_rules(query, ["orders", "customers"], db_id="shop")
    assert not [m for m in ERP_MARKERS if m in text], text
    # the generic protocol is still there
    assert "Core SQL Generation & Schema Mapping Protocol" in text and "Primary Key & Column Projection" in text
    assert "Mandatory Soft-Delete Rule" in text and "Deduplication (DISTINCT)" in text and "Fuzzy LIKE Filtering" in text
    assert "machine" not in text.split("Deduplication (DISTINCT)")[1].split("\n")[0]  # the ERP examples are gone from the generic rules


@pytest.mark.parametrize("value", ["apply", "shadow", None])
@pytest.mark.parametrize("query", QUERIES)
def test_the_erp_database_always_keeps_every_rule(monkeypatch: pytest.MonkeyPatch, query: str, value: str | None):
    mode(monkeypatch, value)
    text = pb.get_scoped_readability_rules(query, ["orders"], db_id="erp_main")
    assert "Status flags: party.status" in text and "Temporal Scope & Financial Year Filtering" in text and "CHANGABLE PACK" in text
    assert text == pb.get_scoped_readability_rules(query, ["orders"])  # and db_id changes nothing for it


@pytest.mark.parametrize("value", ["shadow", None])
def test_a_non_erp_database_outside_apply_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch, value: str | None):
    mode(monkeypatch, value)
    for q in QUERIES:
        assert pb.get_scoped_readability_rules(q, ["orders"], db_id="shop") == pb.get_scoped_readability_rules(q, ["orders"])


def test_a_rule_whose_tables_are_present_is_kept_for_any_database(monkeypatch: pytest.MonkeyPatch):
    mode(monkeypatch, "apply")
    text = pb.get_scoped_readability_rules("when was it delivered", ["delivery_challan", "financial_year", "party", "stock"], db_id="shop")
    assert "Delivery Challan (DC) vs Invoice" in text and "Temporal Scope & Financial Year Filtering" in text and "Status flags" in text


def test_the_gate_failing_keeps_the_text_exactly_as_it_always_was(monkeypatch: pytest.MonkeyPatch):
    def boom(db_id: str) -> str:
        raise RuntimeError("flags unreadable")

    monkeypatch.setattr(sr, "_route_bridge_mode", boom)
    assert pb._trim_erp_rules("shop") is False
    assert pb.get_scoped_readability_rules("latest order number", ["orders"], db_id="shop") == pb.get_scoped_readability_rules("latest order number", ["orders"])


def test_business_intent_does_not_send_a_non_erp_database_to_a_financial_year_table(monkeypatch: pytest.MonkeyPatch):
    schema = "CREATE TABLE orders (id INT, created DATE);"
    mode(monkeypatch, "apply")
    now = pb.build_sql_prompt("total sales this year", schema, dialect(), db_id="shop")
    assert "CURRENT PERIOD" in now and "financial_year" not in now.split("Schema:")[0].split("Extracted Business Intent")[1]
    cumulative = pb.build_sql_prompt("total sales of all time", schema, dialect(), db_id="shop")
    assert "ALL-TIME / CUMULATIVE (DO NOT restrict by date" in cumulative
    with_fy = pb.build_sql_prompt("total sales this year", schema + "\n\nCREATE TABLE financial_year (id INT, current_year CHAR(1));", dialect(), db_id="shop")
    assert "CURRENT FINANCIAL YEAR" in with_fy
    erp = pb.build_sql_prompt("total sales this year", schema, dialect(), db_id="erp_main")
    assert "CURRENT FINANCIAL YEAR" in erp
    mode(monkeypatch, None)
    assert "CURRENT FINANCIAL YEAR" in pb.build_sql_prompt("total sales this year", schema, dialect(), db_id="shop")  # not in apply mode: as before
