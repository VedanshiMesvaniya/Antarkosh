"""Phase 6 / steps 6e, 6f, 6g: the writer, the phase runner and hook, and the flags."""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from src.sql import prompt_builder as pb
from src.sql.relationships.context import prepare
from src.sql.relationships.detectors import detect
from src.sql.relationships.gates import resolve
from src.sql.relationships.model import load_graph
from src.sql.relationships.writer import (
    APPLIED_REL,
    GENERATED_BY,
    INFERRED_REL,
    band,
    select_applied,
    write_relationship_files,
)
from src.sql.sa import relationships_phase as rp
from tests.sql.relationships_fixtures import build, documents, proc_edge, table
from tests.sql.test_auto_procedures import _sync_args, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_catalog_extraction import ROOT, dbs  # noqa: F401  (shared fixture)

INT, TXT = "INTEGER", "VARCHAR(50)"

SHOP = (
    table("customers", {"id": INT, "name": TXT}, pk=("id",)),
    table("products", {"id": INT}, pk=("id",)),
    table("orders", {"id": INT, "customer_id": INT}, pk=("id",), indexes=(("customer_id",),)),
    table("order_items", {"order_id": INT, "line_no": INT, "product_id": INT}, pk=("order_id", "line_no")),
)
SHOP_FKS = (("orders", "customer_id", "customers", "id"),)
SHOP_PROC = (proc_edge("order_items", "product_id", "products", "id"),)


@pytest.fixture()
def rel_db(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs)
    pb.clear_prompt_caches()
    yield dbs
    pb.clear_prompt_caches()


