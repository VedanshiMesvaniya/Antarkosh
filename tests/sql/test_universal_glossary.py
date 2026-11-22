"""The universal ERP glossary and the local concept mapping (``universal.py``, ``concepts.py``, ``review.py`` and their wiring).

The data file is checked against the rules of the master prompt it was written from: coverage of every listed concept, the listed
separations, and the things a universal glossary must never contain. Nothing here names a real customer database.
"""

# ruff: noqa: F811

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

import src.sql.prompt_builder as pb
from src.sql.semantics import concepts as cc
from src.sql.semantics import review as rv
from src.sql.semantics import universal as u
from src.sql.semantics import wiring
from src.sql.semantics.evidence import ColumnEvidence, Item, TableEvidence
from tests.sql.relationships_fixtures import build, table
from tests.sql.test_auto_procedures import _sync_args, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_column_meaning import (  # noqa: F401  (shared harness)
    FakeLLM,
    dbs,
    put,
    read_dict,
    run,
)

ROOT = Path(__file__).resolve().parents[2]
GLOSSARY_FILE = ROOT / u.UNIVERSAL_REL
INT, TXT, NUM = "INTEGER", "VARCHAR(50)", "DECIMAL(12,2)"


@pytest.fixture(scope="module")
def doc() -> dict[str, Any]:
    return json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def glossary(doc: dict[str, Any]) -> u.UniversalGlossary:
    return u.build(doc)


# ----------------------------------------------------------------------------------------------- the shipped data


def test_the_shipped_glossary_is_valid_and_is_the_one_that_loads() -> None:
    u.clear_universal_cache()
    assert u.validate_document(json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))) == []
    loaded = u.load_universal()
    assert loaded is not None and len(loaded) > 250


# Every concept the master prompt lists, by section. Each must be a label or an alias (strong or conditional) of some concept.
COVERAGE: dict[str, list[str]] = {
    "A": ["customer", "client", "consumer", "account", "supplier", "vendor", "seller", "manufacturer", "distributor", "reseller", "partner",
          "contact", "employee", "worker", "manager", "user", "role", "team", "department", "division", "business unit", "branch",
          "legal entity", "company", "organisation", "site", "location"],
    "B": ["lead", "prospect", "opportunity", "quotation", "estimate", "proposal", "sales order", "order", "order line", "contract", "agreement",
          "subscription", "renewal", "price list", "pricing", "discount", "promotion", "coupon", "rebate", "sales representative", "campaign",
          "customer segment", "customer group", "account manager", "support case", "ticket", "complaint", "feedback", "SLA", "escalation"],
    "C": ["purchase requisition", "purchase order", "purchase order line", "supplier quotation", "request for quotation", "goods receipt",
          "supplier invoice", "vendor bill", "procurement", "sourcing", "approval", "approval workflow", "supplier performance",
          "purchase return", "purchase contract", "landed cost"],
    "D": ["product", "item", "goods", "article", "service item", "material", "raw material", "component", "finished good", "semi-finished good",
          "product variant", "product category", "brand", "unit of measure", "barcode", "product identifier", "SKU", "batch", "lot",
          "serial number", "inventory", "stock", "on-hand quantity", "available quantity", "reserved quantity", "allocated quantity",
          "reorder level", "safety stock", "stock movement", "stock adjustment", "stock transfer", "warehouse", "bin", "rack",
          "storage location", "cycle count", "physical inventory", "inventory valuation"],
    "E": ["invoice", "customer invoice", "supplier invoice", "payment", "receipt", "remittance", "credit note", "debit note", "refund", "return",
          "tax", "GST", "VAT", "withholding tax", "currency", "exchange rate", "general ledger", "journal entry", "chart of accounts",
          "account payable", "account receivable", "outstanding balance", "due amount", "payment terms", "fiscal year", "accounting period",
          "budget", "cost center", "profit center", "revenue", "sales amount", "expense", "cost", "gross profit", "net profit", "margin",
          "asset", "fixed asset", "depreciation", "amortization", "cash flow"],
    "F": ["bill of materials", "BOM component", "recipe", "formulation", "production order", "work order", "routing", "operation",
          "work center", "machine", "production line", "shift", "capacity", "material issue", "material consumption", "production output",
          "yield", "scrap", "wastage", "rejection", "rework", "quality inspection", "quality check", "nonconformance", "defect",
          "quality hold", "maintenance", "downtime"],
    "G": ["shipment", "dispatch", "delivery", "delivery order", "packing list", "carrier", "courier", "transporter", "freight", "consignment",
          "tracking number", "tracking event", "delivery route", "trip", "vehicle", "driver", "loading", "unloading", "customs", "import",
          "export", "warehouse transfer", "proof of delivery"],
    "H": ["employee", "applicant", "candidate", "recruitment", "onboarding", "attendance", "time entry", "timesheet", "clock-in", "clock-out",
          "leave", "absence", "holiday", "shift", "roster", "payroll", "salary", "wage", "compensation", "benefit", "reimbursement",
          "performance review", "appraisal", "goal", "training", "designation", "job title"],
    "I": ["project", "programme", "job", "engagement", "task", "milestone", "activity", "resource", "assignment", "allocation", "capacity",
          "timesheet", "billable hours", "non-billable hours", "issue", "service request", "incident", "change request", "knowledge base",
          "document", "attachment"],
    "J": ["user", "role", "permission", "access level", "approval", "audit trail", "activity log", "notification", "alert", "email", "workflow",
          "configuration", "tenant", "company", "branch", "legal entity", "data retention", "archived record", "deleted record"],
    "K": ["count", "total", "sum", "average", "minimum", "maximum", "top", "bottom", "highest", "lowest", "latest", "oldest", "comparison",
          "variance", "growth", "trend", "percentage", "ratio", "ranking", "grouped by", "monthly", "quarterly", "yearly", "current period",
          "previous period", "upcoming", "overdue"],
}


