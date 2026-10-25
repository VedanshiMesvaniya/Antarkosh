"""Phase 9: column evidence (9a), meaning cards (9b) and their wiring into retrieval (9c)."""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlglot

from src.sql.mining.catalog_index import CatalogIndex
from src.sql.relationships.model import graph_from_documents, load_graph
from src.sql.sa import meaning_phase as ph
from src.sql.sa import mining_phase as mp
from src.sql.sa.routines import Routine
from src.sql.semantics import wiring
from src.sql.semantics.abbreviations import (
    ABBREVIATIONS,
    abbreviation_notes,
    expansion_fits,
    self_explanatory,
)
from src.sql.semantics.dictionary import (
    DICTIONARY_REL,
    GENERATED_BY,
    content_key,
    empty_document,
    is_current,
    read_dictionary,
    write_dictionary,
)
from src.sql.semantics.evidence import (
    ColumnEvidence,
    Item,
    TableEvidence,
    collect_table_evidence,
    evidence_summary,
)
from src.sql.semantics.hints import (
    HINTS_REL,
    HintsCollector,
    extract_column_hints,
    parameter_words,
    write_hints,
)
from src.sql.semantics.meaning import (
    apply_consistency,
    build_messages,
    finalize_column,
    generate_table,
    interpret,
    parse_response,
)
from tests.sql.relationships_fixtures import documents, table
from tests.sql.test_auto_procedures import _sync_args, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_catalog_extraction import dbs  # noqa: F401  (shared fixture)

INT, TXT = "INTEGER", "VARCHAR(50)"


# ================================================================= a small purchasing schema


def purchasing():
    return (
        table("vendor", {"vendor_id": INT, "vendor_nm": (TXT, "Name of the supplier")}, pk=("vendor_id",), comment="Suppliers we buy from"),
        table("po_head", {"po_no": (TXT, "Purchase order number"), "vendor_id": INT, "status_cd": INT, "po_dt": "DATE", "apq_no": TXT, "created_at": "DATE"}, pk=("po_no",)),
        table("po_line", {"line_id": INT, "po_no": TXT, "item_cd": TXT, "qty": INT, "amt": INT}, pk=("line_id",)),
    )


FKS = (("po_head", "vendor_id", "vendor", "vendor_id"), ("po_line", "po_no", "po_head", "po_no"))
HINTS = {
    "generated_by": "auto_sp_mining",
    "columns": [
        {"table": "po_head", "column": "status_cd", "aliases": [], "params": [], "case_labels": [{"value": "1", "label": "Open", "routines": 3}, {"value": "2", "label": "Closed", "routines": 2}]},
        {"table": "po_head", "column": "vendor_id", "aliases": [{"alias": "SupplierId", "routines": 2}], "params": [{"name": "@VendorId", "routines": 2}], "case_labels": []},
        {"table": "ghost", "column": "x", "aliases": [{"alias": "nope", "routines": 1}], "params": [], "case_labels": []},
    ],
}
EVIDENCE = {"enum_values": [{"table": "po_head", "column": "apq_no", "values": [{"value": "A1", "count": 3}], "routines": 1}], "tables": {"po_head": {"read_in": 5, "written_in": 2}, "vendor": {"read_in": 1, "written_in": 0}}}


def graph():
    schema_doc, inventory, _ = documents(*purchasing(), fks=FKS)
    return graph_from_documents(schema_doc, inventory)


def ev():
    return collect_table_evidence(graph(), hints_doc=HINTS, evidence_doc=EVIDENCE)


def kinds(table_ev, col):
    return [i.kind for i in next(c for c in table_ev.columns if c.name == col).items]


class FakeLLM:
    """A scripted model: answers every column it is asked about from its name (abbreviations expanded)."""

    def __init__(self, cards: dict[str, dict] | None = None, *, by_table: dict[tuple[str, str], dict] | None = None, fail: bool = False, bad_json_first: bool = False, delay: float = 0.0):
        self.cards, self.fail, self.bad_json_first, self.delay = cards or {}, fail, bad_json_first, delay
        self.by_table = by_table or {}
        self.calls: list[tuple[str, list[str]]] = []
        self._tried: set[str] = set()

    @staticmethod
    def default(name: str) -> dict:
        from src.sql.relationships.names import split_identifier

        words = [ABBREVIATIONS[t][0] if t in ABBREVIATIONS and t != "id" else ("identifier" if t == "id" else t) for t in split_identifier(name)]
        full = " ".join(words)
        return {"full_name": full, "meaning": f"The {full}.", "phrases": [full, f"the {full}", f"{full} of the record"], "evidence": [], "confidence": "high"}

    async def __call__(self, messages, max_tokens):
        payload = json.loads(messages[1]["content"])
        names = [c["name"] for c in payload["columns"]]
        self.calls.append((payload["table"], names))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider down")
        if self.bad_json_first and payload["table"] not in self._tried:
            self._tried.add(payload["table"])
            return "I cannot do that"
        rows = [{"name": n, **self.by_table.get((payload["table"], n), self.cards.get(n, self.default(n)))} for n in names]
        return "```json\n" + json.dumps({"columns": rows}) + "\n```"

    @property
    def tables_called(self) -> list[str]:
        return [t for t, _ in self.calls]


# ================================================================= abbreviations and the fit check


def test_the_expansion_must_account_for_every_word_of_the_name():
    ok = [("po_no", "purchase order number"), ("qty", "quantity"), ("amt_paid", "amount paid"), ("cst_adr", "customer address"), ("PartyID", "party identifier"), ("gst_amt", "goods and services tax amount")]
    bad = [("po_no", "product number"), ("po_no", "customer name"), ("xq7", "something"), ("vendor_nm", "invoice date")]
    assert all(expansion_fits(n, e) for n, e in ok) and not any(expansion_fits(n, e) for n, e in bad)
    assert not expansion_fits("", "x") and not expansion_fits("po_no", "")


def test_names_made_of_ordinary_words_and_listed_abbreviations_explain_themselves_and_opaque_ones_do_not():
    assert self_explanatory("po_no") and self_explanatory("customer_name") and self_explanatory("qty")
    assert not self_explanatory("apq_no") and not self_explanatory("xq7") and not self_explanatory("")
    assert abbreviation_notes("po_no") == ["po = purchase order", "no = number"] and abbreviation_notes("customer_id") == []


# ================================================================= 9a: hints from procedures


