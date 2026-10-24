"""Phase 7: table authority and profiles (twin tables, test/backup tags, ``table_profiles.json``)."""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml

from src.sql.profiles import authority as au
from src.sql.profiles.authority import (
    MARKERS,
    TAG_OF,
    build_profiles,
    check_complete,
    fresh_inferred_edges,
    marker_tags,
    summary,
)
from src.sql.profiles.writer import GENERATED_BY, PROFILES_REL, input_key, write_profiles_file
from src.sql.relationships.context import COPY_MARKERS, prepare, twin_key
from src.sql.relationships.model import load_graph
from src.sql.sa import profiles_phase as pp
from tests.sql.relationships_fixtures import build, documents, table
from tests.sql.test_auto_procedures import _sync_args, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_catalog_extraction import ROOT, dbs  # noqa: F401  (shared fixture)

INT, TXT = "INTEGER", "VARCHAR(50)"
PRODUCT_COLS = {"id": INT, "name": TXT, "price": INT, "sku": TXT}


def profiles_of(*tables, fks=(), proc=(), evidence=None, inferred=None, **kw):
    graph = build(*tables, fks=fks, proc=proc, **kw)
    return graph, build_profiles(prepare(graph), evidence_doc=evidence, inferred_doc=inferred)


def auth(result, name):
    return result.profiles[name].authority


# ================================================================= twins and authority


def test_a_test_copy_loses_to_the_real_table_and_is_tagged():
    graph, r = profiles_of(
        table("product", PRODUCT_COLS, pk=("id",), rows=12000),
        table("product_test", PRODUCT_COLS, pk=("id",), rows=5),
        table("order_item", {"id": INT, "product_id": INT}, pk=("id",)),
        fks=(("order_item", "product_id", "product", "id"),),
    )
    assert auth(r, "product") == "primary" and auth(r, "product_test") == "secondary"
    assert r.profiles["product_test"].preferred == "product"
    assert r.profiles["product_test"].tags == ("test",) and r.profiles["product_test"].tag_basis == "name+twin"
    assert r.profiles["product"].tags == () and r.profiles["product"].confidence == "high"
    assert check_complete(graph, r) == []


def test_singular_and_plural_with_no_evidence_are_undecided_and_nothing_is_guessed():
    _, r = profiles_of(table("product", PRODUCT_COLS), table("products", PRODUCT_COLS))
    assert auth(r, "product") == auth(r, "products") == "undecided"
    assert r.groups[0].primary is None and r.groups[0].kind == "variant"


def test_the_plural_with_clear_evidence_wins_over_a_small_singular():
    ev = {"tables": {"products": {"read_in": 4, "written_in": 1}}}
    _, r = profiles_of(
        table("product", PRODUCT_COLS, pk=("id",), rows=5),
        table("products", PRODUCT_COLS, pk=("id",), rows=20000),
        table("order_item", {"id": INT, "product_id": INT}, pk=("id",)),
        fks=(("order_item", "product_id", "products", "id"),),
        evidence=ev,
    )
    assert auth(r, "products") == "primary" and auth(r, "product") == "secondary"
    assert r.profiles["product"].preferred == "products"
    assert r.profiles["products"].signals.procedure_use == 5 and r.profiles["products"].signals.referenced_by == 1


def test_a_series_is_never_ranked():
    _, r = profiles_of(table("sales_2023", {"id": INT, "amount": INT}), table("sales_2024", {"id": INT, "amount": INT}))
    assert auth(r, "sales_2023") == auth(r, "sales_2024") == "equal" and r.groups[0].kind == "series" and r.groups[0].primary is None


def test_tables_that_only_share_a_word_are_not_twins_and_a_business_word_is_not_a_tag():
    """``lab`` (the labs) and ``lab_sample`` (their samples) are different tables: different columns, so no group, and
    "sample" here is a business word, so no ``test`` tag either."""
    _, r = profiles_of(
        table("lab", {"id": INT, "name": TXT, "site": TXT}),
        table("lab_sample", {"sample_id": INT, "qty": INT, "taken_on": TXT}),
    )
    assert r.groups == [] and auth(r, "lab") == auth(r, "lab_sample") == "standalone"
    assert r.profiles["lab_sample"].tags == () and r.profiles["lab_sample"].tag_basis is None