def test_every_concept_the_master_prompt_lists_is_covered(glossary: u.UniversalGlossary) -> None:
    missing = [f"{section}:{term}" for section, terms in COVERAGE.items() for term in terms if not glossary.recognises(u.words_of(term))]
    assert missing == []
    assert sum(len(t) for t in COVERAGE.values()) > 250  # the whole list was checked


def test_the_master_prompts_separations_are_all_declared(glossary: u.UniversalGlossary) -> None:
    groups = [
        ["customer", "account", "user", "member", "contact"],
        ["vendor", "supplier", "manufacturer", "partner", "seller"],
        ["sales order", "purchase order", "work order", "production order"],
        ["invoice", "receipt", "payment", "bill", "credit note", "debit note"],
        ["product", "raw material", "component", "finished good", "product category", "brand"],
        ["sku", "barcode", "item code", "product code", "serial number", "lot number"],
        ["warehouse", "location", "site", "branch", "store", "bin"],
        ["stock", "available stock", "reserved stock", "physical stock", "inventory value"],
        ["shipment", "dispatch", "delivery", "freight", "consignment"],
        ["revenue", "sales amount", "income", "gross profit", "net profit", "margin"],
        ["expense", "cost", "cost of goods sold", "spend"],
        ["active", "pending", "open", "in-progress", "completed", "delivered", "cancelled", "deleted"],
        ["audit history", "archived record", "soft-deleted record"],
        ["user session", "audit log", "activity log"],
        ["batch", "lot", "serial number"],
        ["quantity", "amount", "rate", "unit price", "total amount"],
    ]
    for group in groups:
        for a in group:
            for b in group:
                if a != b:
                    ca, cb = glossary.by_label[a], glossary.by_label[b]
                    assert glossary.are_separate(ca.concept_id, cb.concept_id), (a, b)


def test_the_master_prompts_own_example_holds(glossary: u.UniversalGlossary) -> None:
    product = glossary.by_id["inventory.product"]
    assert {"product", "item", "goods", "article"} <= set(product.aliases) and {"material", "sku"} <= set(product.conditional_aliases)
    assert {"raw material", "finished good", "product category"} <= set(product.do_not_equate_with)
    assert product.kind == "entity" and product.domain == "inventory"


def test_the_shipped_glossary_is_a_vocabulary_not_a_schema_map(doc: dict[str, Any]) -> None:
    phrases = [p for c in doc["concepts"] for p in (c["label"], *c["aliases"], *c["conditional_aliases"], *c["do_not_equate_with"])]
    for p in phrases:
        assert p == p.lower() and not any(ch in p for ch in "_.()=*'\";,/\\0123456789"), p  # no identifiers, symbols, SQL or values
        assert len(p) >= 3 and p not in {"y", "n", "p", "v", "create", "button"}, p
        assert not set(p.replace("-", " ").split()) & (u.SQL_WORDS | u.AMBIGUOUS_ABBREVIATIONS), p
    assert all(set(c) == {"concept_id", "label", "domain", "aliases", "conditional_aliases", "do_not_equate_with", "kind"} for c in doc["concepts"])


def test_query_intent_and_state_words_are_kept_apart_from_entities(doc: dict[str, Any], glossary: u.UniversalGlossary) -> None:
    intent = {a for c in doc["concepts"] if c["kind"] == "query_intent" for a in c["aliases"]}
    assert {"count", "top", "monthly", "overdue", "grouped by"} <= intent
    for c in doc["concepts"]:
        if c["kind"] != "query_intent":
            assert not intent & {*c["aliases"], *c["conditional_aliases"]}, c["concept_id"]
        if c["kind"] == "state":
            assert c["aliases"] == [c["label"]] and c["conditional_aliases"] == []
    for question in ("top 5 monthly total", "open cancelled pending"):
        assert glossary.expand(u.words_of(question), lambda _w: True) == []  # shaping words are never expanded


# ----------------------------------------------------------------------------------------------- the validator


def _doc(*concepts: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "concepts": list(concepts)}


def _c(label: str = "widget", *, domain: str = "inventory", aliases: list[str] | None = None, cond: list[str] | None = None,
       dne: list[str] | None = None, kind: str = "entity") -> dict[str, Any]:
    return {"concept_id": f"{domain}.{label.replace(' ', '_').replace('-', '_')}", "label": label, "domain": domain,
            "aliases": aliases if aliases is not None else [label], "conditional_aliases": cond or [], "do_not_equate_with": dne or [], "kind": kind}