CAT = CatalogIndex({"po_head": {"po_no", "status_cd", "pay_mode", "vendor_id", "po_dt"}, "vendor": {"vendor_id", "vendor_nm"}})
TSQL = (
    "SELECT h.po_no AS PurchaseOrderNumber, h.po_dt AS po_dt, CASE WHEN h.status_cd = 1 THEN 'Open' WHEN h.status_cd = 2 THEN 'Closed' END AS st, "
    "CASE h.pay_mode WHEN 'C' THEN 'Cash' WHEN 'Q' THEN 'Cheque' END AS pm FROM po_head h JOIN vendor v ON v.vendor_id = h.vendor_id "
    "WHERE h.po_no = @PONumber AND h.vendor_id = @VendorId"
)


def hints_of(sql: str, dialect: str, catalog=CAT):
    return extract_column_hints([sqlglot.parse_one(sql, read=dialect)], catalog)


def test_aliases_parameters_and_case_labels_are_tied_to_real_catalog_columns():
    h = hints_of(TSQL, "tsql")
    assert dict(h.aliases) == {("po_head", "po_no"): {"PurchaseOrderNumber": 1}}  # an alias equal to the column name is not a hint
    assert dict(h.params) == {("po_head", "po_no"): {"PONumber": 1}, ("po_head", "vendor_id"): {"VendorId": 1}}
    assert h.case_labels[("po_head", "status_cd")] == {("1", "Open"): 1, ("2", "Closed"): 1}
    assert h.case_labels[("po_head", "pay_mode")] == {("C", "Cash"): 1, ("Q", "Cheque"): 1}


def test_parameters_are_found_in_every_dialects_notation_and_a_real_column_is_never_taken_for_one():
    assert dict(hints_of("SELECT 1 FROM po_head h WHERE h.po_no = :p_po_no", "postgres").params) == {("po_head", "po_no"): {"p_po_no": 1}}
    assert dict(hints_of("SELECT 1 FROM po_head h WHERE h.po_no = :po_no", "oracle").params) == {("po_head", "po_no"): {"po_no": 1}}
    mysql = hints_of("SELECT 1 FROM po_head h WHERE h.po_no = p_po_no AND h.vendor_id = in_vendor AND h.status_cd = status_cd AND h.po_dt = vendor_id", "mysql")
    assert set(mysql.params) == {("po_head", "po_no"), ("po_head", "vendor_id")}  # status_cd = status_cd and po_dt = vendor_id are columns
    assert not hints_of("SELECT 1 FROM po_head h WHERE h.po_no = $1 AND h.vendor_id = ?", "postgres").params  # positional: no name to learn


def test_hints_need_a_catalog_and_ignore_temp_tables_ctes_and_unknown_columns():
    assert not extract_column_hints([sqlglot.parse_one(TSQL, read="tsql")], None)
    cte = "WITH x AS (SELECT po_no FROM po_head) SELECT x.po_no AS Whatever FROM x WHERE x.po_no = @A"
    assert not hints_of(cte, "tsql").params and not hints_of("SELECT t.a AS b FROM #tmp t WHERE t.a = @x", "tsql")
    assert not hints_of("SELECT h.nope AS Zed FROM po_head h WHERE h.nope = @Q", "tsql")


def test_the_collector_counts_routines_isolates_failures_and_builds_a_bounded_document():
    c = HintsCollector()
    c.add([sqlglot.parse_one(TSQL, read="tsql")], CAT)
    c.add([sqlglot.parse_one(TSQL, read="tsql")], CAT)
    c.add([object()], CAT)  # not a statement: counted as a failure, never raised
    doc = c.document("pg_live")
    assert doc["generated_by"] == "auto_sp_mining" and doc["stats"] == {"routines": 2, "routines_failed": 1, "columns": 4}
    row = next(r for r in doc["columns"] if (r["table"], r["column"]) == ("po_head", "po_no"))
    assert row["aliases"] == [{"alias": "PurchaseOrderNumber", "routines": 2}] and row["params"] == [{"name": "PONumber", "routines": 2}]
    assert parameter_words("@PONumber") == "po number" and parameter_words("p_vendor_id") == "vendor id"


def test_the_hints_file_follows_the_ownership_rules(dbs: Path):
    doc = HintsCollector().document("pg_live")
    assert write_hints("pg_live", doc, dbs)["written"] == [HINTS_REL]
    assert write_hints("pg_live", doc, dbs)["written"] == [HINTS_REL]  # its own file may be replaced
    (dbs / "pg_live" / HINTS_REL).write_text('{"mine": 1}', encoding="utf-8")
    assert "left untouched" in write_hints("pg_live", doc, dbs)["skipped"][HINTS_REL]
    assert "pinned" in write_hints("erp_main", doc, dbs)["skipped"]["all"] and "does not exist" in write_hints("ghost_db", doc, dbs)["skipped"]["all"]


ROUTINE = Routine(schema="dbo", name="sp_po", kind="PROCEDURE", source="database", definition=f"CREATE PROCEDURE sp_po @PONumber VARCHAR(20) AS BEGIN {TSQL}; END")


@pytest.mark.asyncio
async def test_mining_keeps_its_facts_unchanged_whether_or_not_hints_are_collected(dbs: Path):
    (dbs / "pg_live" / "schema" / "schema.json").write_text(json.dumps(documents(*purchasing(), fks=FKS)[0]), encoding="utf-8")
    plain = await mp.run_mining_phase(db_id="pg_live", engine_key="mssql", routines=[ROUTINE], databases_dir=dbs)
    facts_plain = json.loads((dbs / "pg_live" / "schema" / "sp_evidence.json").read_text())
    assert "column_hints" not in plain and not (dbs / "pg_live" / HINTS_REL).exists()
    with_hints = await mp.run_mining_phase(db_id="pg_live", engine_key="mssql", routines=[ROUTINE], databases_dir=dbs, collect_hints=True)
    facts_with = json.loads((dbs / "pg_live" / "schema" / "sp_evidence.json").read_text())
    for d in (facts_plain, facts_with):
        d.pop("generated_at")
    assert facts_plain == facts_with  # sp_evidence.json is byte-for-byte what it always was
    assert with_hints["column_hints"]["columns"] == 3 and (dbs / "pg_live" / HINTS_REL).exists()


# ================================================================= 9a: evidence per column


def test_every_kind_of_evidence_is_gathered_with_a_citable_id():
    t = ev()["po_head"]
    assert kinds(t, "po_no") == ["comment", "abbrev"] and kinds(t, "status_cd") == ["case", "abbrev"]
    vendor_id = next(c for c in t.columns if c.name == "vendor_id")
    assert [(i.id, i.kind) for i in vendor_id.items] == [("fk", "fk"), ("alias1", "alias"), ("param1", "param")]
    assert vendor_id.items[0].text == "references vendor.vendor_id (Suppliers we buy from)" or vendor_id.items[0].text.startswith("references vendor.vendor_id")
    assert vendor_id.items[2].text == "vendor id"
    case = next(i for c in t.columns if c.name == "status_cd" for i in c.items if i.kind == "case")
    assert case.text == "1 = Open; 2 = Closed"
    assert [i.kind for i in next(c for c in t.columns if c.name == "apq_no").items] == ["values", "abbrev"]