def test_twins_need_ninety_percent_of_their_column_names_in_common():
    base = {f"c{i}": INT for i in range(10)}
    one_extra = {**base, "backed_up_on": TXT}  # 10 of 11 shared = 0.91
    two_extra = {**base, "backed_up_on": TXT, "backed_up_by": TXT}  # 10 of 12 shared = 0.83
    _, r = profiles_of(table("account", base, pk=("c0",)), table("account_bak", one_extra))
    assert len(r.groups) == 1 and auth(r, "account_bak") == "secondary"
    _, r = profiles_of(table("account", base, pk=("c0",)), table("account_bak", two_extra))
    assert r.groups == [] and auth(r, "account_bak") == "standalone"
    assert r.profiles["account_bak"].tags == ("backup",)  # an unambiguous word still tags the name alone


def test_ambiguous_words_tag_only_next_to_a_look_alike_and_clear_words_tag_alone():
    _, r = profiles_of(table("sample_results", {"id": INT, "v": INT}), table("tmp_stage", {"id": INT, "w": INT}), table("blood_test", {"id": INT, "x": INT}))
    assert r.profiles["sample_results"].tags == () and r.profiles["blood_test"].tags == ()
    assert r.profiles["tmp_stage"].tags == ("temp",) and r.profiles["tmp_stage"].tag_basis == "name"
    _, r = profiles_of(table("blood", PRODUCT_COLS), table("blood_test", PRODUCT_COLS))
    assert r.profiles["blood_test"].tags == ("test",) and r.profiles["blood_test"].tag_basis == "name+twin"


def test_a_marked_table_is_never_primary_while_an_unmarked_sibling_exists():
    _, r = profiles_of(
        table("orders", {"id": INT, "total": INT}),
        table("orders_old", {"id": INT, "total": INT}, pk=("id",), rows=9000),
        table("line", {"id": INT, "a": INT, "b": INT, "c": INT}, pk=("id",)),
        table("line2", {"id": INT, "o": INT}, pk=("id",)),
        table("line3", {"id": INT, "o": INT}, pk=("id",)),
        fks=(("line", "a", "orders_old", "id"), ("line2", "o", "orders_old", "id"), ("line3", "o", "orders_old", "id")),
        evidence={"tables": {"orders_old": {"read_in": 9, "written_in": 9}}},
    )
    assert auth(r, "orders_old") != "primary" and r.groups[0].primary is None


def test_the_extra_abbreviation_marker_is_understood_in_names_and_twins():
    _, r = profiles_of(table("track", PRODUCT_COLS, pk=("id",)), table("track_bk_2024", PRODUCT_COLS))
    assert auth(r, "track") == "primary" and auth(r, "track_bk_2024") == "secondary" and r.groups[0].kind == "copy"
    assert twin_key(("track", "bk"))[0] == ("track", "bk")  # Phase 6's own list is unchanged
    assert twin_key(("track", "bk"), MARKERS) == (("track",), True)


def test_the_same_table_name_in_two_schemas_is_a_group_but_not_guessed():
    _, r = profiles_of(
        table("orders", {"id": INT, "total": INT}, schema="sales"),
        table("orders", {"id": INT, "total": INT}, schema="archive"),
        multi_schema=True,
    )
    assert {auth(r, "sales.orders"), auth(r, "archive.orders")} == {"undecided"}


def test_views_are_profiled_but_are_never_twins():
    graph, r = profiles_of(table("product", PRODUCT_COLS, pk=("id",)), table("product_view", PRODUCT_COLS, kind="view"))
    assert r.groups == [] and auth(r, "product_view") == "standalone" and r.profiles["product_view"].kind == "view"
    assert check_complete(graph, r) == []


def test_a_table_called_just_test_is_left_alone():
    _, r = profiles_of(table("test", {"id": INT}), table("old", {"id": INT}))
    assert r.profiles["test"].tags == () and r.profiles["old"].tags == ()