def test_a_minimal_valid_document_passes() -> None:
    assert u.validate_document(_doc(_c("widget"), _c("gadget", aliases=["gadget", "gizmo"]))) == []


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        (_c("item_code"), "plain lower-case business phrase"),  # a column name
        (_c("table"), "SQL or schema words"),
        (_c("widget", aliases=["widget", "select"]), "SQL or schema words"),
        (_c("widget", aliases=["widget", "item2"]), "plain lower-case business phrase"),  # digits
        (_c("widget", aliases=["widget", "po"]), "plain lower-case business phrase"),  # too short and ambiguous
        (_c("widget", aliases=["widget", "button"]), "code or interface word"),
        (_c("widget", aliases=["widget", "Y"]), "plain lower-case business phrase"),
        (_c("widget", aliases=["widget", "top"]), "SQL or schema words"),  # SQL-shaped, and not a query-intent concept
        (_c("widget", aliases=["widget", "sku"]), "may only be a conditional alias"),
        (_c("widget", aliases=["gadget"]), "label must be one of its aliases"),
        (_c("widget", kind="mystery"), "unknown kind"),
        (_c("widget", domain="query_intent"), "go together"),
        (_c("open", domain="status", kind="state", aliases=["open", "ajar"]), "no aliases besides its label"),
        ({**_c("widget"), "concept_id": "inventory.other"}, "concept_id must be"),
        ({**_c("widget"), "extra": 1}, "keys must be exactly"),
    ],
)
def test_the_validator_refuses_what_a_universal_glossary_must_never_contain(bad: dict[str, Any], fragment: str) -> None:
    problems = u.validate_document(_doc(bad))
    assert any(fragment in p for p in problems), problems


def test_the_validator_refuses_conflicts_between_concepts() -> None:
    dup = u.validate_document(_doc(_c("widget", aliases=["widget", "thing"]), _c("gadget", aliases=["gadget", "thing"])))
    assert any("also used by" in p for p in dup)
    asym = u.validate_document(_doc(_c("widget", dne=["gadget"]), _c("gadget")))
    assert any("not symmetric" in p for p in asym)
    missing = u.validate_document(_doc(_c("widget", dne=["nothing here"])))
    assert any("not a concept label" in p for p in missing)
    both = u.validate_document(_doc(_c("widget", aliases=["widget"], cond=["gizmo"], dne=["gadget"]), _c("gadget", aliases=["gadget", "gizmo"], dne=["widget"])))
    assert any("must not equal" in p for p in both)  # a word that belongs to both sides of a do-not-equate pair
    repeat = u.validate_document(_doc(_c("widget", aliases=["widget"], cond=["widget"])))
    assert any("repeats a strong alias" in p for p in repeat)
    intent = u.validate_document(_doc(_c("count", domain="query_intent", kind="query_intent"), _c("widget", aliases=["widget", "count"])))
    assert any("query-intent word" in p for p in intent)
    assert u.validate_document({"concepts": []}) and u.validate_document([]) and u.validate_document(None)


def test_every_listed_ambiguous_abbreviation_is_refused() -> None:
    for abbr in sorted(u.AMBIGUOUS_ABBREVIATIONS):
        assert u.validate_document(_doc(_c("widget", aliases=["widget", abbr]))), abbr


# ----------------------------------------------------------------------------------------------- matching and widening


def _w(text: str) -> tuple[str, ...]:
    return u.words_of(text)


def _g(*concepts: dict[str, Any]) -> u.UniversalGlossary:
    d = _doc(*concepts)
    assert u.validate_document(d) == []
    return u.build(d)


def test_the_longest_phrase_wins_and_a_word_is_used_once(glossary: u.UniversalGlossary) -> None:
    assert [m.concept.label for m in glossary.match(_w("raw material cost"))] == ["raw material", "cost"]  # not product / material
    assert [m.concept.label for m in glossary.match(_w("product category sales"))] == ["product category"]
    assert [m.concept.label for m in glossary.match(_w("supplier invoice"))] == ["supplier invoice"]
    assert [m.concept.label for m in glossary.match(_w("Suppliers and Items"))] == ["supplier", "product"]  # plurals, and "item" is a product word
    assert [m.concept.label for m in glossary.match(_w("semi finished goods"))] == ["semi-finished good"]  # hyphen or space


def test_strong_aliases_always_widen_conditional_ones_only_with_local_support(glossary: u.UniversalGlossary) -> None:
    q = _w("show each item")
    assert glossary.expand(q) == ["product", "goods", "article"]
    with_material = glossary.expand(q, lambda ws: tuple(ws) == ("material",))
    assert "material" in with_material and with_material.index("material") == 1  # the database's own word comes right after the label
    assert "sku" not in with_material  # the database does not use it
    assert "material" not in glossary.expand(q, lambda _ws: False)
    # a conditional word spoken by the user counts only when the database uses it too
    assert [m.concept.label for m in glossary.match(_w("sku"), lambda _ws: True)] == ["sku"]  # its own concept, never "product"
    assert [(m.concept.label, m.via) for m in glossary.match(_w("godown"), lambda ws: tuple(ws) == ("godown",))] == [("warehouse", "conditional")]
    assert glossary.match(_w("godown")) == [] and glossary.match(_w("godown"), lambda _ws: False) == []