def put(dbs: Path, *tables, **kw) -> None:
    """Write the catalog files a real sync would have written into databases/pg_live/schema."""
    schema_doc, inventory, evidence = documents(*tables, **kw)
    folder = dbs / "pg_live" / "schema"
    (folder / "schema.json").write_text(json.dumps(schema_doc), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    if evidence is not None:
        (folder / "sp_evidence.json").write_text(json.dumps(evidence), encoding="utf-8")


def infer(graph):
    ctx = prepare(graph)
    edges, report = resolve(ctx, detect(ctx))
    return edges, report.as_counts()


def write(dbs: Path, graph, *, apply: bool, **kw):
    edges, counts = infer(graph)
    return write_relationship_files("pg_live", graph, edges, counts, apply=apply, databases_dir=dbs, **kw)


def read(dbs: Path, rel: str):
    return json.loads((dbs / "pg_live" / rel).read_text(encoding="utf-8"))


def pairs(doc) -> set[tuple[str, str, str, str]]:
    return {(r["from_table"], r["from_column"], r["to_table"], r["to_column"]) for r in doc["relationships"]}


SHOP_APPLIED = {("orders", "customer_id", "customers", "id"), ("order_items", "product_id", "products", "id")}


# ================================================================= 6e: selection

def test_bands():
    assert (band(0.70), band(0.699), band(0.45), band(0.449)) == ("high", "medium", "medium", "low")


def test_only_high_known_unambiguous_in_scope_edges_are_applied_and_the_rest_are_counted():
    graph = build(*SHOP, fks=SHOP_FKS, proc=SHOP_PROC)
    edges, _ = infer(graph)
    chosen, held = select_applied(graph, edges)
    assert {e.key[:1] + e.key[1:2] + e.key[2:3] + e.key[3:] for e in chosen} == SHOP_APPLIED
    assert held == {"below_threshold": 1}  # order_items.order_id: names + header/detail shape only (0.696)


def test_a_name_only_schema_applies_nothing_because_names_alone_never_reach_the_threshold():
    graph = build(*SHOP)
    edges, _ = infer(graph)
    assert edges and select_applied(graph, edges)[0] == []


def test_ambiguous_edges_wait_unless_a_declared_key_or_procedure_vouches_for_them():
    graph = build(
        table("product", {"id": INT}, pk=("id",)), table("products", {"id": INT}, pk=("id",)), table("line", {"product_id": INT}),
        proc=(proc_edge("line", "product_id", "product", "id"), proc_edge("line", "product_id", "products", "id")),
    )
    edges, _ = infer(graph)
    assert all(e.ambiguous for e in edges) and len(select_applied(graph, edges)[0]) == 2  # both are vouched for by a procedure


def test_direction_unknown_edges_are_never_applied():
    graph = build(table("a", {"x": INT}), table("b", {"y": INT}), proc=(proc_edge("a", "x", "b", "y"),))
    edges, _ = infer(graph)
    chosen, held = select_applied(graph, edges)
    assert chosen == [] and held == {"direction_unknown": 1}


def test_multi_schema_unknown_scope_applies_nothing_and_known_scope_only_the_answering_schema():
    tables = (
        table("customer", {"id": INT}, pk=("id",), schema="sales"), table("orders", {"id": INT, "customer_id": INT}, pk=("id",), schema="sales"),
        table("customer", {"id": INT}, pk=("id",), schema="hr"), table("staff", {"id": INT, "customer_id": INT}, pk=("id",), schema="hr"),
    )
    proc = (proc_edge("sales.orders", "customer_id", "sales.customer", "id"), proc_edge("hr.staff", "customer_id", "hr.customer", "id"))
    unknown = build(*tables, proc=proc, multi_schema=True)
    edges, _ = infer(unknown)
    assert select_applied(unknown, edges) == ([], {"scope_unknown": len(edges)})
    known = build(*tables, proc=proc, multi_schema=True, answering_schema="sales")
    edges, _ = infer(known)
    chosen, held = select_applied(known, edges)
    assert {e.child for e in chosen} == {"sales.orders"} and held == {"outside_scope": 1}


def _hub(children: int, **kw):
    tables = [table("hub", {"id": INT}, pk=("id",))] + [table(f"c{i}", {"id": INT, "hub_id": INT}, pk=("id",)) for i in range(children)]
    return build(*tables, **kw)


def test_the_per_table_cap_holds_back_the_weakest_but_never_a_declared_key():
    proc = tuple(proc_edge(f"c{i}", "hub_id", "hub", "id") for i in range(8))
    graph = _hub(8, proc=proc)
    edges, _ = infer(graph)
    chosen, held = select_applied(graph, edges, max_per_table=5)
    assert len(chosen) == 5 and held == {"over_table_cap": 3}
    declared = _hub(8, fks=tuple((f"c{i}", "hub_id", "hub", "id") for i in range(8)))
    edges, _ = infer(declared)
    assert len(select_applied(declared, edges, max_per_table=5)[0]) == 8


# ================================================================= 6e: writing

def test_review_mode_writes_only_the_inferred_file_and_leaves_relationships_json_alone(rel_db: Path):
    graph = build(*SHOP, fks=SHOP_FKS, proc=SHOP_PROC)
    result = write(rel_db, graph, apply=False)
    assert result["written"] == [INFERRED_REL] and result["skipped"] == {}
    doc = read(rel_db, INFERRED_REL)
    assert doc["generated_by"] == GENERATED_BY and doc["stats"]["edges"] == len(doc["relationships"]) == 3
    assert {r["band"] for r in doc["relationships"]} == {"high", "medium"} and all("evidence" in r for r in doc["relationships"])
    assert read(rel_db, APPLIED_REL) == {"relationships": []}  # the template placeholder, untouched
    assert pb.get_raw_relationships("pg_live") == []  # prompts see nothing new


def test_apply_mode_publishes_the_high_edges_and_prompts_see_them_at_once(rel_db: Path):
    graph = build(*SHOP, fks=SHOP_FKS, proc=SHOP_PROC)
    assert pb.get_raw_relationships("pg_live") == [] and pb.load_relationships("pg_live") == ""  # prime both caches
    result = write(rel_db, graph, apply=True)
    assert result["written"] == [INFERRED_REL, APPLIED_REL] and result["applied_edges"] == 2
    assert pairs(read(rel_db, APPLIED_REL)) == SHOP_APPLIED
    # the caches were cleared by the writer: no restart needed
    assert pairs({"relationships": pb.get_raw_relationships("pg_live")}) == SHOP_APPLIED
    assert pb.load_relationships("pg_live") == "- order_items: product_id->products.id\n- orders: customer_id->customers.id"


def test_an_unchanged_input_is_not_rewritten_and_does_not_clear_the_caches(rel_db: Path, monkeypatch):
    graph = build(*SHOP, fks=SHOP_FKS, proc=SHOP_PROC)
    cleared: list[str] = []
    monkeypatch.setattr("src.sql.prompt_builder.clear_prompt_caches", lambda db_id=None: cleared.append(db_id))
    assert write(rel_db, graph, apply=True)["written"] == [INFERRED_REL, APPLIED_REL] and cleared == ["pg_live"]
    before = {p: (rel_db / "pg_live" / p).read_bytes() for p in (INFERRED_REL, APPLIED_REL)}
    again = write(rel_db, graph, apply=True)
    assert again["written"] == [] and again["unchanged"] == [INFERRED_REL, APPLIED_REL] and cleared == ["pg_live"]
    assert {p: (rel_db / "pg_live" / p).read_bytes() for p in before} == before  # byte for byte: not even the timestamp moved


def test_a_changed_input_or_threshold_rewrites(rel_db: Path):
    write(rel_db, build(*SHOP, fks=SHOP_FKS, proc=SHOP_PROC), apply=True)
    changed = write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True)  # the procedure evidence is gone
    assert changed["written"] == [INFERRED_REL, APPLIED_REL] and pairs(read(rel_db, APPLIED_REL)) == {("orders", "customer_id", "customers", "id")}
    assert write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True, threshold=0.60)["written"] == [INFERRED_REL, APPLIED_REL]


