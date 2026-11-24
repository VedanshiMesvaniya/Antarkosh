"""Release 1 retrieval help: extra query words and column-level table matches (``src/sql/retrieval_terms.py``).

Nothing here names a real database, table or question: the catalogs are small synthetic ones written to a temporary folder.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import src.sql.prompt_builder as pb
import src.sql.retrieval_terms as rt
import src.sql.schema_retrieval as sr
from src.sql import registry
from src.sql.relationships.model import load_graph
from src.sql.semantics.dictionary import GENERATED_BY
from tests.sql.relationships_fixtures import documents, table

DB = "terms_db"
OTHER = "other_db"
ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------------------------------- file helpers


def _catalog(root: Path, db: str, *tables: dict[str, Any]) -> None:
    schema_doc, inventory, _ = documents(*tables)
    folder = root / db / "schema"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(json.dumps(schema_doc), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")


def _semantics(root: Path, db: str, name: str, doc: dict[str, Any]) -> None:
    folder = root / db / "semantics"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(doc), encoding="utf-8")


def _stock_world() -> tuple[dict[str, Any], ...]:
    """Eight tables use the word "product"; "material" lives in three; "plant" in one."""
    products = [table(f"p{i}", {"id": "int", "product_ref": "int", "qty": "int"}, pk=("id",)) for i in range(8)]
    return (
        *products,
        table("stock_ledger", {"id": "int", "mat_code": ("varchar", "material code of the stock line"), "qty": "int"}, pk=("id",)),
        table("issue_slip", {"id": "int", "material_no": "varchar", "issued": "int"}, pk=("id",)),
        table("receipt", {"id": "int", "material_ref": "int", "received": "int"}, pk=("id",)),
        table("site_master", {"id": "int", "plant_name": "varchar", "city": "varchar"}, pk=("id",)),
        table("misc", {"id": "int", "created_on": "datetime"}, pk=("id",)),
    )


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    rt.clear_retrieval_terms_cache()
    yield tmp_path
    rt.clear_retrieval_terms_cache()


# ----------------------------------------------------------------------------------------------- 1. query terms


def test_generic_aliases_add_words_and_never_replace_the_question(dbs: Path) -> None:
    # the universal ERP glossary knows "item": strong aliases always; "material" only where the database uses that word (see below)
    assert rt.expand_query_terms("Show each item with its cost", DB) == ["product", "good", "article"]
    assert rt.expand_query_terms("which facility is busiest", DB) == ["plant", "site"]
    assert rt.expand_query_terms("total purchase value by month", DB) == ["purchase amount"]
    assert rt.expand_query_terms("how many orders last week", DB) == []


def test_alias_phrase_needs_its_words_next_to_each_other_and_skips_words_already_asked(dbs: Path) -> None:
    assert rt.expand_query_terms("value of the purchase", DB) == []
    assert rt.expand_query_terms("item product list", DB) == ["good", "article"]  # "product" is already in the question


def test_database_glossary_adds_the_concept_a_synonym_stands_for(dbs: Path) -> None:
    _semantics(dbs, DB, "glossary.json", {"vendor": ["supplier", "seller"], "invoice": "bill, tax bill", "empty": []})
    assert rt.expand_query_terms("list every supplier", DB) == ["vendor"]
    assert rt.expand_query_terms("open tax bill count", DB) == ["invoice"]
    assert rt.expand_query_terms("list every vendor", DB) == []  # the schema word is already there
    assert rt.expand_query_terms("list every supplier", OTHER) == []  # another database has its own glossary (here: none)


def test_unreadable_glossary_and_pinned_database_give_nothing(dbs: Path) -> None:
    folder = dbs / DB / "semantics"
    folder.mkdir(parents=True)
    (folder / "glossary.json").write_text("{not json", encoding="utf-8")
    assert rt.expand_query_terms("supplier item", DB) == ["product", "good", "article"]  # the universal glossary still works
    assert rt.expand_query_terms("show item", "erp_main") == []
    assert rt.expand_query_terms("", DB) == [] and rt.expand_query_terms(None, DB) == []  # type: ignore[arg-type]
    assert rt.expand_query_terms("show item", "../escape") == []  # an invalid id has no files


def test_extra_terms_are_capped(dbs: Path) -> None:
    _semantics(dbs, DB, "glossary.json", {f"concept{i}": ["thing"] for i in range(20)})
    assert len(rt.expand_query_terms("show thing", DB)) == rt.MAX_EXTRA_TERMS


# ----------------------------------------------------------------------------------------------- 2. column records


def _find(query: str, db: str = DB, **kw: Any) -> list[str]:
    extra = rt.expand_query_terms(query, db)
    kw.setdefault("available", {t for t in ("stock_ledger", "issue_slip", "receipt", "site_master", "misc", *[f"p{i}" for i in range(8)])})
    return [h.table for h in rt.find_column_tables(query, extra, db, **kw)]


def test_item_finds_material_columns_although_no_column_is_called_item(dbs: Path) -> None:
    _catalog(dbs, DB, *_stock_world())
    found = _find("show the balance of each item")
    assert set(found) == {"stock_ledger", "issue_slip", "receipt"}  # "material" is used by three tables
    assert not any(t.startswith("p") for t in found)  # "product" is used by eight: it says nothing about where to look


def test_a_word_used_by_many_tables_alone_finds_nothing(dbs: Path) -> None:
    _catalog(dbs, DB, *_stock_world())
    assert [h.table for h in rt.find_column_tables("show product details", [], DB, available={f"p{i}" for i in range(8)} | {"stock_ledger"})] == []


def test_catalog_comments_and_generated_meanings_count_as_descriptions(dbs: Path) -> None:
    _catalog(dbs, DB, table("ledger", {"id": "int", "mc": "varchar"}, pk=("id",)), table("misc", {"id": "int", "x": "int"}, pk=("id",)))
    _semantics(dbs, DB, "column_dictionary.json", {
        "generated_by": GENERATED_BY, "tables": {"ledger": {"columns": {"mc": {"full_name": "warehouse bin location", "phrases": ["storage rack"]}}}},
    })
    assert _find("which storage rack holds it", available={"ledger", "misc"}) == ["ledger"]
    # a dictionary that is not the generated one is ignored
    _semantics(dbs, DB, "column_dictionary.json", {"generated_by": "someone", "tables": {"ledger": {"columns": {"mc": {"full_name": "warehouse bin"}}}}})
    assert _find("which warehouse bin holds it", available={"ledger", "misc"}) == []


def test_column_glossary_synonyms_count(dbs: Path) -> None:
    _catalog(dbs, DB, table("ledger", {"id": "int", "mc": "varchar"}, pk=("id",)), table("misc", {"id": "int", "x": "int"}, pk=("id",)))
    _semantics(dbs, DB, "column_glossary.json", {"bin location": {"maps_to": "ledger.mc", "synonyms": ["rack"]}, "bad": {"maps_to": "x"}, "odd": 3})
    assert _find("which rack is it on", available={"ledger", "misc"}) == ["ledger"]


def test_only_real_tables_that_can_be_shown_and_are_not_already_chosen(dbs: Path) -> None:
    _catalog(
        dbs, DB,
        table("stock_ledger", {"id": "int", "mat_code": ("varchar", "material code")}, pk=("id",)),
        table("stock_view", {"id": "int", "mat_code": ("varchar", "material code")}, kind="view"),
        table("receipt", {"id": "int", "material_ref": "int"}, pk=("id",)),
        table("issue_slip", {"id": "int", "material_no": "varchar"}, pk=("id",)),
    )
    everything = {"stock_ledger", "stock_view", "receipt", "issue_slip"}
    assert "stock_view" not in _find("each item", available=everything)  # views are never suggested
    assert "issue_slip" not in _find("each item", available={"stock_ledger", "receipt"})  # not available to the prompt
    assert "receipt" not in _find("each item", available=everything, exclude={"receipt"})  # already a candidate
    assert _find("each item", available=everything, limit=1) != []
    assert len(_find("each item", available=everything, limit=1)) == 1
    assert _find("each item", available=set()) == []


def test_test_backup_and_copy_tables_are_not_suggested(dbs: Path) -> None:
    _catalog(
        dbs, DB,
        table("stock", {"id": "int", "material_no": "varchar"}, pk=("id",), rows=1000),
        table("stock_bak", {"id": "int", "material_no": "varchar"}, pk=("id",), rows=10),
    )
    assert "stock_bak" in _find("each item", available={"stock", "stock_bak"})  # without a profile file nothing says it is a copy
    graph = load_graph(DB)
    assert graph is not None
    profiles = {"generated_by": "auto_table_profiles", "inputs_hash": graph.inputs_hash, "tables": {"stock_bak": {"tags": ["backup"], "authority": "secondary"}}}
    (dbs / DB / "schema" / "table_profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
    assert _find("each item", available={"stock", "stock_bak"}) == ["stock"]
    stale = {**profiles, "inputs_hash": "old"}  # a profile file written for an older catalog is not trusted
    (dbs / DB / "schema" / "table_profiles.json").write_text(json.dumps(stale), encoding="utf-8")
    assert "stock_bak" in _find("each item", available={"stock", "stock_bak"})


def test_a_bare_name_shared_by_two_schemas_cannot_be_looked_up_and_is_skipped(dbs: Path) -> None:
    _catalog(
        dbs, DB,
        table("stock", {"id": "int", "material_no": "varchar"}, schema="a"),
        table("stock", {"id": "int", "material_no": "varchar"}, schema="b"),
        table("receipt", {"id": "int", "material_ref": "int"}, schema="a"),
    )
    assert _find("each item", available={"stock", "receipt"}) == ["receipt"]


def test_no_catalog_or_a_broken_one_means_no_help_and_no_error(dbs: Path) -> None:
    assert _find("each item") == []
    folder = dbs / DB / "schema"
    folder.mkdir(parents=True)
    (folder / "schema.json").write_text("{not json", encoding="utf-8")
    assert _find("each item") == []
    assert _find("each item", db="erp_main") == []


def test_one_database_never_sees_another_databases_tables_or_words(dbs: Path) -> None:
    _catalog(dbs, DB, *_stock_world())
    _catalog(dbs, OTHER, table("vendor_master", {"id": "int", "material_grade": "varchar"}, pk=("id",)))
    _semantics(dbs, DB, "glossary.json", {"warehouse": ["godown"]})
    mine = _find("each item", db=DB)
    theirs = _find("each item", db=OTHER, available={"vendor_master"})
    assert "vendor_master" not in mine and theirs == ["vendor_master"]
    assert not set(mine) & {"vendor_master"}
    assert rt.expand_query_terms("godown stock", OTHER) == []  # DB's glossary does not apply to OTHER


def test_the_index_follows_the_files(dbs: Path) -> None:
    _catalog(dbs, DB, table("ledger", {"id": "int", "material_no": "varchar"}, pk=("id",)))
    assert _find("each item", available={"ledger", "slip"}) == ["ledger"]
    _catalog(dbs, DB, table("ledger", {"id": "int", "qty": "int"}, pk=("id",)), table("slip", {"id": "int", "material_ref": "int", "note": "varchar"}, pk=("id",)))
    assert _find("each item", available={"ledger", "slip"}) == ["slip"]  # rebuilt because schema.json changed


# ----------------------------------------------------------------------------------------------- 3. retrieval wiring


def _ddl(name: str, *cols: str) -> str:
    return f"TABLE {name} (\n" + ",\n".join(f"  {c} varchar" for c in ("id", *cols)) + "\n)"


class _Emb:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def embed_query(self, q: str) -> tuple[list[float], dict]:
        self.seen.append(q)
        return [0.0], {}


def _store(tables: dict[str, str], hits: list[str], seen: list[str]) -> Any:
    class _Chunk:
        def __init__(self, t: str) -> None:
            self.content, self.document_id, self.db_id, self.metadata = tables[t], f"schema:{DB}", DB, {"db_id": DB}

    class _Hit:
        def __init__(self, t: str) -> None:
            self.chunk = _Chunk(t)

    class _Store:
        async def search_hybrid(self, **kw: Any) -> list[Any]:
            seen.append(kw["query_text"])
            return [_Hit(t) for t in hits]

    return _Store()


def _setup(monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool]) -> tuple[dict[str, str], str]:
    tables = {
        "orders": _ddl("orders", "customer_ref", "order_date"),
        "stock_ledger": _ddl("stock_ledger", "mat_code"),
        "issue_slip": _ddl("issue_slip", "material_no"),
        "misc": _ddl("misc", "created_on"),
    }
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")
    monkeypatch.setattr(sr, "route_anchor_tables", lambda q, db_id=DB: set())
    monkeypatch.setattr(sr, "is_feature_enabled", lambda f: True)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: flags.get(f, False))
    return tables, "\n\n".join(tables.values())


def _retrieve(query: str, tables: dict[str, str], full: str) -> tuple[str, list[str], list[str]]:
    emb, searched = _Emb(), []
    out = asyncio.run(sr.retrieve_schema_from_qdrant(query, full, _store(tables, ["orders"], searched), emb, db_id=DB))
    return out, emb.seen, searched


def _present(out: str) -> set[str]:
    return set(re.findall(r"(?im)^(?:table\s+)?(\w+)\s*(?:\(|:)", out))


def test_flag_off_is_exactly_the_old_behaviour(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _catalog(dbs, DB, table("orders", {"id": "int", "customer_ref": "int"}, pk=("id",)),
             table("stock_ledger", {"id": "int", "mat_code": ("varchar", "material code")}, pk=("id",)))
    tables, full = _setup(monkeypatch, {})
    off, embedded, searched = _retrieve("show each item", tables, full)
    assert embedded == searched == ["show each item"]  # the question, nothing added
    assert _present(off) == {"orders"}


def test_flag_on_adds_search_words_and_the_tables_whose_columns_match(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _catalog(dbs, DB, table("orders", {"id": "int", "customer_ref": "int"}, pk=("id",)),
             table("stock_ledger", {"id": "int", "mat_code": ("varchar", "material code")}, pk=("id",)),
             table("issue_slip", {"id": "int", "material_no": "varchar"}, pk=("id",)))
    tables, full = _setup(monkeypatch, {"auto_retrieval_apply": True})
    on, embedded, searched = _retrieve("show each item", tables, full)
    # the question first, extra words after it; "material" comes before the other aliases because this database uses that word
    assert embedded == searched == ["show each item product material good article"]
    assert {"orders", "stock_ledger", "issue_slip"} <= _present(on)
    assert "misc" not in _present(on)
    assert on.index("orders") < on.index("stock_ledger")  # vector tables keep their place ahead of column matches


def test_flag_on_changes_nothing_when_no_word_applies(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _catalog(dbs, DB, table("orders", {"id": "int", "customer_ref": "int"}, pk=("id",)),
             table("stock_ledger", {"id": "int", "mat_code": ("varchar", "material code")}, pk=("id",)))
    tables, full = _setup(monkeypatch, {})
    off, *_ = _retrieve("how many orders last week", tables, full)
    _setup(monkeypatch, {"auto_retrieval_apply": True})
    on, embedded, searched = _retrieve("how many orders last week", tables, full)
    assert on == off and embedded == searched == ["how many orders last week"]


def test_flag_on_without_catalog_files_is_the_old_behaviour(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tables, full = _setup(monkeypatch, {})
    off, *_ = _retrieve("how many orders", tables, full)
    _setup(monkeypatch, {"auto_retrieval_apply": True})
    on, *_ = _retrieve("how many orders", tables, full)
    assert on == off


def test_the_token_budget_still_bounds_what_column_matches_add(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wide = {f"m{i}": table(f"m{i}", {"id": "int", f"material_c{i}": "varchar"}, pk=("id",)) for i in range(6)}
    _catalog(dbs, DB, table("orders", {"id": "int"}, pk=("id",)), *wide.values())
    tables, _ = _setup(monkeypatch, {"auto_retrieval_apply": True})
    tables = {"orders": tables["orders"], **{n: _ddl(n, f"material_c{n[1:]}") for n in wide}}
    out, *_ = _retrieve("each item", tables, "\n\n".join(tables.values()))
    assert len(_present(out) - {"orders"}) <= rt.MAX_COLUMN_TABLES


def test_pinned_database_is_never_changed_even_with_every_flag(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.utils import feature_flags as ff

    monkeypatch.undo()  # use the real flag reader for this check
    assert ff.is_db_feature_enabled("auto_retrieval_apply", "erp_main") is False


# ----------------------------------------------------------------------------------------------- 4. the flag itself


def test_flag_is_registered_off_by_default_and_documented() -> None:
    from src.utils.feature_flags import DB_FEATURE_FLAGS, DEFAULT_NEW_DB_FLAGS, OFF_BY_DEFAULT_FLAGS

    assert "auto_retrieval_apply" in DB_FEATURE_FLAGS and "auto_retrieval_apply" in OFF_BY_DEFAULT_FLAGS
    assert DEFAULT_NEW_DB_FLAGS["auto_retrieval_apply"] is False
    cfg = yaml.safe_load((ROOT / "config" / "feature_flags.example.yaml").read_text(encoding="utf-8"))
    assert cfg["db_defaults"]["auto_retrieval_apply"] is False
    assert cfg["db_features"]["_example_db"]["auto_retrieval_apply"] is False


# ----------------------------------------------------------------------------------------------- 5. keyword hygiene, evidence tiers, countries
#
# General rules only: every table, column and phrase below is invented for the test and no rule depends on any one database.


def _card(full_name: str, phrases: list[str], confidence: str = "high") -> dict[str, Any]:
    return {"full_name": full_name, "phrases": phrases, "confidence": confidence}


def _dictionary(root: Path, tables: dict[str, dict[str, dict[str, Any]]]) -> None:
    _semantics(root, DB, "column_dictionary.json", {"generated_by": GENERATED_BY, "tables": {t: {"columns": cols} for t, cols in tables.items()}})


def _scrap_world(root: Path) -> set[str]:
    _catalog(
        root, DB,
        table("nc_scrap", {"id": "int", "qty_scrap": "int"}, pk=("id",)),
        table("commodity_master", {"id": "int", "commodity_name": "varchar"}, pk=("id",)),
        table("pr_main", {"id": "int", "disputed": "varchar"}, pk=("id",)),
        table("misc", {"id": "int", "created_on": "datetime"}, pk=("id",)),
    )
    # the generated phrase is a sentence: it used to put the word "there" in the index of pr_main
    _dictionary(root, {"pr_main": {"disputed": _card("dispute flag", ["is there a dispute", "did containment break"])}})
    return {"nc_scrap", "commodity_master", "pr_main", "misc"}


def test_filler_words_are_stop_words_and_never_select_a_table(dbs: Path) -> None:
    for word in ("there", "here", "please", "me", "us", "it", "its", "do", "does", "did", "be", "been", "being", "can", "could", "would", "should"):
        assert word in rt._STOP or rt._useful(word) == set(), word
    assert rt._useful("how much scrap is there per commodity, please") == {"scrap", "commodity"}
    available = _scrap_world(dbs)
    found = _find("How much scrap is there per commodity?", available=available)
    assert set(found) == {"nc_scrap", "commodity_master"}
    assert "pr_main" not in found  # "there" no longer reaches the dispute column
    assert _find("is there anything here, please", available=available) == []  # a question that is all filler finds nothing by itself
    assert _find("is there a dispute", available=available) == ["pr_main"]  # ... while the real word in it still does


def test_a_generated_sentence_is_never_indexed_but_the_label_and_real_phrases_are(dbs: Path) -> None:
    available = _scrap_world(dbs)
    idx = rt.load_column_index(DB)
    assert idx is not None
    words = {w for _t, _c, ws in idx.columns for w in ws}
    assert {"dispute", "flag", "scrap"} <= words  # the full_name label "dispute flag" is indexed
    assert not words & {"there", "containment", "break"}  # the two sentences are not
    assert _find("which dispute is open", available=available) == ["pr_main"]
    assert rt._sentence_like("is there a dispute") and rt._sentence_like("did containment break") and rt._sentence_like("does this record repeat")
    assert not rt._sentence_like("quantity scrapped") and not rt._sentence_like("list price") and not rt._sentence_like("date of birth")


def test_exact_names_outrank_generated_meanings_and_sentences_score_nothing(dbs: Path) -> None:
    _catalog(
        dbs, DB,
        table("gen_supplier_master", {"id": "int", "supplier_name": "varchar"}, pk=("id",)),
        table("po_main", {"id": "int", "vendor_ref": "int", "supplier_name": "varchar"}, pk=("id",)),
        table("quality_log", {"id": "int", "x1": "varchar", "x2": "varchar"}, pk=("id",)),
        table("sentence_log", {"id": "int", "y1": "varchar"}, pk=("id",)),
        table("misc", {"id": "int", "created_on": "datetime"}, pk=("id",)),
    )
    _dictionary(dbs, {
        "quality_log": {"x1": _card("supplier complaint", [], "medium")},
        "sentence_log": {"y1": _card("record flag", ["is it a supplier problem"], "high")},
    })
    ev = rt.question_evidence("show every supplier", [], DB, ["gen_supplier_master", "po_main", "quality_log", "sentence_log", "misc"])
    assert ev["gen_supplier_master"].score == rt.EVIDENCE_TABLE_NAME and ev["gen_supplier_master"].strong_table
    assert ev["po_main"].score == rt.EVIDENCE_COLUMN_NAME and not ev["po_main"].strong_table
    assert ev["quality_log"].score == rt.EVIDENCE_MEANING  # a medium-confidence generated label: used, but weakest
    assert ev["gen_supplier_master"].score > ev["po_main"].score > ev["quality_log"].score > 0
    assert "sentence_log" not in ev and "misc" not in ev  # a generic LLM sentence adds no score at all


def test_a_word_many_tables_use_is_no_evidence_for_a_column_but_a_table_name_always_counts(dbs: Path) -> None:
    tables = [table(f"t{i}", {"id": "int", "supplier_id": "int"}, pk=("id",)) for i in range(8)]
    _catalog(dbs, DB, table("gen_supplier_master", {"id": "int", "supplier_name": "varchar"}, pk=("id",)), *tables)
    ev = rt.question_evidence("every supplier", [], DB, ["gen_supplier_master", "t0", "t1"])
    assert set(ev) == {"gen_supplier_master"}  # supplier_id sits in eight tables: it points at none of them


def test_medium_meanings_help_retrieval_locally_but_never_reach_the_prompt_glossary(dbs: Path) -> None:
    from src.sql.semantics.wiring import build_glossary_entries

    _catalog(
        dbs, DB,
        table("scrap_log", {"id": "int", "q1": "int"}, pk=("id",)),
        table("rack_log", {"id": "int", "r1": "varchar"}, pk=("id",)),
        table("bin_log", {"id": "int", "b1": "varchar"}, pk=("id",)),
        table("misc", {"id": "int", "created_on": "datetime"}, pk=("id",)),
    )
    cards = {
        "scrap_log": {"q1": _card("scrap quantity", ["quantity scrapped"], "medium")},
        "rack_log": {"r1": _card("storage rack", ["rack position"], "high")},
        "bin_log": {"b1": _card("pallet bin", ["pallet slot"], "low")},
    }
    _dictionary(dbs, cards)
    available = {"scrap_log", "rack_log", "bin_log", "misc"}
    assert _find("how much was scrapped", available=available) == ["scrap_log"]  # medium: used to find the table
    assert _find("which storage rack", available=available) == ["rack_log"]  # high: used
    assert _find("which pallet slot", available=available) == []  # low: left to semantic_review.json
    graph = load_graph(DB)
    assert graph is not None
    doc = {"generated_by": GENERATED_BY, "tables": {t: {"columns": c} for t, c in cards.items()}}
    mapped = {e["maps_to"] for e in build_glossary_entries(doc, graph).values()}
    assert mapped == {"rack_log.r1"}  # only the high-confidence meaning can ever be shown to the model


def test_a_card_without_a_confidence_is_still_used_as_before(dbs: Path) -> None:
    _catalog(dbs, DB, table("ledger", {"id": "int", "mc": "varchar"}, pk=("id",)), table("misc", {"id": "int", "x": "int"}, pk=("id",)))
    _semantics(dbs, DB, "column_dictionary.json", {"generated_by": GENERATED_BY, "tables": {"ledger": {"columns": {"mc": {"full_name": "warehouse bin"}}}}})
    assert _find("which warehouse bin", available={"ledger", "misc"}) == ["ledger"]


# ---- countries


def test_country_names_are_detected_from_a_small_global_reference_and_ordinary_words_are_not() -> None:
    assert rt.detect_countries("suppliers from Germany") == ["Germany"]
    assert rt.detect_countries("orders shipped to the UK and the united states") == ["United Kingdom", "United States"]
    assert rt.detect_countries("Cote d'Ivoire sales") == ["Cote d'Ivoire"]
    for plain in ("how many orders are there in total", "show us it", "who is in charge", "orders by Jordan", ""):
        assert rt.detect_countries(plain) == []
    assert rt.detect_countries(None) == []  # type: ignore[arg-type]


def test_the_country_reference_is_only_standard_names_and_cannot_hold_an_ordinary_word() -> None:
    doc = json.loads((ROOT / "config" / "iso_country_terms.json").read_text(encoding="utf-8"))
    names = [c["name"] for c in doc["countries"]]
    assert len(names) == len(set(names)) > 150 and "Germany" in names and "India" in names
    for entry in doc["countries"]:
        assert set(entry) <= {"name", "aliases"}  # names and aliases only: no client data, no values, no tables
        for label in (entry["name"], *entry.get("aliases", [])):
            assert label.isascii() and not set(rt._words(label)) <= rt._STOP, label  # "us", "in", "it" can never be a country


def test_a_bad_country_file_means_no_detection_and_no_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rt, "PROJECT_ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    rt.clear_retrieval_terms_cache()
    assert rt.detect_countries("suppliers from Germany") == []  # missing file
    (tmp_path / "config" / "iso_country_terms.json").write_text("{not json", encoding="utf-8")
    rt.clear_retrieval_terms_cache()
    assert rt.detect_countries("suppliers from Germany") == []
    (tmp_path / "config" / "iso_country_terms.json").write_text(json.dumps({"countries": [{"name": "Us"}, {"name": "Peru", "aliases": ["in"]}, 5]}), encoding="utf-8")
    rt.clear_retrieval_terms_cache()
    assert rt.detect_countries("orders for us in Peru") == ["Peru"]  # "us" and "in" are filler and are skipped
    rt.clear_retrieval_terms_cache()


def test_a_named_country_marks_tables_that_hold_a_country_or_a_place_and_never_adds_words(dbs: Path) -> None:
    _catalog(
        dbs, DB,
        table("gen_supplier_master", {"id": "int", "supplier_name": "varchar"}, pk=("id",)),
        table("sup_address", {"id": "int", "supplier_id": "int", "country_name": "varchar"}, pk=("id",)),
        table("plant_site", {"id": "int", "site_location": "varchar"}, pk=("id",)),
        table("misc", {"id": "int", "created_on": "datetime"}, pk=("id",)),
    )
    names = ["gen_supplier_master", "sup_address", "plant_site", "misc"]
    ev = rt.question_evidence("suppliers from Germany", [], DB, names)
    assert ev["sup_address"].place == 0 and ev["plant_site"].place == 1 and ev["plant_site"].score == 0
    assert "misc" not in ev and ev["gen_supplier_master"].strong_table
    assert rt.place_tables(DB, names) == {"sup_address": 0, "plant_site": 1}
    assert all(e.place is None for e in rt.question_evidence("suppliers from nowhere", [], DB, names).values())  # no country named: no marks
    assert rt.expand_query_terms("suppliers from Germany", DB) == []  # the search text never gains a country word
    assert not any("german" in r for e in ev.values() for r in e.reasons)  # the country name itself is not evidence for any table


def test_evidence_is_empty_without_a_catalog_and_never_raises(dbs: Path) -> None:
    assert rt.question_evidence("every supplier", [], DB, ["a"]) == {}
    assert rt.question_evidence("every supplier", [], "erp_main", ["a"]) == {}
    assert rt.place_tables(DB, ["a"]) == {}