def test_a_concept_never_widens_into_something_it_must_not_equal(glossary: u.UniversalGlossary) -> None:
    for text in ("raw material", "finished good", "product category", "brand"):
        assert "product" not in glossary.expand(_w(text), lambda _ws: True), text
    assert glossary.expand(_w("supplier"), lambda _ws: True) == []  # vendor and supplier are separate concepts
    assert glossary.expand(_w("customer")) == ["client"]


def test_what_the_question_already_says_is_never_added_again(glossary: u.UniversalGlossary) -> None:
    assert "product" not in glossary.expand(_w("item product"))
    assert glossary.expand(_w("product")) == ["item", "goods", "article"]  # the label is the match itself


def test_widening_is_capped(glossary: u.UniversalGlossary) -> None:
    many = glossary.expand(_w("item customer employee warehouse manufacturer quotation"), lambda _ws: True, limit=3)
    assert len(many) == 3
    assert len(glossary.expand(_w("item customer employee warehouse manufacturer quotation"), lambda _ws: True)) <= u.MAX_EXPANSION


def test_intent_and_state_concepts_match_but_never_widen_and_a_shared_phrase_is_skipped() -> None:
    g = _g(
        _c("alpha", cond=["shared"]), _c("beta", cond=["shared"]), _c("count", domain="query_intent", kind="query_intent", aliases=["count", "tally"]),
    )
    assert [m.concept.label for m in g.match(_w("tally"))] == ["count"] and g.expand(_w("tally")) == []
    assert g.match(_w("shared"), lambda _ws: True) == []  # two concepts claim it: ambiguous, so neither is chosen


def test_a_conditional_word_that_is_a_do_not_equate_word_is_never_added() -> None:
    g = _g(
        _c("alpha", aliases=["alpha", "first"], cond=["third"], dne=["beta"]),
        _c("beta", aliases=["beta", "second"], dne=["alpha"]),
    )
    assert g.expand(_w("first"), lambda _ws: True) == ["alpha", "third"]
    assert "second" not in g.expand(_w("first"), lambda _ws: True)


# ----------------------------------------------------------------------------------------------- loading