def test_a_curated_relationships_json_is_never_replaced(rel_db: Path):
    curated = {"note": "hand written", "relationships": [{"from_table": "a", "from_column": "x", "to_table": "b", "to_column": "id"}]}
    path = rel_db / "pg_live" / APPLIED_REL
    path.write_text(json.dumps(curated), encoding="utf-8")
    result = write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True)
    assert APPLIED_REL in result["skipped"] and APPLIED_REL not in result["written"]
    assert json.loads(path.read_text(encoding="utf-8")) == curated
    assert INFERRED_REL in result["written"]  # the review file is still produced
    for broken in ("{not json", json.dumps([{"from_table": "a"}])):  # unreadable or a bare list: not ours either
        path.write_text(broken, encoding="utf-8")
        assert APPLIED_REL in write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True)["skipped"]
        assert path.read_text(encoding="utf-8") == broken


def test_the_placeholder_and_our_own_file_are_replaceable_but_a_foreign_inferred_file_is_not(rel_db: Path):
    path = rel_db / "pg_live" / APPLIED_REL
    path.write_text("[]", encoding="utf-8")  # an empty bare list is also a placeholder
    assert APPLIED_REL in write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True)["written"]
    inferred = rel_db / "pg_live" / INFERRED_REL
    inferred.write_text(json.dumps({"relationships": [1]}), encoding="utf-8")
    result = write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True)
    assert INFERRED_REL in result["skipped"] and json.loads(inferred.read_text(encoding="utf-8")) == {"relationships": [1]}


def test_nothing_to_apply_leaves_the_placeholder_but_empties_a_file_we_wrote_earlier(rel_db: Path):
    result = write(rel_db, build(*SHOP), apply=True)  # names alone: nothing is safe
    assert APPLIED_REL in result["skipped"] and read(rel_db, APPLIED_REL) == {"relationships": []}
    write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True)
    assert pairs(read(rel_db, APPLIED_REL)) == {("orders", "customer_id", "customers", "id")}
    write(rel_db, build(*SHOP), apply=True)  # the evidence went away: the stale edge must not linger
    assert read(rel_db, APPLIED_REL)["relationships"] == []