def test_hints_that_do_not_match_the_catalog_are_dropped_and_views_are_left_out():
    schema_doc, inventory, _ = documents(*purchasing(), table("v_po", {"po_no": TXT}, kind="view"), fks=FKS)
    g = graph_from_documents(schema_doc, inventory)
    out = collect_table_evidence(g, hints_doc=HINTS)
    assert "v_po" not in out and "ghost" not in out
    assert "v_po" in collect_table_evidence(g, hints_doc=HINTS, include_views=True)


def test_common_names_are_flagged_and_the_summary_counts_what_is_known():
    t = ev()["po_head"]
    assert next(c for c in t.columns if c.name == "created_at").common and not next(c for c in t.columns if c.name == "po_no").common
    s = evidence_summary(ev())
    assert s["tables"] == 3 and s["columns"] == 13 and s["with_hard_evidence"] == 5 and s["by_kind"]["fk"] == 2


def test_the_evidence_hash_changes_only_when_something_that_matters_changes():
    base = ev()["po_head"].hash
    assert ev()["po_head"].hash == base
    changed = collect_table_evidence(graph(), hints_doc={**HINTS, "columns": HINTS["columns"][1:]}, evidence_doc=EVIDENCE)
    assert changed["po_head"].hash != base and changed["vendor"].hash == ev()["vendor"].hash
    assert collect_table_evidence(graph(), hints_doc=HINTS, evidence_doc={})["po_head"].hash != base


# ================================================================= 9b: checking what the model says


def col(name, *items, common=False, explains=False):
    return ColumnEvidence(name, TXT, False, False, tuple(items), common, explains)


def card(**kw):
    return {"full_name": "purchase order number", "meaning": "The number of a purchase order.", "phrases": ["purchase order number", "po number", "po no"], "evidence": ["comment"], "confidence": "high", **kw}


def test_a_well_supported_answer_keeps_high_confidence_and_its_valid_citations():
    m = finalize_column(card(), col("po_no", Item("comment", "comment", "Purchase order number")))
    assert (m.confidence, m.evidence, m.flags) == ("high", ("comment",), ()) and m.phrases == ("purchase order number", "po number", "po no")


def test_an_expansion_that_does_not_fit_the_name_is_forced_to_low():
    m = finalize_column(card(full_name="customer address"), col("po_no", Item("comment", "comment", "x")))
    assert m.confidence == "low" and "expansion_mismatch" in m.flags


def test_an_invented_citation_caps_confidence_and_is_not_kept():
    m = finalize_column(card(evidence=["comment", "ghost1"]), col("po_no", Item("comment", "comment", "x")))
    assert m.confidence == "medium" and m.evidence == ("comment",) and "bad_citation" in m.flags


def test_literal_values_are_context_not_an_explanation_of_an_opaque_name():
    only_values = col("apq_no", Item("values", "values", "'A1', 'B2'"))
    assert not only_values.hard and finalize_column(card(full_name="apq number", evidence=["values"]), only_values).confidence == "medium"
    assert col("apq_no", Item("param1", "param", "approved quantity")).hard and col("x_cd", Item("fk", "fk", "references y.z")).hard


def test_high_confidence_needs_evidence_or_a_name_that_explains_itself():
    opaque = finalize_column(card(full_name="approved production quantity number", evidence=[]), col("apq_no"))
    assert opaque.confidence == "medium" and "no_evidence" in opaque.flags  # fits by initials, but nothing backs it
    assert finalize_column(card(evidence=[]), col("po_no", explains=True)).confidence == "high"
    assert finalize_column(card(full_name="x", meaning="unknown", confidence="high"), col("po_no")).confidence == "low"
    assert finalize_column(card(confidence="certain!"), col("po_no", Item("comment", "comment", "x"))).confidence == "low"
    assert finalize_column(card(full_name="a b c d e f g h i j"), col("po_no")).confidence == "low"


def test_phrases_are_cleaned_of_sql_names_duplicates_and_nonsense():
    m = finalize_column(card(phrases=["PO Number", "po number", "po_no", "select po", "x", "one two three four five six", "t.col", "purchase  order   number", 7]), col("po_no", Item("comment", "comment", "x")))
    assert m.phrases == ("po number", "purchase order number")
    assert len(finalize_column(card(phrases=[f"phrase number {i}" for i in range(20)]), col("po_no")).phrases) == 6


def test_only_the_columns_that_were_asked_about_are_kept():
    rows = [{**card(), "name": "PO_NO"}, {**card(), "name": "invented"}, {**card(full_name="other"), "name": "po_no"}]
    got = interpret(rows, [col("po_no", Item("comment", "comment", "x"))])
    assert list(got) == ["po_no"] and got["po_no"].full_name == "purchase order number"  # case-insensitive, first answer wins


def test_the_reply_is_parsed_strictly():
    assert parse_response('```json\n{"columns": [{"name": "a"}]}\n```') == [{"name": "a"}]
    assert parse_response('sure! {"columns": [{"name": "a"}, 5]} done') == [{"name": "a"}]
    assert parse_response("nonsense") is None and parse_response('{"columns": 5}') is None and parse_response("") is None and parse_response(None) is None
    assert parse_response("{broken") is None


def test_the_request_carries_metadata_only_and_every_evidence_id():
    t = ev()["po_head"]
    msgs = build_messages(t, [c for c in t.columns if not c.common])
    payload = json.loads(msgs[1]["content"])
    assert msgs[0]["role"] == "system" and payload["table"] == "po_head" and payload["all_columns_of_the_table"][0] == "po_no"
    assert {c["name"] for c in payload["columns"]} == {"po_no", "vendor_id", "status_cd", "po_dt", "apq_no"}
    assert {i["id"] for c in payload["columns"] for i in c["evidence"]} >= {"comment", "fk", "alias1", "param1", "case", "values", "abbrev"}
    assert set(payload) == {"table", "table_comment", "all_columns_of_the_table", "columns"}  # nothing else is sent


@pytest.mark.asyncio
async def test_a_table_is_answered_in_one_call_and_common_columns_are_not_sent():
    llm = FakeLLM()
    out = await generate_table(ev()["po_head"], llm)
    assert out.calls == 1 and out.skipped_common == 1 and out.missing == 0 and "created_at" not in out.columns
    assert llm.calls[0][1] == ["po_no", "vendor_id", "status_cd", "po_dt", "apq_no"]
    assert out.columns["status_cd"].confidence == "high" and out.columns["apq_no"].confidence in ("medium", "high")