def test_a_learned_table_prefix_is_the_databases_convention_not_a_marker():
    tables = [table(f"tmp_t{i}", {"id": INT, f"c{i}": INT}) for i in range(6)] + [table(n, {"id": INT, "x": INT}) for n in ("alpha", "beta", "gamma", "delta")]
    _, r = profiles_of(*tables)
    assert all(r.profiles[f"tmp_t{i}"].tags == () for i in range(6))


def test_every_marker_word_maps_to_a_tag():
    assert set(MARKERS) <= set(TAG_OF) and COPY_MARKERS <= MARKERS
    assert marker_tags(table_obj("customer_bak"), frozenset()) == ("backup",)
    assert marker_tags(table_obj("customer_test"), frozenset()) == () and marker_tags(table_obj("customer_test"), frozenset(), backed=True) == ("test",)


def table_obj(name):
    return build(table(name, {"id": INT})).tables[name]


# ================================================================= evidence from other phases


def _inferred(graph, child, parent, **kw):
    entry = {"from_table": child, "from_schema": None, "to_table": parent, "to_schema": None, "band": "high", "direction_known": True, "ambiguous": False}
    entry.update(kw)
    return {"generated_by": "auto_relationships", "inputs_hash": graph.inputs_hash, "input_key": "k1", "relationships": [entry]}


def test_fresh_high_confidence_inferred_edges_count_as_references_and_stale_ones_do_not():
    tables = (table("product", PRODUCT_COLS), table("products", PRODUCT_COLS), table("line", {"id": INT, "product_id": INT}))
    graph = build(*tables)
    doc = _inferred(graph, "line", "products")
    assert fresh_inferred_edges(graph, doc) == [("line", "products")]
    assert fresh_inferred_edges(graph, {**doc, "inputs_hash": "stale"}) == []
    assert fresh_inferred_edges(graph, {**doc, "generated_by": "someone_else"}) == []
    assert fresh_inferred_edges(graph, _inferred(graph, "line", "products", band="medium")) == []
    assert fresh_inferred_edges(graph, _inferred(graph, "line", "products", ambiguous=True)) == []
    assert fresh_inferred_edges(graph, _inferred(graph, "line", "products", direction_known=False)) == []
    assert fresh_inferred_edges(graph, None) == []
    r = build_profiles(prepare(graph), inferred_doc=doc)
    assert r.profiles["products"].signals.referenced_by == 1 and r.inferred_used and summary(r)["inferred_edges_used"]


def test_broken_evidence_files_are_ignored_not_fatal():
    graph = build(table("product", PRODUCT_COLS), table("products", PRODUCT_COLS))
    for ev in ({"tables": []}, {"tables": {"products": "x"}}, {"tables": {"ghost": {"read_in": 3}}}, {"tables": {"products": {"read_in": "many"}}}, {}):
        r = build_profiles(prepare(graph), evidence_doc=ev)
        assert check_complete(graph, r) == []


# ================================================================= completeness and determinism


def test_every_table_and_view_gets_exactly_one_profile_and_the_gate_notices_a_gap():
    graph, r = profiles_of(table("a", {"id": INT}), table("b", {"id": INT}), table("v_a", {"id": INT}, kind="view"), table("a_bak", {"id": INT}))
    assert set(r.profiles) == set(graph.tables) and check_complete(graph, r) == []
    del r.profiles["a_bak"]
    assert "no profile" in check_complete(graph, r)[0]


def test_the_result_is_deterministic():
    tables = (table("product", PRODUCT_COLS), table("products", PRODUCT_COLS), table("product_bak", PRODUCT_COLS), table("sales_1", {"x": INT}), table("sales_2", {"x": INT}))
    a = build_profiles(prepare(build(*tables)))
    b = build_profiles(prepare(build(*reversed(tables))))
    assert a.profiles == b.profiles and a.groups == b.groups


# ================================================================= the file


@pytest.fixture()
def prof_db(dbs: Path) -> Path:
    return dbs