def test_applied_edges_keep_extra_keys_the_prompt_readers_ignore_and_no_temp_files_remain(rel_db: Path):
    write(rel_db, build(*SHOP, fks=SHOP_FKS), apply=True)
    entry = read(rel_db, APPLIED_REL)["relationships"][0]
    assert {"from_table", "from_column", "to_table", "to_column", "confidence", "evidence", "cardinality"} <= set(entry)
    assert [p.name for p in (rel_db / "pg_live/schema").iterdir() if p.suffix == ".tmp"] == []


def test_erp_main_and_a_missing_folder_are_never_written(rel_db: Path):
    graph = build(*SHOP, fks=SHOP_FKS)
    edges, counts = infer(graph)
    assert write_relationship_files("erp_main", graph, edges, counts, apply=True, databases_dir=rel_db)["skipped"] == {
        "all": "pinned database: curated files are never replaced"
    }
    assert write_relationship_files("ghost", graph, edges, counts, apply=True, databases_dir=rel_db)["skipped"] == {"all": "database folder does not exist"}
    assert not (rel_db / "ghost").exists() and not (rel_db / "erp_main").exists()


def test_the_edges_are_identical_across_runs_whatever_order_the_catalog_lists_tables_in(rel_db: Path):
    graph = build(*SHOP, fks=SHOP_FKS, proc=SHOP_PROC)
    write(rel_db, graph, apply=True)
    first = {rel: read(rel_db, rel) for rel in (INFERRED_REL, APPLIED_REL)}
    for rel in first:
        (rel_db / "pg_live" / rel).unlink()
    write(rel_db, build(*reversed(SHOP), fks=SHOP_FKS, proc=SHOP_PROC), apply=True)  # tables listed in another order
    for rel, doc in first.items():
        again = read(rel_db, rel)
        for d in (doc, again):  # the hashes cover the input documents as listed, so they may differ; the content may not
            for volatile in ("generated_at", "inputs_hash", "input_key"):
                d.pop(volatile, None)
        assert again == doc


# ================================================================= 6f: the runner

@pytest.mark.asyncio
async def test_runner_reads_the_files_on_disk_and_returns_counts_only(rel_db: Path):
    put(rel_db, *SHOP, fks=SHOP_FKS, proc=SHOP_PROC)
    report = await rp.run_relationships_phase(db_id="pg_live")
    assert report["ok"] and report["mode"] == "review" and (report["tables"], report["edges"], report["high"], report["medium"]) == (4, 3, 2, 1)
    assert report["files"]["written"] == [INFERRED_REL] and report["applied_edges"] == 0
    flat = json.dumps(report)
    assert all(name not in flat for name in ("customers", "order_items", "product_id"))  # counts, not content
    assert read(rel_db, APPLIED_REL) == {"relationships": []}


@pytest.mark.asyncio
async def test_runner_in_apply_mode_publishes(rel_db: Path):
    put(rel_db, *SHOP, fks=SHOP_FKS, proc=SHOP_PROC)
    report = await rp.run_relationships_phase(db_id="pg_live", apply=True)
    assert report["mode"] == "apply" and report["applied_edges"] == 2 and report["held_back"] == {"below_threshold": 1}
    assert pairs(read(rel_db, APPLIED_REL)) == SHOP_APPLIED


@pytest.mark.asyncio
async def test_runner_with_no_schema_json_or_for_erp_main_does_nothing(rel_db: Path):
    assert (await rp.run_relationships_phase(db_id="pg_live"))["skipped"].startswith("no schema.json")
    assert await rp.run_relationships_phase(db_id="erp_main", apply=True) == {"ok": True, "skipped": "pinned database"}
    assert not (rel_db / "pg_live" / INFERRED_REL).exists()