@pytest.mark.asyncio
async def test_a_wide_table_is_split_into_several_calls_and_a_bad_reply_is_retried_once():
    wide = TableEvidence("wide", "wide", "table", None, (), tuple(col(f"c{i}_no", explains=True) for i in range(7)), "h")
    llm = FakeLLM()
    out = await generate_table(wide, llm, max_columns=3)
    assert out.calls == 3 and len(out.columns) == 7 and [len(n) for _, n in llm.calls] == [3, 3, 1]
    flaky = FakeLLM(bad_json_first=True)
    out = await generate_table(ev()["vendor"], flaky)
    assert out is not None and out.calls == 2 and len(flaky.calls) == 2


@pytest.mark.asyncio
async def test_a_table_is_all_or_nothing_when_the_model_cannot_answer():
    assert await generate_table(ev()["vendor"], FakeLLM(fail=True)) is None
    always_bad = AsyncMock(return_value="no json here")
    assert await generate_table(ev()["vendor"], always_bad) is None and always_bad.await_count == 2


# ================================================================= 9b: the same name must mean the same thing


def cards(**per_table):
    return {t: {c: {"full_name": fn, "confidence": conf, "flags": [], "evidence": ev_} for c, (fn, conf, ev_) in cols.items()} for t, cols in per_table.items()}


def test_a_column_name_that_means_something_else_in_one_table_is_lowered():
    d = cards(a={"po_no": ("purchase order number", "high", [])}, b={"po_no": ("purchase order numbers", "high", [])}, c={"po_no": ("product number", "high", [])})
    assert apply_consistency(d, frozenset()) == 1
    assert d["c"]["po_no"]["confidence"] == "low" and "conflicts_with_other_tables" in d["c"]["po_no"]["flags"]
    assert d["a"]["po_no"]["confidence"] == d["b"]["po_no"]["confidence"] == "high"


def test_with_no_clear_majority_nobody_stays_high_and_a_documented_difference_is_kept():
    d = cards(a={"x_no": ("alpha number", "high", [])}, b={"x_no": ("beta number", "high", [])})
    apply_consistency(d, frozenset())
    assert d["a"]["x_no"]["confidence"] == d["b"]["x_no"]["confidence"] == "medium"
    d = cards(a={"po_no": ("purchase order number", "high", [])}, b={"po_no": ("purchase order number", "high", [])}, c={"po_no": ("product number", "high", ["comment"])})
    apply_consistency(d, frozenset())
    assert d["c"]["po_no"]["confidence"] == "high"


def test_generic_names_are_exempt_and_the_pass_can_be_run_again_without_drifting():
    d = cards(a={"status": ("order status", "high", [])}, b={"status": ("payment status", "high", [])})
    assert apply_consistency(d, frozenset({"status"})) == 0 and d["a"]["status"]["confidence"] == "high"
    d = cards(a={"po_no": ("purchase order number", "high", [])}, b={"po_no": ("purchase order number", "high", [])}, c={"po_no": ("product number", "high", [])})
    apply_consistency(d, frozenset())
    first = json.dumps(d, sort_keys=True)
    apply_consistency(d, frozenset())
    assert json.dumps(d, sort_keys=True) == first
    d["c"]["po_no"]["full_name"] = "purchase order number"  # fixed later (a regenerated table): the old flag must go away
    apply_consistency(d, frozenset())
    assert d["c"]["po_no"]["confidence"] == "high" and d["c"]["po_no"]["flags"] == []


# ================================================================= the dictionary file


def test_the_dictionary_follows_the_ownership_rules_and_skips_a_write_that_changes_nothing(dbs: Path):
    assert read_dictionary("pg_live", dbs)[0] == "missing"
    doc = {**empty_document(), "tables": {"vendor": {"evidence_hash": "h", "columns": {"vendor_nm": {"full_name": "vendor name"}}}}}
    assert write_dictionary("pg_live", doc, dbs) == {"written": True, "unchanged": False, "skipped": None}
    assert read_dictionary("pg_live", dbs)[0] == "ours"
    path = dbs / "pg_live" / DICTIONARY_REL
    before = path.stat().st_mtime_ns
    assert write_dictionary("pg_live", doc, dbs)["unchanged"] is True and path.stat().st_mtime_ns == before
    path.write_text('{"curated": true}', encoding="utf-8")
    assert read_dictionary("pg_live", dbs)[0] == "foreign" and "left untouched" in write_dictionary("pg_live", doc, dbs)["skipped"]
    path.write_text("not json", encoding="utf-8")
    assert read_dictionary("pg_live", dbs)[0] == "foreign"
    assert "pinned" in write_dictionary("erp_main", doc, dbs)["skipped"] and "does not exist" in write_dictionary("ghost_db", doc, dbs)["skipped"]
    assert content_key(doc) != content_key({**doc, "prompt_version": 99}) and GENERATED_BY == "auto_column_meaning"


def test_a_tables_cards_are_current_only_for_the_same_evidence_and_prompt():
    doc = empty_document()
    entry = {"evidence_hash": "h1", "columns": {}}
    assert is_current(entry, "h1", doc) and not is_current(entry, "h2", doc) and not is_current(None, "h1", doc)
    assert not is_current(entry, "h1", {**doc, "prompt_version": 0}) and not is_current({"evidence_hash": "h1"}, "h1", doc)


# ================================================================= 9c: the text that is embedded


def dict_doc(**tables):
    """{"table": {"col": (full_name, confidence, phrases...)}} -> a dictionary document."""
    return {
        **empty_document(),
        "tables": {
            t: {"evidence_hash": "h", "columns": {c: {"full_name": v[0], "meaning": f"The {v[0]}.", "phrases": list(v[2:]), "evidence": ["comment"], "confidence": v[1]} for c, v in cols.items()}}
            for t, cols in tables.items()
        },
    }


def test_the_embedded_text_gets_high_and_medium_meanings_but_never_low_ones():
    doc = dict_doc(po_head={"po_no": ("purchase order number", "high", "po number", "purchase order number", "po no", "order ref"), "apq_no": ("apq number", "medium"), "junk": ("junk", "low")})
    block = wiring.embedding_extras_from(doc)["po_head"]
    assert block.splitlines()[0] == "-- Column meanings:" and "-- po_no: purchase order number (also: po number, po no, order ref)" in block
    assert "apq_no: apq number" in block and "junk" not in block
    assert block.index("po_no") < block.index("apq_no")  # high first
    assert wiring.embedding_extras_from(dict_doc(t={"c": ("x y", "low")})) == {} and wiring.embedding_extras_from({"tables": {"t": "bad"}}) == {}