def put(dbs: Path, *tables, **kw):
    schema_doc, inventory, evidence = documents(*tables, **kw)
    folder = dbs / "pg_live" / "schema"
    (folder / "schema.json").write_text(json.dumps(schema_doc), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    if evidence is not None:
        (folder / "sp_evidence.json").write_text(json.dumps(evidence), encoding="utf-8")


def read(dbs: Path):
    return json.loads((dbs / "pg_live" / PROFILES_REL).read_text(encoding="utf-8"))


SET = (table("product", PRODUCT_COLS, pk=("id",), rows=100), table("product_test", PRODUCT_COLS, rows=2), table("customer", {"id": INT, "name": TXT}, pk=("id",)))


def test_the_file_lists_every_table_and_marks_the_copy(prof_db: Path):
    put(prof_db, *SET)
    graph = load_graph("pg_live", prof_db)
    r = build_profiles(prepare(graph))
    out = write_profiles_file("pg_live", graph, r, databases_dir=prof_db)
    assert out == {"written": True, "unchanged": False, "skipped": None}
    doc = read(prof_db)
    assert doc["generated_by"] == GENERATED_BY and set(doc["tables"]) == {"product", "product_test", "customer"}
    assert doc["tables"]["product_test"]["authority"] == "secondary" and doc["tables"]["product_test"]["preferred"] == "product"
    assert doc["tables"]["customer"]["authority"] == "standalone" and doc["groups"][0]["primary"] == "product"
    assert doc["stats"]["tagged"] == {"test": 1}


def test_an_unchanged_input_is_not_rewritten_and_a_changed_one_is(prof_db: Path):
    put(prof_db, *SET)
    graph = load_graph("pg_live", prof_db)
    r = build_profiles(prepare(graph))
    write_profiles_file("pg_live", graph, r, databases_dir=prof_db)
    path = prof_db / "pg_live" / PROFILES_REL
    before = path.stat().st_mtime_ns
    assert write_profiles_file("pg_live", graph, r, databases_dir=prof_db)["unchanged"] is True and path.stat().st_mtime_ns == before
    put(prof_db, *SET, table("extra", {"id": INT}))
    graph2 = load_graph("pg_live", prof_db)
    assert write_profiles_file("pg_live", graph2, build_profiles(prepare(graph2)), databases_dir=prof_db)["written"] is True
    assert input_key(graph, None) != input_key(graph2, None) and input_key(graph, None) != input_key(graph, "other")


def test_a_curated_file_is_never_overwritten(prof_db: Path):
    put(prof_db, *SET)
    path = prof_db / "pg_live" / PROFILES_REL
    path.write_text('{"mine": true}', encoding="utf-8")
    graph = load_graph("pg_live", prof_db)
    out = write_profiles_file("pg_live", graph, build_profiles(prepare(graph)), databases_dir=prof_db)
    assert out["written"] is False and "left untouched" in out["skipped"] and json.loads(path.read_text()) == {"mine": True}
    path.write_text("not json at all", encoding="utf-8")  # unreadable counts as curated too
    assert write_profiles_file("pg_live", graph, build_profiles(prepare(graph)), databases_dir=prof_db)["written"] is False


def test_pinned_and_missing_folders_are_skipped(prof_db: Path):
    put(prof_db, *SET)
    graph = load_graph("pg_live", prof_db)
    r = build_profiles(prepare(graph))
    assert "pinned" in write_profiles_file("erp_main", graph, r, databases_dir=prof_db)["skipped"]
    assert "does not exist" in write_profiles_file("ghost_db", graph, r, databases_dir=prof_db)["skipped"]
    assert not (prof_db / "ghost_db").exists()


# ================================================================= the runner and the sync hook


@pytest.fixture()
def rel_db(prof_db: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", prof_db)
    return prof_db


@pytest.mark.asyncio
async def test_the_runner_reports_counts_only_and_writes_the_file(rel_db: Path):
    put(rel_db, *SET)
    report = await pp.run_profiles_phase(db_id="pg_live", databases_dir=rel_db)
    assert report["ok"] and report["tables"] == 3 and report["groups"] == 1 and report["decided"] == 1 and report["secondary"] == 1
    assert report["tagged"] == {"test": 1} and report["file"]["written"] is True
    assert "product_test" not in json.dumps(report)  # no table names in the report


@pytest.mark.asyncio
async def test_the_runner_skips_pinned_databases_and_databases_without_a_catalog(rel_db: Path):
    assert "pinned" in (await pp.run_profiles_phase(db_id="erp_main", databases_dir=rel_db))["skipped"]
    report = await pp.run_profiles_phase(db_id="pg_live", databases_dir=rel_db)
    assert report["ok"] and "no schema.json" in report["skipped"] and not (rel_db / "pg_live" / PROFILES_REL).exists()


@pytest.mark.asyncio
async def test_the_runner_uses_a_fresh_inferred_file_and_ignores_a_stale_one(rel_db: Path):
    put(rel_db, *SET, table("line", {"id": INT, "product_id": INT}))
    graph = load_graph("pg_live", rel_db)
    inferred = rel_db / "pg_live" / "schema" / "inferred_relationships.json"
    inferred.write_text(json.dumps(_inferred(graph, "line", "product")), encoding="utf-8")
    assert (await pp.run_profiles_phase(db_id="pg_live", databases_dir=rel_db))["inferred_edges_used"] is True
    inferred.write_text(json.dumps({**_inferred(graph, "line", "product"), "inputs_hash": "old"}), encoding="utf-8")
    assert (await pp.run_profiles_phase(db_id="pg_live", databases_dir=rel_db))["inferred_edges_used"] is False


@pytest.mark.asyncio
async def test_a_blown_time_budget_reports_and_the_worker_never_writes_afterwards(rel_db: Path, monkeypatch):
    put(rel_db, *SET)
    real = pp.build_profiles
    monkeypatch.setattr(pp, "build_profiles", lambda *a, **k: (time.sleep(0.4), real(*a, **k))[1])
    report = await pp.run_profiles_phase(db_id="pg_live", databases_dir=rel_db, budget_seconds=0.05)
    assert report["ok"] is False and "time budget" in report["skipped"]
    await asyncio.sleep(0.8)
    assert not (rel_db / "pg_live" / PROFILES_REL).exists()


@pytest.mark.asyncio
async def test_an_incomplete_profile_set_is_never_written(rel_db: Path, monkeypatch):
    put(rel_db, *SET)
    monkeypatch.setattr(pp, "check_complete", lambda graph, result: ["1 catalog tables have no profile"])
    report = await pp.run_profiles_phase(db_id="pg_live", databases_dir=rel_db)
    assert report["ok"] is False and "completeness" in report["error"] and not (rel_db / "pg_live" / PROFILES_REL).exists()


def _flags(*on):
    return lambda flag, db: flag in on


@pytest.mark.asyncio
async def test_sync_without_the_flag_is_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and "table_profiles" not in result and not (dbs / "pg_live" / PROFILES_REL).exists()


@pytest.mark.asyncio
async def test_sync_with_the_flag_writes_the_file_and_adds_a_small_report(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_table_profiles"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    report = result["table_profiles"]
    assert result["status"] == "ok" and report["ok"] and report["tables"] + report["views"] == len(read(dbs)["tables"])
    assert set(read(dbs)["tables"]) == {"customer", "orders", "order_items", "v_orders"}  # nothing hidden, the view included


@pytest.mark.asyncio
async def test_a_crash_in_the_phase_never_fails_the_sync(sqlite_db: Path, dbs: Path, monkeypatch):
    from unittest.mock import AsyncMock

    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_table_profiles"))
    monkeypatch.setattr(pp, "run_profiles_phase", AsyncMock(side_effect=RuntimeError("boom")))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["table_profiles"]["ok"] is False and "boom" in result["table_profiles"]["error"]


def test_the_flag_is_on_for_new_databases_off_for_the_pinned_one_and_the_template_has_no_profiles_file():
    from src.utils import feature_flags as ff

    assert "auto_table_profiles" in ff.DB_FEATURE_FLAGS
    cfg = yaml.safe_load((ROOT / "config" / "feature_flags.example.yaml").read_text(encoding="utf-8"))
    assert cfg["db_defaults"]["auto_table_profiles"] is True and cfg["db_features"]["_example_db"]["auto_table_profiles"] is False
    assert ff.is_db_feature_enabled("auto_table_profiles", "a_new_database") is True and ff.is_db_feature_enabled("auto_table_profiles", "erp_main") is False
    assert not (ROOT / "databases" / "_template" / PROFILES_REL).exists()
    assert au.RULESET_VERSION >= 1