def test_a_missing_or_invalid_file_means_no_glossary_and_never_an_error(tmp_path: Path) -> None:
    u.clear_universal_cache()
    assert u.load_universal(tmp_path / "nothing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    assert u.load_universal(bad) is None
    bad.write_text(json.dumps(_doc(_c("widget", aliases=["widget", "select"]))), encoding="utf-8")
    assert u.load_universal(bad) is None  # a glossary that breaks the rules is not used at all
    bad.write_text(json.dumps(_doc(_c("widget"))), encoding="utf-8")
    again = u.load_universal(bad)
    assert again is not None and again is u.load_universal(bad)  # fixed: picked up, then cached
    u.clear_universal_cache()


# ----------------------------------------------------------------------------------------------- the concept layer (pure)


def _card(full_name: str, *, confidence: str = "high", evidence: list[str] | None = None, meaning: str = "", flags: list[str] | None = None) -> dict[str, Any]:
    return {"full_name": full_name, "meaning": meaning or f"The {full_name}.", "phrases": [full_name], "evidence": evidence or [],
            "confidence": confidence, "base_confidence": confidence, "flags": flags or []}


def _doc_of(**tables: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"tables": {t: {"columns": cols} for t, cols in tables.items()}}


def _layer(doc: dict[str, Any], *tables: dict[str, Any], taken: set[str] | None = None, glossary: u.UniversalGlossary) -> cc.ConceptLayer:
    return cc.build_concept_layer(doc, build(*tables), glossary, taken or set())


def test_a_supported_meaning_becomes_a_concept_entry_in_the_masters_shape(glossary: u.UniversalGlossary) -> None:
    graph_tables = (
        table("vendors", {"vendor_id": INT, "vendor_nm": (TXT, "legal name of the supplier")}, pk=("vendor_id",)),
        table("po", {"po_id": INT, "vendor_id": INT}, pk=("po_id",)),
    )
    doc = _doc_of(vendors={"vendor_nm": _card("vendor name", evidence=["comment"]), "vendor_id": _card("vendor identifier")})
    layer = _layer(doc, *graph_tables, glossary=glossary)
    entry = layer.entries["vendor"]
    assert entry["maps_to"] == "vendors.vendor_nm" and entry["concept_id"] == "party.vendor" and entry["confidence"] == "high"
    assert entry["safe_for_prompt"] is True and entry["aliases_seen"] == ["vendor"] and entry["synonyms"] == []
    names = [(m["column"], m["role"]) for m in entry["mappings"]]
    assert names == [("vendor_nm", "entity_name"), ("vendor_id", "identifier")]  # entity name first, then the key
    first = entry["mappings"][0]
    assert set(first) == {"table", "column", "role", "evidence", "confidence", "safe_for_prompt"}
    assert first["evidence"] == ["column name", "column comment"] and entry["mappings"][1]["evidence"] == ["column name", "primary key"]
    assert layer.stats["entries"] == 1 and layer.stats["high"] == 2


def test_a_name_alone_is_never_high_it_goes_to_review(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(vendors={"vendor_nm": _card("vendor name")})
    layer = _layer(doc, table("vendors", {"vendor_id": INT, "vendor_nm": TXT}, pk=("vendor_id",)), glossary=glossary)
    assert layer.entries == {}
    [row] = layer.needs_stronger_evidence
    assert row["concept_id"] == "party.vendor" and row["reason"] == "the name is the only evidence" and row["role"] == "entity_name"


def test_medium_confidence_is_a_candidate_for_review_never_an_entry(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(vendors={"vendor_id": _card("vendor identifier", confidence="medium")})
    layer = _layer(doc, table("vendors", {"vendor_id": INT}, pk=("vendor_id",)), glossary=glossary)
    assert layer.entries == {} and layer.needs_stronger_evidence[0]["reason"] == "medium confidence card"


def test_low_confidence_cards_are_not_even_linked(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(vendors={"vendor_id": _card("vendor identifier", confidence="low")})
    layer = _layer(doc, table("vendors", {"vendor_id": INT}, pk=("vendor_id",)), glossary=glossary)
    assert layer.entries == {} and layer.needs_stronger_evidence == [] and layer.stats["cards_seen"] == 0


def test_a_column_that_names_two_concepts_the_glossary_keeps_apart_is_ambiguous(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(x={"cv": _card("customer vendor number", evidence=["comment"])})
    layer = _layer(doc, table("x", {"id": INT, "cv": TXT}, pk=("id",)), glossary=glossary)
    assert layer.entries == {} and layer.ambiguous == [
        {"kind": "two_concepts", "column": "x.cv", "full_name": "customer vendor number", "concepts": ["party.customer", "party.vendor"]}
    ]


def test_a_concept_spread_over_many_tables_is_too_ambiguous_to_map(glossary: u.UniversalGlossary) -> None:
    names = [f"t{i}" for i in range(cc.MAX_CONCEPT_TABLES + 1)]
    tables = [table(n, {"id": INT, "sup": (TXT, "supplier name")}, pk=("id",)) for n in names]
    doc = _doc_of(**{n: {"sup": _card("supplier name", evidence=["comment"])} for n in names})
    layer = _layer(doc, *tables, glossary=glossary)
    assert layer.entries == {} and [a["kind"] for a in layer.ambiguous] == ["spread_over_tables"]


def test_a_role_the_columns_type_cannot_hold_is_refused(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(inv={"amt": _card("invoice amount", evidence=["comment"]), "amt2": _card("invoice amount", evidence=["comment"])})
    layer = _layer(doc, table("inv", {"id": INT, "amt": (TXT, "invoice amount"), "amt2": (NUM, "invoice amount")}, pk=("id",)), glossary=glossary)
    mapped = [m["column"] for m in layer.entries["invoice"]["mappings"]]
    assert mapped == ["amt2"] and layer.entries["invoice"]["mappings"][0]["role"] == "amount"  # the text column is no amount


def test_roles_follow_structure_first_then_the_words_left_of_the_concept(glossary: u.UniversalGlossary) -> None:
    tables = (
        table("po", {"id": INT, "vendor_id": INT, "po_dt": "DATE", "po_no": (TXT, "number")}, pk=("id",)),
        table("vendor", {"id": INT, "name": TXT}, pk=("id",)),
    )
    doc = _doc_of(po={"vendor_id": _card("vendor identifier", evidence=["fk"]), "po_dt": _card("purchase order date", evidence=["comment"]),
                      "po_no": _card("purchase order number", evidence=["comment"])})
    layer = _layer(doc, *tables, glossary=glossary)
    po = {m["column"]: m["role"] for m in layer.entries["purchase order"]["mappings"]}
    assert po == {"po_no": "identifier", "po_dt": "date"}


def test_a_foreign_key_is_a_mapping_but_never_the_best_one(glossary: u.UniversalGlossary) -> None:
    g = build(
        table("vendors", {"id": INT, "vendor_nm": (TXT, "supplier legal name")}, pk=("id",)),
        table("po", {"id": INT, "vendor_id": INT}, pk=("id",)),
        fks=(("po", "vendor_id", "vendors", "id"),),
    )
    doc = _doc_of(po={"vendor_id": _card("vendor identifier", evidence=["fk"])}, vendors={"vendor_nm": _card("vendor name", evidence=["comment"])})
    layer = cc.build_concept_layer(doc, g, glossary, set())
    roles = [(m["table"], m["role"]) for m in layer.entries["vendor"]["mappings"]]
    assert roles == [("vendors", "entity_name"), ("po", "foreign_key")] and layer.entries["vendor"]["maps_to"] == "vendors.vendor_nm"


def test_a_word_a_person_or_a_card_already_owns_is_not_taken(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(vendors={"vendor_nm": _card("vendor name", evidence=["comment"])})
    t = table("vendors", {"id": INT, "vendor_nm": (TXT, "name")}, pk=("id",))
    assert _layer(doc, t, glossary=glossary, taken={"vendor"}).entries == {}
    assert "vendor" in _layer(doc, t, glossary=glossary).entries


def test_intent_state_and_unknown_words_map_nothing(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(x={"a": _card("total", evidence=["comment"]), "b": _card("open status", evidence=["comment"]), "c": _card("flux capacitor", evidence=["comment"])})
    layer = _layer(doc, table("x", {"id": INT, "a": (NUM, "t"), "b": (TXT, "s"), "c": (TXT, "f")}, pk=("id",)), glossary=glossary)
    assert layer.entries == {} and layer.stats["linked"] == 0


def test_only_strong_aliases_become_synonyms_and_codes_never_do(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(c={"nm": _card("customer name", evidence=["comment"])})
    layer = _layer(doc, table("c", {"id": INT, "nm": (TXT, "name")}, pk=("id",)), glossary=glossary)
    assert layer.entries["customer"]["synonyms"] == ["client"]  # "buyer", "consumer" are conditional: not used
    for code in ("y", "N", "0", "1", "+", "-", " ", ""):
        assert cc.is_code_phrase(code), code
    for word in ("yes", "client", "item"):
        assert not cc.is_code_phrase(word)


def test_the_layer_is_deterministic_and_pure(glossary: u.UniversalGlossary) -> None:
    doc = _doc_of(vendors={"vendor_nm": _card("vendor name", evidence=["comment"]), "vendor_id": _card("vendor identifier")})
    t = table("vendors", {"vendor_id": INT, "vendor_nm": (TXT, "name")}, pk=("vendor_id",))
    before = copy.deepcopy(doc)
    assert _layer(doc, t, glossary=glossary).entries == _layer(doc, t, glossary=glossary).entries and doc == before


# ----------------------------------------------------------------------------------------------- wiring into the glossary entries


def _purchasing_graph_doc() -> tuple[Any, dict[str, Any]]:
    g = build(
        table("vendor", {"id": INT, "vendor_nm": (TXT, "supplier legal name")}, pk=("id",)),
    )
    doc = _doc_of(vendor={"vendor_nm": {**_card("vendor name", evidence=["comment"]), "phrases": ["vendor name", "y", "0", "supplier name"]}})
    return g, doc


def test_without_a_universal_glossary_the_entries_are_exactly_what_they_always_were() -> None:
    g, doc = _purchasing_graph_doc()
    entries = wiring.build_glossary_entries(doc, g)
    assert set(entries) == {"vendor name"} and "concept_id" not in entries["vendor name"]
    assert entries["vendor name"]["synonyms"] == ["y", "0", "supplier name"]  # old behaviour untouched
    assert wiring.build_glossary_and_layer(doc, g) == (entries, None)


def test_with_one_the_concept_entries_join_and_code_phrases_are_dropped(glossary: u.UniversalGlossary) -> None:
    g, doc = _purchasing_graph_doc()
    entries = wiring.build_glossary_entries(doc, g, glossary)
    assert set(entries) == {"vendor name", "vendor"} and entries["vendor name"]["synonyms"] == ["supplier name"]
    assert entries["vendor"]["auto"] == "auto_column_meaning" and entries["vendor"]["concept_id"] == "party.vendor"


def test_switching_off_removes_concept_entries_with_the_rest_and_never_touches_curated(dbs: Path, glossary: u.UniversalGlossary) -> None:
    (dbs / "pg_live" / "semantics").mkdir(parents=True, exist_ok=True)
    path = dbs / "pg_live" / wiring.GLOSSARY_REL
    curated = {"revenue": {"maps_to": "SUM(x)", "note": "mine", "synonyms": ["sales"]}}
    path.write_text(json.dumps(curated), encoding="utf-8")
    g, doc = _purchasing_graph_doc()
    wiring.merge_glossary("pg_live", wiring.build_glossary_entries(doc, g, glossary), dbs)
    assert set(json.loads(path.read_text())) == {"revenue", "vendor name", "vendor"}
    wiring.merge_glossary("pg_live", {}, dbs)
    assert json.loads(path.read_text()) == curated


def test_an_entry_marked_not_safe_for_prompts_is_never_shown_or_used_to_route(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {
        "vendor": {"maps_to": "vendor.vendor_nm", "synonyms": ["supplier"], "safe_for_prompt": True},
        "client": {"maps_to": "cust.cust_nm", "synonyms": [], "safe_for_prompt": False},
        "plain": {"maps_to": "plain.col", "synonyms": []},  # legacy or curated: no marker, unchanged behaviour
    }
    monkeypatch.setattr(pb, "get_raw_column_glossary", lambda db_id="x": data)
    text = pb.build_column_glossary_for_query("vendor client plain", "x")
    assert 'vendor.vendor_nm' in text and "plain.col" in text and "cust.cust_nm" not in text


def test_the_prompt_gets_only_the_glossary_entries_that_match_the_question(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {f"thing number {i}": {"maps_to": f"t{i}.c", "synonyms": [f"gizmo {i}"], "safe_for_prompt": True} for i in range(500)}
    data["vendor"] = {"maps_to": "vendors.vendor_nm", "synonyms": ["seller"], "safe_for_prompt": True}
    monkeypatch.setattr(pb, "get_raw_column_glossary", lambda db_id="x": data)
    text = pb.build_column_glossary_for_query("how many vendors", "x")
    assert text.count("\n") == 0 and "vendors.vendor_nm" in text  # one line: the entry the question names, never the 501
    flood = pb.build_column_glossary_for_query("thing number gizmo", "x")
    assert len(flood.splitlines()) <= 15  # and never more than the cap, however many match


# ----------------------------------------------------------------------------------------------- the review file


def _evidence(table_name: str, *cols: ColumnEvidence) -> dict[str, TableEvidence]:
    return {table_name: TableEvidence(table=table_name, name=table_name, kind="table", comment=None, neighbors=(), columns=cols, hash="h")}


def _col(name: str, *kinds: str, fk: bool = False) -> ColumnEvidence:
    return ColumnEvidence(name=name, type="int", is_primary_key=False, is_foreign_key=fk, items=tuple(Item(k, k, f"{k} text") for k in kinds),
                          common=False, explains_itself=False)


def test_a_stored_code_needs_two_independent_signals_or_it_stays_unknown() -> None:
    g = build(table("t", {"id": INT, "a": TXT, "b": TXT, "c": TXT, "d": TXT}, pk=("id",)))
    ev = _evidence("t", _col("a", "case"), _col("b", "case", "comment"), _col("c", "case", fk=True), _col("d", "case", "case"))
    review = rv.build_review({"tables": {}}, g, ev, None)
    by = {r["column"]: r for r in review["code_values"]}
    assert by["a"]["status"] == "unknown" and by["a"]["signals"] == ["case"]  # a CASE alone is not enough: never a code from one signal
    assert by["b"]["status"] == "supported" and by["b"]["signals"] == ["case", "comment"]
    assert by["c"]["status"] == "supported" and by["c"]["signals"] == ["case", "fk"]  # a lookup table behind the code
    assert by["d"]["status"] == "unknown"  # the same kind twice is one signal
    assert review["summary"]["code_values"] == 4 and review["summary"]["code_values_unknown"] == 2
    assert "uncertain_mappings" in review and all(len(v) <= rv.MAX_REVIEW_PER_SECTION for v in review.values() if isinstance(v, list))


def test_uncertain_and_ambiguous_items_are_listed_and_high_ones_are_not() -> None:
    g = build(table("t", {"id": INT, "a": TXT, "b": TXT, "c": TXT, "d": TXT, "e": TXT, "f": TXT}, pk=("id",)))
    cards = {
        "a": _card("alpha thing"),
        "b": _card("beta thing", confidence="medium"),
        "c": _card("gamma thing", confidence="low"),
        "d": _card("delta thing", flags=["inconsistent_across_tables"]),
        "e": {**_card("echo thing"), "phrases": ["shared phrase"]},
        "f": {**_card("foxtrot thing"), "phrases": ["shared phrase"]},
    }
    cards["g"] = {**_card("golf thing"), "phrases": ["shared phrase"]}
    g = build(table("t", {"id": INT, **{k: TXT for k in cards}}, pk=("id",)))
    review = rv.build_review(_doc_of(t=cards), g, {}, cc.ConceptLayer(needs_stronger_evidence=[{"concept_id": "x.y"}], ambiguous=[{"kind": "two_concepts"}]))
    assert [r["column"] for r in review["uncertain_mappings"]] == ["b", "c", "d"]
    assert review["uncertain_mappings"][2]["reason"] == "inconsistent_across_tables"
    assert review["ambiguous_aliases"][0] == {"kind": "alias_names_many_columns", "alias": "shared phrase", "columns": 3} and review["ambiguous_aliases"][1]["kind"] == "two_concepts"
    assert review["needs_stronger_evidence"] == [{"concept_id": "x.y"}]


def test_the_review_is_capped_per_section() -> None:
    cols = {f"c{i}": _card(f"thing {i}", confidence="low") for i in range(rv.MAX_REVIEW_PER_SECTION + 50)}
    g = build(table("t", {"id": INT, **{k: TXT for k in cols}}, pk=("id",)))
    review = rv.build_review(_doc_of(t=cols), g, {}, None)
    assert len(review["uncertain_mappings"]) == rv.MAX_REVIEW_PER_SECTION and review["summary"]["uncertain_mappings"] == rv.MAX_REVIEW_PER_SECTION + 50


def test_the_review_file_follows_the_ownership_rules(dbs: Path) -> None:
    review = {"summary": {"uncertain_mappings": 1}, "uncertain_mappings": [{"table": "t"}]}
    path = dbs / "pg_live" / rv.REVIEW_REL
    out = rv.write_review("pg_live", review, dbs)
    doc = json.loads(path.read_text())
    assert out["written"] and doc["generated_by"] == rv.GENERATED_BY and doc["review"] == review and "database works without it" in doc["note"]
    assert rv.write_review("pg_live", review, dbs)["unchanged"] is True
    assert rv.write_review("pg_live", {**review, "summary": {"uncertain_mappings": 2}}, dbs)["written"]
    path.write_text('{"mine": true}', encoding="utf-8")  # a file this phase did not write is never replaced
    assert "left untouched" in rv.write_review("pg_live", review, dbs)["skipped"] and path.read_text() == '{"mine": true}'
    path.write_text("{broken", encoding="utf-8")
    assert "left untouched" in rv.write_review("pg_live", review, dbs)["skipped"]
    assert "pinned" in rv.write_review("erp_main", review, dbs)["skipped"] and "does not exist" in rv.write_review("ghost_db", review, dbs)["skipped"]


# ----------------------------------------------------------------------------------------------- the whole phase


@pytest.mark.asyncio
async def test_with_the_flag_off_nothing_new_is_written(dbs: Path) -> None:
    put(dbs)
    report = await run(dbs, FakeLLM())
    assert "concepts" not in report and "review" not in report
    assert not (dbs / "pg_live" / rv.REVIEW_REL).exists()
    glossary = json.loads((dbs / "pg_live" / wiring.GLOSSARY_REL).read_text())
    assert not any("concept_id" in e or "safe_for_prompt" in e for e in glossary.values())


@pytest.mark.asyncio
async def test_with_the_flag_on_concepts_and_the_review_are_written_and_a_second_run_changes_nothing(dbs: Path) -> None:
    put(dbs)
    first = await run(dbs, FakeLLM(), concepts=True)
    glossary = json.loads((dbs / "pg_live" / wiring.GLOSSARY_REL).read_text())
    linked = {t: e for t, e in glossary.items() if "concept_id" in e}
    assert linked and all(e["auto"] == "auto_column_meaning" and e["confidence"] == "high" and e["safe_for_prompt"] is True for e in linked.values())
    assert all(m["confidence"] == "high" and m["safe_for_prompt"] and m["evidence"] for e in linked.values() for m in e["mappings"])
    review = json.loads((dbs / "pg_live" / rv.REVIEW_REL).read_text())["review"]
    assert first["review"]["written"] and first["concepts"]["entries"] == len(linked) and review["summary"]["code_values"] >= 1
    assert "po_head" not in json.dumps(first["concepts"])  # the report is counts only
    second = await run(dbs, FakeLLM(), concepts=True)
    assert second["glossary"]["unchanged"] and second["review"]["unchanged"]


@pytest.mark.asyncio
async def test_an_unusable_universal_glossary_leaves_the_card_entries_alone(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(dbs)
    monkeypatch.setattr(u, "load_universal", lambda path=None: None)
    report = await run(dbs, FakeLLM(), concepts=True)
    glossary = json.loads((dbs / "pg_live" / wiring.GLOSSARY_REL).read_text())
    assert report["ok"] and glossary and not any("concept_id" in e for e in glossary.values())
    assert "concepts" not in report and report["review"]["written"]  # the review still tells a person what is uncertain


@pytest.mark.asyncio
async def test_with_prompt_injection_off_every_generated_entry_goes_but_the_review_stays(dbs: Path) -> None:
    put(dbs)
    await run(dbs, FakeLLM(), concepts=True)
    report = await run(dbs, FakeLLM(), concepts=True, inject=False)
    assert report["glossary"]["removed"] > 0
    left = dbs / "pg_live" / wiring.GLOSSARY_REL
    assert not left.exists() or json.loads(left.read_text()) == {}
    assert (dbs / "pg_live" / rv.REVIEW_REL).exists()


@pytest.mark.asyncio
async def test_a_failure_in_the_review_never_fails_the_phase(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(dbs)
    monkeypatch.setattr(rv, "build_review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    report = await run(dbs, FakeLLM(), concepts=True)
    assert report["ok"] and report["review"] == {"error": "RuntimeError: boom"} and report["glossary"]["written"]


@pytest.mark.asyncio
async def test_the_pinned_database_is_never_touched(dbs: Path) -> None:
    assert "pinned" in (await run_pinned(dbs))["skipped"]


async def run_pinned(dbs: Path) -> dict[str, Any]:
    from src.sql.sa import meaning_phase as ph

    return await ph.run_meaning_phase(db_id="erp_main", databases_dir=dbs, llm=FakeLLM(), concepts=True)


# ----------------------------------------------------------------------------------------------- the flag, the switch, the sync call


def test_flag_is_registered_off_by_default_and_documented() -> None:
    from src.sql.sync import resync
    from src.utils.feature_flags import DB_FEATURE_FLAGS, DEFAULT_NEW_DB_FLAGS, OFF_BY_DEFAULT_FLAGS

    assert "auto_semantic_glossary_apply" in DB_FEATURE_FLAGS and "auto_semantic_glossary_apply" in OFF_BY_DEFAULT_FLAGS
    assert DEFAULT_NEW_DB_FLAGS["auto_semantic_glossary_apply"] is False
    assert ("auto_semantic_glossary_apply", "semantic_glossary_apply") in resync._SWITCHES  # toggling it re-runs the phase: no stale skip
    cfg = yaml.safe_load((ROOT / "config" / "feature_flags.example.yaml").read_text(encoding="utf-8"))
    assert cfg["db_defaults"]["auto_semantic_glossary_apply"] is False and cfg["db_features"]["_example_db"]["auto_semantic_glossary_apply"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("flags", "concepts"), [(("auto_catalog", "auto_column_meaning"), False), (("auto_catalog", "auto_column_meaning", "auto_semantic_glossary_apply"), True)])
async def test_the_sync_passes_the_flag_to_the_meaning_phase(sqlite_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...], concepts: bool) -> None:
    from src.sql.sa import meaning_phase as ph
    from src.sql.schema_retrieval import sync_live_schema

    fake = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(ph, "run_meaning_phase", fake)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in flags)
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok"
    fake.assert_awaited_once()
    assert fake.await_args.kwargs["concepts"] is concepts and fake.await_args.kwargs["db_id"] == "pg_live"