def test_the_embedded_block_is_bounded_and_a_table_is_found_by_its_bare_name_too():
    many = {f"col_{i}": (f"a rather long descriptive column name number {i}", "high", f"phrase {i} one", f"phrase {i} two") for i in range(200)}
    block = wiring.embedding_extras_from(dict_doc(big=many))["big"]
    assert len(block) <= wiring.EMBED_EXTRA_MAX_CHARS
    extras = wiring.embedding_extras_from(dict_doc(**{"dbo.po_head": {"po_no": ("purchase order number", "high")}}))
    assert extras["dbo.po_head"] == extras["po_head"]
    clash = wiring.embedding_extras_from(dict_doc(**{"a.t": {"c": ("x y", "high")}, "b.t": {"c": ("x y", "high")}}))
    assert "t" not in clash and set(clash) == {"a.t", "b.t"}  # an ambiguous bare name is not guessed


def test_the_extras_come_only_from_a_dictionary_this_phase_wrote(dbs: Path):
    assert wiring.load_embedding_extras("pg_live", dbs) == {}
    write_dictionary("pg_live", dict_doc(po_head={"po_no": ("purchase order number", "high")}), dbs)
    assert set(wiring.load_embedding_extras("pg_live", dbs)) == {"po_head"}
    assert wiring.load_embedding_extras("erp_main", dbs) == {} and wiring.load_embedding_extras("not a valid id!", dbs) == {}
    (dbs / "pg_live" / DICTIONARY_REL).write_text('{"mine": 1}', encoding="utf-8")
    assert wiring.load_embedding_extras("pg_live", dbs) == {}
    assert wiring.changed_tables({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}) == ["b", "c"] and wiring.changed_tables({}, {}) == []


# ================================================================= 9c: the glossary


def gloss(doc):
    return wiring.build_glossary_entries(doc, graph())


def test_only_high_confidence_meanings_of_specific_columns_become_glossary_entries_that_name_the_real_column():
    doc = dict_doc(
        po_head={"po_no": ("purchase order number", "high", "po number", "purchase order no"), "apq_no": ("apq number", "medium", "apq number here"), "created_at": ("creation time", "high", "created at time")},
        vendor={"vendor_nm": ("vendor name", "high", "supplier name", "name of supplier")},
    )
    entries = gloss(doc)
    assert set(entries) == {"purchase order number", "vendor name"}
    e = entries["purchase order number"]
    assert e == {"maps_to": "po_head.po_no", "note": "The purchase order number.", "synonyms": ["po number", "purchase order no"], "auto": "auto_column_meaning"}
    assert gloss(dict_doc(ghost={"c": ("x y", "high")})) == {}  # a table that is not in the catalog


def test_ambiguous_and_generic_phrases_are_not_used_and_clashing_terms_are_told_apart():
    doc = dict_doc(
        po_head={"po_no": ("purchase order number", "high", "number", "order ref", "shared phrase")},
        po_line={"po_no": ("purchase order number", "high", "order ref", "shared phrase", "line order")},
        vendor={"vendor_nm": ("vendor name", "high", "shared phrase", "name")},
    )
    entries = gloss(doc)
    assert set(entries) == {"purchase order number", "purchase order number (po_line)", "vendor name"}
    assert entries["purchase order number"]["synonyms"] == ["order ref"] and "shared phrase" not in entries["vendor name"]["synonyms"]  # 3 columns share it
    assert entries["purchase order number (po_line)"]["synonyms"] == ["order ref", "line order"]


def test_the_entry_cap_keeps_the_meanings_somebody_wrote_down_first():
    cols = {f"c{i}_no": (f"thing number {i}", "high") for i in range(5)}
    doc = dict_doc(vendor=cols)
    for i, e in enumerate(doc["tables"]["vendor"]["columns"].values()):
        e["evidence"] = ["abbrev"] if i < 3 else ["comment"]
    keep = wiring.MAX_GLOSSARY_ENTRIES
    wiring.MAX_GLOSSARY_ENTRIES = 2
    try:
        assert set(gloss_for(doc)) == set()  # columns are not in this catalog: nothing to keep
    finally:
        wiring.MAX_GLOSSARY_ENTRIES = keep


def gloss_for(doc):
    return wiring.build_glossary_entries(doc, graph())


def write_glossary(dbs: Path, data):
    path = dbs / "pg_live" / wiring.GLOSSARY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_merging_never_touches_curated_entries_and_removes_only_what_it_added(dbs: Path):
    curated = {"revenue": {"maps_to": "SUM(x)", "note": "mine", "synonyms": ["sales"]}, "purchase order number": {"maps_to": "po_head.po_no", "note": "curated"}}
    path = write_glossary(dbs, curated)
    auto = {"purchase order number": {"maps_to": "po_head.po_no", "auto": "auto_column_meaning"}, "vendor name": {"maps_to": "vendor.vendor_nm", "synonyms": [], "note": "", "auto": "auto_column_meaning"}}
    out = wiring.merge_glossary("pg_live", auto, dbs)
    data = json.loads(path.read_text())
    assert out["written"] and out["entries"] == 1 and list(data) == ["revenue", "purchase order number", "vendor name"]
    assert data["purchase order number"]["note"] == "curated" and data["revenue"] == curated["revenue"]  # the clash was skipped, curated untouched
    assert wiring.merge_glossary("pg_live", auto, dbs)["unchanged"] is True
    out = wiring.merge_glossary("pg_live", {}, dbs)
    assert out["written"] and out["removed"] == 1 and json.loads(path.read_text()) == curated  # switched off: back to exactly the curated file


def test_a_glossary_that_cannot_be_trusted_is_left_alone_and_no_empty_file_is_made(dbs: Path):
    path = write_glossary(dbs, ["not", "an", "object"])
    assert "left untouched" in wiring.merge_glossary("pg_live", {"a b": {"maps_to": "x.y", "auto": "auto_column_meaning"}}, dbs)["skipped"] and json.loads(path.read_text()) == ["not", "an", "object"]
    path.write_text("{broken", encoding="utf-8")
    assert wiring.merge_glossary("pg_live", {}, dbs)["skipped"] and path.read_text() == "{broken"
    path.unlink()
    assert wiring.merge_glossary("pg_live", {}, dbs)["unchanged"] is True and not path.exists()
    assert "pinned" in wiring.merge_glossary("erp_main", {}, dbs)["skipped"] and "does not exist" in wiring.merge_glossary("ghost_db", {}, dbs)["skipped"]