@pytest.mark.asyncio
async def test_a_blown_time_budget_reports_and_the_worker_never_writes_afterwards(rel_db: Path, monkeypatch):
    import time

    put(rel_db, *SHOP, fks=SHOP_FKS)
    real = rp.detect
    monkeypatch.setattr(rp, "detect", lambda ctx: (time.sleep(0.4), real(ctx))[1])
    report = await rp.run_relationships_phase(db_id="pg_live", apply=True, budget_seconds=0.05)
    assert report["ok"] is False and "time budget" in report["skipped"]
    await asyncio.sleep(0.8)  # the worker thread finishes its stage, sees the stop signal and exits
    assert not (rel_db / "pg_live" / INFERRED_REL).exists() and read(rel_db, APPLIED_REL) == {"relationships": []}


# ================================================================= 6f: the sync hook

def _flags(*on):
    return lambda flag, db: flag in on


@pytest.mark.asyncio
async def test_sync_without_the_flag_is_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_relationships_apply"))  # apply alone does nothing
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and "relationships" not in result
    assert not (dbs / "pg_live" / INFERRED_REL).exists()


@pytest.mark.asyncio
async def test_sync_with_the_flag_writes_the_review_file_and_returns_a_small_report(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_relationships"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    report = result["relationships"]
    assert report["ok"] and report["mode"] == "review" and report["edges"] == 2 and report["high"] == 2  # the two declared foreign keys
    assert pairs(read(dbs, INFERRED_REL)) == {("orders", "customer_id", "customer", "id"), ("order_items", "order_id", "orders", "id")}
    assert read(dbs, APPLIED_REL) == {"relationships": []}


@pytest.mark.asyncio
async def test_sync_with_both_flags_publishes_to_relationships_json(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_relationships", "auto_relationships_apply"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["relationships"]["applied_edges"] == 2
    assert pairs(read(dbs, APPLIED_REL)) == {("orders", "customer_id", "customer", "id"), ("order_items", "order_id", "orders", "id")}
    assert len(pb.get_raw_relationships("pg_live")) == 2


@pytest.mark.asyncio
async def test_a_crash_in_the_phase_never_fails_the_sync(sqlite_db: Path, dbs: Path, monkeypatch):
    from unittest.mock import AsyncMock

    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_relationships"))
    monkeypatch.setattr(rp, "run_relationships_phase", AsyncMock(side_effect=RuntimeError("boom")))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["relationships"]["ok"] is False and "boom" in result["relationships"]["error"]


# ================================================================= 6g: flags

def test_both_flags_exist_and_erp_main_is_pinned_off_even_when_the_yaml_says_true(monkeypatch):
    from src.utils import feature_flags as ff

    assert "auto_relationships" in ff.DB_FEATURE_FLAGS and "auto_relationships_apply" in ff.DB_FEATURE_FLAGS
    monkeypatch.setattr(ff, "_load_db_flags_from_yaml", lambda: {
        "erp_main": {"auto_relationships": True, "auto_relationships_apply": True}, "pg_live": {"auto_relationships_apply": True},
    })
    assert not ff.is_db_feature_enabled("auto_relationships", "erp_main") and not ff.is_db_feature_enabled("auto_relationships_apply", "erp_main")
    assert ff.is_db_feature_enabled("auto_relationships_apply", "pg_live") and not ff.is_db_feature_enabled("auto_relationships", "pg_live")
    assert not ff.is_db_feature_enabled("auto_relationships_apply", "unlisted")


def test_the_shipped_config_keeps_both_relationship_flags_off_for_every_new_database():
    """New databases inherit ``db_defaults``, so it must keep both flags off. A database listed under ``db_features``
    is an explicit choice by its owner and is not checked here; the example entry must stay off."""
    cfg = yaml.safe_load((ROOT / "config" / "feature_flags.yaml").read_text(encoding="utf-8"))
    defaults = cfg.get("db_defaults") or {}
    assert not defaults.get("auto_relationships") and not defaults.get("auto_relationships_apply")
    assert cfg["db_features"]["_example_db"]["auto_relationships"] is False
    assert cfg["db_features"]["_example_db"]["auto_relationships_apply"] is False


def test_loading_a_graph_for_a_database_without_files_is_none(dbs: Path):
    assert load_graph("pg_live", dbs) is None
