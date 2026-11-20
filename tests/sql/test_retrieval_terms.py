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
    assert rt.expand_query_terms("Show each item with its cost", DB) == ["product", "material"]
    assert rt.expand_query_terms("which facility is busiest", DB) == ["plant", "site"]
    assert rt.expand_query_terms("total purchase value by month", DB) == ["purchase amount"]
    assert rt.expand_query_terms("how many orders last week", DB) == []


def test_alias_phrase_needs_its_words_next_to_each_other_and_skips_words_already_asked(dbs: Path) -> None:
    assert rt.expand_query_terms("value of the purchase", DB) == []
    assert rt.expand_query_terms("item product list", DB) == ["material"]  # "product" is already in the question


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
    assert rt.expand_query_terms("supplier item", DB) == ["product", "material"]  # the alias list still works
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
    assert _find("show product details") == []


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
    assert embedded == searched == ["show each item product material"]  # the question first, extra words after it
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
