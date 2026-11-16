"""Abbreviations that live only in key columns (qn_number) get a whole-word anchor to the table that owns the key."""

from __future__ import annotations

from typing import Any

import pytest

from src.sql import table_router
from src.sql.relationships.model import graph_from_documents
from src.sql.semantics import routing
from src.sql.semantics.routing import build_hints
from tests.sql.relationships_fixtures import documents, table

INT, TXT = "INTEGER", "VARCHAR(20)"


def hints(*tables: dict[str, Any]):
    schema_doc, inventory, _ = documents(*tables)
    g = graph_from_documents(schema_doc, inventory)
    return build_hints(g, set(g.tables))


def qn_schema():
    return (
        table("sqts_main_lear", {"qn_number": TXT, "prinbr": TXT, "plant": TXT}, pk=("qn_number",)),
        table("sqts_history", {"id": INT, "qn_number": TXT, "event_type": INT}, pk=("id",)),
        table("sqts_cost_recovery", {"id": INT, "qn_number": TXT, "price": INT}, pk=("id",)),
        table("gen_supplier_codes", {"prinbr": TXT, "supplier_code": TXT}, pk=("prinbr",)),
    )


def key_rules(h):
    return [r for r in h.anchor_rules if r.get("whole_word")]


def test_the_key_owner_is_anchored_for_the_abbreviation_and_its_plural():
    h = hints(*qn_schema())
    assert [(r["keywords"], r["tables"]) for r in key_rules(h)] == [(["qn", "qns"], ["sqts_main_lear"])]
    assert h.stats["key_token_rules"] == 1 and all(r["auto"] == routing.GENERATED_BY for r in key_rules(h))
    assert any(r.get("whole_word") for r in h.fallback_rules)


def test_no_rule_when_the_token_is_in_a_table_name_the_owner_is_ambiguous_or_nothing_references_it():
    # token already part of a table name: retrieval finds it by name
    h = hints(table("qn_header", {"qn_number": TXT}, pk=("qn_number",)), table("qn_lines", {"id": INT, "qn_number": TXT}, pk=("id",)))
    assert key_rules(h) == []
    # two tables both own a key that starts with the token
    h = hints(table("a_main", {"qn_number": TXT}, pk=("qn_number",)), table("b_main", {"qn_id": INT}, pk=("qn_id",)), table("c", {"id": INT, "qn_number": TXT}, pk=("id",)))
    assert key_rules(h) == []
    # a key nothing else carries is not a shared entity
    assert key_rules(hints(table("a_main", {"qn_number": TXT}, pk=("qn_number",)), table("z", {"id": INT}, pk=("id",)))) == []


@pytest.mark.parametrize("generic", ["id", "code", "number", "status", "type", "user"])
def test_generic_stems_never_become_entity_tokens(generic):
    h = hints(table("owner_t", {f"{generic}_no": TXT}, pk=(f"{generic}_no",)), table("other_t", {"id": INT, f"{generic}_no": TXT}, pk=("id",)))
    assert key_rules(h) == []


def test_words_the_synonym_rules_cover_are_not_duplicated():
    h = hints(table("masters", {"vendor_id": INT}, pk=("vendor_id",)), table("orders", {"id": INT, "vendor_id": INT}, pk=("id",)))
    assert key_rules(h) == []


# ---------------------------------------------------------------- the router


def route(monkeypatch: pytest.MonkeyPatch, rules: list[dict[str, Any]], query: str, section: str = "anchor_rules") -> list[str]:
    monkeypatch.setattr(table_router, "load_routing_hints", lambda db_id="x": {section: rules})
    return table_router.route_tables_for_query(query, db_id="x", section=section)


def test_a_whole_word_rule_fires_only_on_a_separate_word(monkeypatch: pytest.MonkeyPatch):
    rule = [{"keywords": ["qn", "qns"], "tables": ["sqts_main_lear"], "whole_word": True}]
    for q in ("QN history events", "how many qns per plant", "show qn, please", "qn", "(qn) list", "the-qn-list"):
        assert route(monkeypatch, rule, q) == ["sqts_main_lear"], q
    for q in ("check the qnty", "equnit", "aqn", "qn2", "report"):
        assert route(monkeypatch, rule, q) == [], q


def test_a_plain_rule_still_matches_anywhere_exactly_as_before(monkeypatch: pytest.MonkeyPatch):
    assert route(monkeypatch, [{"keywords": ["po"], "tables": ["purchase"]}], "the report") == ["purchase"]
    assert route(monkeypatch, [{"keywords": ["po"], "tables": ["purchase"], "whole_word": False}], "the report") == ["purchase"]
    assert route(monkeypatch, [{"keywords": ["po"], "tables": ["purchase"], "whole_word": "yes"}], "the report") == ["purchase"]  # only a real true counts


def test_a_whole_word_rule_with_a_conditional_still_adds_the_conditional_tables(monkeypatch: pytest.MonkeyPatch):
    rule = [{"keywords": ["qn"], "tables": ["a"], "whole_word": True, "conditional": {"keywords": ["history"], "tables": ["b"]}}]
    assert route(monkeypatch, rule, "qn history") == ["a", "b"]
    assert route(monkeypatch, rule, "qn list") == ["a"]
    assert route(monkeypatch, rule, "history only") == []