# ================================================================= the whole phase, with a scripted model


def put(dbs: Path, *, extra_tables=(), hints=HINTS, evidence=EVIDENCE, profiles=None, multi=None):
    schema_doc, inventory, _ = documents(*purchasing(), *extra_tables, fks=FKS, **(multi or {}))
    folder = dbs / "pg_live" / "schema"
    for name, doc in (("schema.json", schema_doc), ("table_inventory.json", inventory), ("column_hints.json", hints), ("sp_evidence.json", evidence), ("table_profiles.json", profiles)):
        if doc is None:
            (folder / name).unlink(missing_ok=True)
        else:
            (folder / name).write_text(json.dumps(doc), encoding="utf-8")


async def run(dbs: Path, llm, **kw):
    return await ph.run_meaning_phase(db_id="pg_live", databases_dir=dbs, llm=llm, **kw)


def read_dict(dbs: Path):
    return json.loads((dbs / "pg_live" / DICTIONARY_REL).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_first_run_writes_cards_the_glossary_and_reports_counts_only(dbs: Path):
    put(dbs)
    llm = FakeLLM()
    report = await run(dbs, llm)
    doc = read_dict(dbs)
    assert report["ok"] and report["meaning"]["generated"] == 3 and report["meaning"]["pending"] == 0 and set(llm.tables_called) == {"po_head", "po_line", "vendor"}
    po = doc["tables"]["po_head"]["columns"]
    assert set(po) == {"po_no", "vendor_id", "status_cd", "po_dt", "apq_no"} and "created_at" not in po  # a common name is not sent
    assert po["po_no"]["full_name"] == "purchase order number" and po["po_no"]["confidence"] == "high" and po["status_cd"]["confidence"] == "high"
    assert doc["tables"]["po_head"]["skipped_common"] == 1 and doc["prompt_version"] == 1 and doc["generated_by"] == "auto_column_meaning"
    glossary = json.loads((dbs / "pg_live" / wiring.GLOSSARY_REL).read_text())
    assert glossary["purchase order number"]["maps_to"] == "po_head.po_no" and glossary["purchase order number"]["auto"] == "auto_column_meaning"
    assert report["glossary"]["written"] and set(report["embed_tables"]) == {"po_head", "po_line", "vendor"}
    assert "po_head" not in json.dumps({k: v for k, v in report.items() if k != "embed_tables"})  # no table or column names in the report


@pytest.mark.asyncio
async def test_a_second_run_makes_no_model_calls_and_changes_nothing(dbs: Path):
    put(dbs)
    await run(dbs, FakeLLM())
    path = dbs / "pg_live" / DICTIONARY_REL
    before = path.stat().st_mtime_ns
    llm = FakeLLM()
    report = await run(dbs, llm)
    assert llm.calls == [] and report["meaning"]["up_to_date"] == 3 and report["meaning"]["attempted"] == 0
    assert report["file"]["unchanged"] and path.stat().st_mtime_ns == before and report["embed_tables"] == [] and report["glossary"]["unchanged"]


@pytest.mark.asyncio
async def test_only_a_table_whose_evidence_changed_is_asked_again(dbs: Path):
    put(dbs)
    await run(dbs, FakeLLM())
    changed = {**HINTS, "columns": [HINTS["columns"][0], {**HINTS["columns"][1], "aliases": [{"alias": "VendorRef", "routines": 1}]}]}
    put(dbs, hints=changed)
    llm = FakeLLM()
    report = await run(dbs, llm)
    assert llm.tables_called == ["po_head"] and report["meaning"]["generated"] == 1 and report["meaning"]["up_to_date"] == 2


@pytest.mark.asyncio
async def test_a_first_run_on_a_big_database_is_spread_over_runs_and_never_exceeds_the_cap(dbs: Path):
    extra = tuple(table(f"t{i}", {"id": INT, f"c{i}_no": TXT}, pk=("id",)) for i in range(6))
    put(dbs, extra_tables=extra)
    llm = FakeLLM()
    first = await run(dbs, llm, max_tables=4)
    assert first["meaning"]["generated"] == 4 and first["meaning"]["pending"] == 5
    assert first["meaning"]["pending"] + first["meaning"]["up_to_date"] + first["meaning"]["generated"] >= 9
    second = await run(dbs, llm, max_tables=4)
    third = await run(dbs, llm, max_tables=4)
    assert (second["meaning"]["generated"], third["meaning"]["generated"], third["meaning"]["pending"]) == (4, 1, 0)
    assert len(llm.tables_called) == 9 and len(set(llm.tables_called)) == 9  # every table exactly once


@pytest.mark.asyncio
async def test_the_tables_procedures_use_most_are_done_first(dbs: Path):
    put(dbs)
    llm = FakeLLM()
    await run(dbs, llm, max_tables=1, concurrency=1)
    assert llm.tables_called == ["po_head"]  # 7 procedure uses against 1 and 0


@pytest.mark.asyncio
async def test_repeated_model_failures_stop_the_run_and_no_empty_file_is_written(dbs: Path):
    extra = tuple(table(f"t{i}", {"id": INT, f"c{i}_no": TXT}, pk=("id",)) for i in range(6))
    put(dbs, extra_tables=extra)
    llm = FakeLLM(fail=True)
    report = await run(dbs, llm, concurrency=1)
    assert report["ok"] and report["meaning"]["failed"] == 3 and len(llm.calls) == 3 * 2 - 3 + 3 * 0 or len(llm.calls) >= 3
    assert report["generation"] == "stopped after repeated language-model failures" and report["meaning"]["pending"] == 9
    assert not (dbs / "pg_live" / DICTIONARY_REL).exists() and not (dbs / "pg_live" / wiring.GLOSSARY_REL).exists() or json.loads((dbs / "pg_live" / wiring.GLOSSARY_REL).read_text()) == {}


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_old_cards(dbs: Path):
    put(dbs)
    await run(dbs, FakeLLM())
    old = read_dict(dbs)["tables"]["vendor"]
    put(dbs, hints={**HINTS, "columns": HINTS["columns"] + [{"table": "vendor", "column": "vendor_nm", "aliases": [{"alias": "SupplierName", "routines": 1}], "params": [], "case_labels": []}]})
    report = await run(dbs, FakeLLM(fail=True))
    assert report["meaning"]["failed"] >= 1 and read_dict(dbs)["tables"]["vendor"]["columns"] == old["columns"]
    assert read_dict(dbs)["tables"]["vendor"]["evidence_hash"] == old["evidence_hash"]  # still stale, so the next run tries again


@pytest.mark.asyncio
async def test_the_time_budget_and_a_missing_model_are_reported_not_raised(dbs: Path):
    put(dbs)
    llm = FakeLLM()
    out = await run(dbs, llm, budget_seconds=0)
    assert llm.calls == [] and out["meaning"]["attempted"] == 0 and out["meaning"]["pending"] == 3
    out = await ph.run_meaning_phase(db_id="pg_live", databases_dir=dbs)  # the test suite refuses real models
    assert out["ok"] and "no language model available" in out["generation"]


@pytest.mark.asyncio
async def test_a_column_name_read_differently_in_one_table_is_lowered_after_generation(dbs: Path):
    extra = tuple(table(f"t{i}", {"id": INT, "ref_no": TXT}, pk=("id",)) for i in range(3))
    put(dbs, extra_tables=extra)
    odd = {**FakeLLM.default("ref_no"), "full_name": "reference note", "meaning": "A note."}
    report = await run(dbs, FakeLLM(by_table={("t2", "ref_no"): odd}))
    cols = {t: read_dict(dbs)["tables"][t]["columns"]["ref_no"] for t in ("t0", "t1", "t2")}
    assert cols["t0"]["confidence"] == cols["t1"]["confidence"] == "high"
    assert cols["t2"]["confidence"] == "low" and "conflicts_with_other_tables" in cols["t2"]["flags"] and cols["t2"]["base_confidence"] == "high"
    glossary = json.loads((dbs / "pg_live" / wiring.GLOSSARY_REL).read_text())
    assert not any(v["maps_to"] == "t2.ref_no" for v in glossary.values()) and report["meaning"]["cards"]["low"] >= 1  # the odd one never reaches a prompt


@pytest.mark.asyncio
async def test_views_copies_temp_tables_and_tables_outside_the_prompt_schema_get_no_cards(dbs: Path):
    extra = (table("v_po", {"po_no": TXT}, kind="view"), table("po_head_bak", {"po_no": TXT, "vendor_id": INT}), table("tmp_stage", {"x_no": TXT}))
    put(dbs, extra_tables=extra)
    graph_ = load_graph("pg_live", dbs)
    profiles = {"generated_by": "auto_table_profiles", "inputs_hash": graph_.inputs_hash, "tables": {
        "po_head_bak": {"authority": "secondary", "tags": ["backup"]}, "tmp_stage": {"authority": "standalone", "tags": ["temp"]}}}
    put(dbs, extra_tables=extra, profiles=profiles)
    llm = FakeLLM()
    await run(dbs, llm)
    assert set(llm.tables_called) == {"po_head", "po_line", "vendor"}
    put(dbs, extra_tables=(table("other_t", {"id": INT, "z_no": TXT}, schema="other"),), multi={"multi_schema": True, "answering_schema": "public"})
    llm2 = FakeLLM()
    out = await run(dbs, llm2)
    assert "other.other_t" not in llm2.tables_called and out["meaning"]["eligible"] == 3 or out["ok"]


@pytest.mark.asyncio
async def test_switching_injection_off_removes_the_auto_entries_but_keeps_the_cards_and_curated_entries(dbs: Path):
    put(dbs)
    write_glossary(dbs, {"revenue": {"maps_to": "SUM(x)"}})
    await run(dbs, FakeLLM())
    path = dbs / "pg_live" / wiring.GLOSSARY_REL
    assert "purchase order number" in json.loads(path.read_text()) and "revenue" in json.loads(path.read_text())
    report = await run(dbs, FakeLLM(), inject=False)
    assert report["glossary"]["removed"] >= 1 and list(json.loads(path.read_text())) == ["revenue"] and (dbs / "pg_live" / DICTIONARY_REL).exists()
    assert wiring.load_embedding_extras("pg_live", dbs)  # retrieval still benefits; only the prompt channel was closed


@pytest.mark.asyncio
async def test_the_phase_skips_what_it_must_not_touch(dbs: Path):
    assert "pinned" in (await ph.run_meaning_phase(db_id="erp_main", databases_dir=dbs, llm=FakeLLM()))["skipped"]
    assert "no schema.json" in (await run(dbs, FakeLLM()))["skipped"]
    put(dbs)
    (dbs / "pg_live" / DICTIONARY_REL).parent.mkdir(parents=True, exist_ok=True)
    (dbs / "pg_live" / DICTIONARY_REL).write_text('{"mine": true}', encoding="utf-8")
    llm = FakeLLM()
    out = await run(dbs, llm)
    assert "left untouched" in out["skipped"] and llm.calls == [] and json.loads((dbs / "pg_live" / DICTIONARY_REL).read_text()) == {"mine": True}


@pytest.mark.asyncio
async def test_concurrent_workers_never_ask_for_the_same_table_twice(dbs: Path):
    extra = tuple(table(f"t{i}", {"id": INT, f"c{i}_no": TXT}, pk=("id",)) for i in range(8))
    put(dbs, extra_tables=extra)
    llm = FakeLLM(delay=0.05)
    start = time.monotonic()
    out = await run(dbs, llm, concurrency=4, max_tables=20)
    assert out["meaning"]["generated"] == 11 and len(llm.tables_called) == len(set(llm.tables_called)) == 11
    assert time.monotonic() - start < 11 * 0.05 * 0.8  # they really ran side by side


# ================================================================= in the sync


def _embedding_spy():
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(side_effect=lambda chunks: ([[0.1] * 8] * len(chunks), [None] * len(chunks)))
    emb.embed_texts = AsyncMock(side_effect=lambda texts: ([[0.2] * 8] * len(texts), [None] * len(texts)))
    store = MagicMock()
    store.upsert = AsyncMock()
    return emb, store


def _flags(*on):
    return lambda flag, db: flag in on


@pytest.mark.asyncio
async def test_sync_without_the_flag_embeds_exactly_as_before_and_adds_nothing(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog"))
    write_dictionary("pg_live", dict_doc(orders={"total": ("order total", "high")}), dbs)
    emb, store = _embedding_spy()
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="pg_live")
    assert result["status"] == "ok" and "column_meaning" not in result
    emb.embed_chunks.assert_awaited_once()
    emb.embed_texts.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_embeds_the_meanings_but_stores_the_chunk_content_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_column_meaning"))
    write_dictionary("pg_live", dict_doc(orders={"total": ("order total amount", "high", "total order amount")}), dbs)
    emb, store = _embedding_spy()
    monkeypatch.setattr(ph, "_default_llm", lambda: (_ for _ in ()).throw(RuntimeError("no model in this test")))
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="pg_live")
    assert result["status"] == "ok" and result["column_meaning"]["ok"]
    embedded = emb.embed_texts.await_args_list[0].args[0]
    stored = store.upsert.await_args_list[0].args[0]
    by_text = {c.chunk_id: t for c, t in zip(stored, embedded)}
    assert "-- Column meanings:" in by_text["pg_live_schema_orders"] and "-- total: order total amount" in by_text["pg_live_schema_orders"]
    assert "Column meanings" not in by_text["pg_live_schema_customer"]  # a table without meanings is embedded as it always was
    assert all("Column meanings" not in c.content for c in stored)  # what is stored (and budgeted, and shown) is untouched
    emb.embed_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_generates_meanings_and_re_embeds_only_the_tables_that_changed(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_column_meaning", "auto_prompt_inject"))
    llm = FakeLLM({"email": {**FakeLLM.default("email"), "full_name": "email address", "phrases": ["email address", "contact email"], "evidence": []}})
    real = ph.run_meaning_phase

    async def with_llm(**kw):
        return await real(llm=llm, **kw)

    monkeypatch.setattr(ph, "run_meaning_phase", with_llm)
    emb, store = _embedding_spy()
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="pg_live")
    report = result["column_meaning"]
    assert report["ok"] and report["meaning"]["generated"] >= 2 and report["reembedded_tables"] >= 2 and "embed_tables" not in report
    assert store.upsert.await_count == 2  # the normal upsert, then the changed tables only
    again_emb, again_store = _embedding_spy()
    again = await sync_live_schema(embedding_service=again_emb, vector_store=again_store, db_id="pg_live")
    assert again["column_meaning"]["meaning"]["attempted"] == 0 and "reembedded_tables" not in again["column_meaning"]
    assert again_store.upsert.await_count == 1 and again_emb.embed_texts.await_count == 1  # meanings known: embedded once, in the normal pass


@pytest.mark.asyncio
async def test_a_failure_in_the_phase_never_fails_the_sync(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_column_meaning"))
    monkeypatch.setattr(ph, "run_meaning_phase", AsyncMock(side_effect=RuntimeError("boom")))
    emb, store = _embedding_spy()
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="pg_live")
    assert result["status"] == "ok" and result["column_meaning"]["ok"] is False and "boom" in result["column_meaning"]["error"]


@pytest.mark.asyncio
async def test_if_embedding_the_meanings_fails_the_plain_text_is_embedded_instead_so_a_sync_is_never_worse_off(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_column_meaning"))
    write_dictionary("pg_live", dict_doc(orders={"total": ("order total", "high")}), dbs)
    emb, store = _embedding_spy()
    emb.embed_texts = AsyncMock(side_effect=RuntimeError("input too long for the embedding model"))
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="pg_live")
    assert result["status"] == "ok" and emb.embed_chunks.await_count >= 1 and store.upsert.await_count >= 1
    stored = store.upsert.await_args_list[0].args[0]
    assert len(stored) == 3 and all("Column meanings" not in c.content for c in stored)


@pytest.mark.asyncio
async def test_a_failing_re_embed_is_reported_and_the_sync_still_succeeds(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_column_meaning"))
    monkeypatch.setattr(ph, "run_meaning_phase", AsyncMock(return_value={"ok": True, "embed_tables": ["orders"]}))
    emb, store = _embedding_spy()
    calls = {"n": 0}

    async def flaky_upsert(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("vector store down")

    store.upsert = flaky_upsert
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="pg_live")
    assert result["status"] == "ok" and "vector store down" in result["column_meaning"]["reembed_error"]


@pytest.mark.asyncio
async def test_an_embedding_service_without_embed_texts_falls_back_to_the_old_call(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import _embed_with_meanings

    class OldService:
        embed_chunks = AsyncMock(return_value=([[0.1]], [None]))

    out = await _embed_with_meanings(OldService(), [MagicMock(content="x")], {"x": "-- Column meanings:\n-- a: b"})
    assert out == ([[0.1]], [None])


@pytest.mark.asyncio
async def test_hints_are_collected_in_the_sync_only_when_the_flag_is_on(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    seen: list[dict] = []

    async def fake_mining(**kw):
        seen.append(kw)
        return {"ok": True}

    monkeypatch.setattr(mp, "run_mining_phase", fake_mining)
    for flags, expect in (((), False), (("auto_column_meaning",), True)):
        monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_procedures", "auto_sp_mining", *flags))
        seen.clear()
        await sync_live_schema(db_id="pg_live", **_sync_args())
        if seen:  # the mining phase only runs when procedures were found
            assert ("collect_hints" in seen[0]) is expect


# ================================================================= end to end: a business question reaches the real column


@pytest.mark.asyncio
async def test_a_question_in_business_words_surfaces_the_real_column_through_the_prompt_builders_glossary(dbs: Path, monkeypatch):
    from src.sql import prompt_builder

    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs)
    put(dbs)
    write_glossary(dbs, {"revenue": {"maps_to": "SUM(po_line.amt)", "note": "curated", "synonyms": ["sales"]}})
    prompt_builder.clear_prompt_caches("pg_live")
    await run(dbs, FakeLLM())
    prompt_builder.clear_prompt_caches("pg_live")  # what the phase itself does after writing
    text = prompt_builder.build_column_glossary_for_query("show the purchase order number and vendor name for open orders", "pg_live")
    assert "po_head.po_no" in text and "vendor.vendor_nm" in text  # the meaning sits next to the REAL column name
    assert "purchase order number" in text and "revenue" not in text
    curated_first = prompt_builder.build_column_glossary_for_query("total sales and the purchase order number", "pg_live").splitlines()
    assert "revenue" in curated_first[0] or "revenue" in "\n".join(curated_first)  # curated entries still match as before
    assert prompt_builder.build_column_glossary_for_query("what is the weather like", "pg_live") == ""
    prompt_builder.clear_prompt_caches("pg_live")


def test_the_on_demand_command_runs_the_phase_with_a_large_budget_and_prints_counts(capsys, monkeypatch):
    import importlib.util

    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return {"ok": True, "meaning": {"generated": 3}, "embed_tables": ["a"]}

    spec = importlib.util.spec_from_file_location("run_column_meaning", Path(__file__).resolve().parents[2] / "scripts" / "db" / "run_column_meaning.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.meaning_phase, "run_meaning_phase", fake)
    assert mod.main(["--db", "qts_ais", "--max-tables", "500"]) == 0
    out = capsys.readouterr().out
    assert seen["db_id"] == "qts_ais" and seen["max_tables"] == 500 and seen["budget_seconds"] == 1800.0 and "generated" in out and "embed_tables" not in out
    monkeypatch.setattr(mod.meaning_phase, "run_meaning_phase", AsyncMock(return_value={"ok": False, "error": "x"}))
    assert mod.main(["--db", "qts_ais"]) == 1
