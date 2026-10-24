"""Phase 8: join paths and indirect jumps (``join_paths.json``)."""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml

from src.sql.paths import links as lk
from src.sql.paths.links import build_links, collect_joins, content_hash, select_nodes
from src.sql.paths.search import (
    Params,
    Route,
    check_routes,
    find_paths,
    hub_factor,
    route_shape,
    summary_counts,
)
from src.sql.paths.writer import (
    GENERATED_BY,
    PATHS_REL,
    input_key,
    paths_document,
    write_paths_file,
)
from src.sql.relationships.model import graph_from_documents, load_graph
from src.sql.sa import paths_phase as pp
from tests.sql.relationships_fixtures import build, documents, proc_edge, table
from tests.sql.test_auto_procedures import _sync_args, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_catalog_extraction import ROOT, dbs  # noqa: F401  (shared fixture)

INT = "INTEGER"
FIX = Path(__file__).parent / "fixtures" / "relationships"


def t(name, *cols, **kw):
    return table(name, {"id": INT, **{c: INT for c in cols}}, pk=("id",), **kw)


def run(*tables, fks=(), proc=(), relationships=None, inferred=None, profiles=None, params=None, **kw):
    """Build the trusted graph from synthetic tables and search it. Returns (pairs, adj, inputs, graph)."""
    graph = build(*tables, fks=fks, proc=proc, **kw)
    inputs = select_nodes(graph, profiles)
    adj = build_links(collect_joins(graph, inputs, relationships_doc=relationships, inferred_doc=inferred))
    pairs, _ = find_paths(adj, inputs.blocked_as_bridge, params or Params())
    return pairs, adj, inputs, graph


# The shop: order_item -> order -> customer -> region, order_item -> product, and a junction product_tag between product and tag
SHOP = (
    t("region"), t("customer", "region_id"), t("orders", "customer_id"), t("order_item", "order_id", "product_id"),
    t("product"), t("tag"), t("product_tag", "product_id", "tag_id"),
)
SHOP_FKS = (
    ("customer", "region_id", "region", "id"), ("orders", "customer_id", "customer", "id"), ("order_item", "order_id", "orders", "id"),
    ("order_item", "product_id", "product", "id"), ("product_tag", "product_id", "product", "id"), ("product_tag", "tag_id", "tag", "id"),
)


def tables_of(routes: list[Route]) -> list[tuple[str, ...]]:
    return [r.tables for r in routes]


# ================================================================= search and scoring


def test_a_chain_of_declared_keys_is_found_with_a_score_that_falls_per_join():
    pairs, *_ = run(*SHOP, fks=SHOP_FKS)
    two = pairs[("customer", "order_item")][0]
    three = pairs[("order_item", "region")][0]
    assert two.tables == ("customer", "orders", "order_item") and two.score == 0.9 and two.shape == "chain"
    assert three.tables == ("order_item", "orders", "customer", "region") and three.score == 0.81


def test_direct_neighbours_are_never_listed_as_indirect_jumps():
    pairs, adj, *_ = run(*SHOP, fks=SHOP_FKS)
    assert all(b not in adj[a] for a, b in pairs) and ("orders", "customer") not in pairs and ("customer", "orders") not in pairs


def test_depth_limits_the_routes_and_depth_four_reaches_further():
    two, *_ = run(*SHOP, fks=SHOP_FKS, params=Params(max_depth=2))
    three, *_ = run(*SHOP, fks=SHOP_FKS)
    four, *_ = run(*SHOP, fks=SHOP_FKS, params=Params(max_depth=4))
    assert ("order_item", "region") not in two and ("order_item", "region") in three
    assert ("region", "tag") not in three and ("region", "tag") not in two
    assert max(len(r.tables) - 1 for rs in four.values() for r in rs) == 4
    assert ("product", "region") in four and ("product", "region") not in three  # product-order_item-orders-customer-region is 4 joins
    assert ("product_tag", "region") not in four  # 5 joins


def test_shapes_chain_bridge_and_siblings_are_named_and_the_odd_shapes_cost_more():
    pairs, *_ = run(*SHOP, fks=SHOP_FKS)
    bridge = pairs[("product", "tag")][0]  # product <- product_tag -> tag: the normal many-to-many
    siblings = pairs[("order_item", "product_tag")][0]  # both are children of product
    assert bridge.shape == "bridge" and bridge.score == 0.81
    assert siblings.shape == "siblings" and siblings.score == 0.63
    assert route_shape([]) == "unknown" and route_shape([None]) == "unknown" and route_shape(["up", "up"]) == "chain"
    assert route_shape(["up", "down", "up"]) == "mixed"


def test_a_hub_is_damped_and_a_normal_table_is_not():
    kids = tuple(t(f"k{i}", "status_id") for i in range(40))
    star = (t("status"), *kids)
    fks = tuple((f"k{i}", "status_id", "status", "id") for i in range(40))
    damped, *_ = run(*star, fks=fks)
    assert ("k0", "k1") not in damped  # 0.9 x 0.7 siblings x sqrt(8/40) is far under the floor
    free, *_ = run(*star, fks=fks, params=Params(hub_free=100))
    assert free[("k0", "k1")][0].score == 0.63
    assert hub_factor(8, 8) == 1.0 and round(hub_factor(32, 8), 3) == 0.5 and hub_factor(1, 8) == 1.0


def test_weak_edges_die_quickly_because_scores_multiply():
    weak = {"relationships": [
        {"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id", "confidence": 0.7},
        {"from_table": "order_item", "from_column": "order_id", "to_table": "orders", "to_column": "id", "confidence": 0.7},
        {"from_table": "customer", "from_column": "region_id", "to_table": "region", "to_column": "id", "confidence": 0.7},
    ]}
    pairs, *_ = run(*SHOP, relationships=weak)
    assert ("customer", "order_item") in pairs and pairs[("customer", "order_item")][0].score == round(0.7 * 0.7 * 0.9, 4)
    assert ("order_item", "region") not in pairs  # 0.7^3 x 0.81 = 0.28, under 0.30


def test_a_pair_is_stored_once_oriented_from_the_smaller_name_and_two_routes_at_most():
    two_ways = SHOP + (t("link_a", "customer_id", "region_id"),)
    fks = SHOP_FKS + (("link_a", "customer_id", "customer", "id"), ("link_a", "region_id", "region", "id"))
    pairs, *_ = run(*two_ways, fks=fks)
    assert all(a < b for a, b in pairs) and all(r.tables[0] == a and r.tables[-1] == b for (a, b), rs in pairs.items() for r in rs)
    assert all(len(rs) <= 2 for rs in pairs.values())
    assert len(tables_of(pairs[("orders", "region")])) == 2  # via customer, and via customer-link_a-region


def test_several_foreign_keys_between_two_tables_are_one_link_that_remembers_the_others():
    tabs = (t("language"), t("film", "language_id", "original_language_id"), t("film_text", "film_id"))
    fks = (("film", "language_id", "language", "id"), ("film", "original_language_id", "language", "id"), ("film_text", "film_id", "film", "id"))
    _, adj, *_ = run(*tabs, fks=fks)
    assert adj["film"]["language"].alternates == 1 and adj["film"]["language"].join.column_of("film") == "language_id"


def test_the_result_is_deterministic_whatever_order_the_tables_arrive_in():
    a, *_ = run(*SHOP, fks=SHOP_FKS)
    b, *_ = run(*reversed(SHOP), fks=tuple(reversed(SHOP_FKS)))
    assert a == b and list(a) == sorted(a)


def test_work_caps_cut_the_search_but_never_produce_a_false_route():
    graph = build(*SHOP, fks=SHOP_FKS)
    inputs = select_nodes(graph, None)
    adj = build_links(collect_joins(graph, inputs, relationships_doc=None, inferred_doc=None))
    tiny = Params(max_pops_per_table=3, max_targets_per_table=1)
    pairs, stats = find_paths(adj, set(), tiny)
    assert stats.work_capped > 0 or stats.target_capped > 0
    assert check_routes(pairs, adj, inputs.nodes, tiny) == []
    capped, st = find_paths(adj, set(), Params(max_pairs=2))
    assert len(capped) == 2 and st.pairs_dropped > 0


def test_the_route_check_catches_a_route_that_is_not_real():
    pairs, adj, inputs, _ = run(*SHOP, fks=SHOP_FKS)
    p = Params()
    assert check_routes(pairs, adj, inputs.nodes, p) == []
    fake = {("region", "tag"): [Route(("region", "tag"), 0.9, "chain")], ("customer", "orders"): [Route(("customer", "region", "orders"), 0.9, "chain")]}
    problems = check_routes(fake, adj, inputs.nodes, p)
    assert len(problems) >= 2 and any("directly" in x for x in problems)
    assert check_routes({("a", "c"): [Route(("a", "b", "a", "c"), 0.9, "chain")]}, adj, {"a", "b", "c"}, p)


def test_the_stop_signal_is_honoured_between_tables():
    graph = build(*SHOP, fks=SHOP_FKS)
    adj = build_links(collect_joins(graph, select_nodes(graph, None), relationships_doc=None, inferred_doc=None))

    def boom():
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        find_paths(adj, set(), Params(), should_stop=boom)


# ================================================================= which tables and edges are trusted


def test_views_and_tables_outside_the_prompt_schema_are_not_part_of_any_route():
    tabs = (t("a"), t("b", "a_id"), t("v_b", "a_id", kind="view"), t("c", "b_id"))
    pairs, _, inputs, _graph = run(*tabs, fks=(("b", "a_id", "a", "id"), ("c", "b_id", "b", "id")))
    assert ("a", "c") in pairs and "v_b" not in inputs.nodes and inputs.counts["view"] == 1
    multi = build(t("x", schema="s1"), t("y", "x_id", schema="s2"), multi_schema=True)
    assert select_nodes(multi, None).nodes == set() and select_nodes(multi, None).counts["scope_unknown"] == 2
    scoped = build(t("x", schema="s1"), t("y", "x_id", schema="s2"), multi_schema=True, answering_schema="s1")
    assert select_nodes(scoped, None).nodes == {"s1.x"} and select_nodes(scoped, None).counts["outside_scope"] == 1


def test_curated_relationships_resolve_by_bare_name_default_to_090_and_report_what_they_cannot_resolve():
    doc = {"relationships": [
        {"from_table": "ORDERS", "from_column": "customer_id", "to_table": "Customer", "to_column": "id"},
        {"from_table": "ghost", "from_column": "x", "to_table": "customer", "to_column": "id"},
        {"from_table": "orders", "from_column": "", "to_table": "customer", "to_column": "id"},
        "junk",
    ]}
    graph = build(*SHOP)
    inputs = select_nodes(graph, None)
    joins = collect_joins(graph, inputs, relationships_doc=doc, inferred_doc=None)
    assert [(j.a, j.b, j.confidence, j.kinds, j.child) for j in joins] == [("customer", "orders", 0.9, ("relationships_file",), "orders")]
    assert inputs.counts["relationships_file_unresolved"] == 2
    assert collect_joins(graph, select_nodes(graph, None), relationships_doc={"relationships": "no"}, inferred_doc=None) == []


def _inferred_doc(graph, **entry):
    base = {"from_table": "orders", "from_schema": None, "from_column": "customer_id", "to_table": "customer", "to_schema": None, "to_column": "id",
            "band": "high", "publishable": True, "direction_known": True, "confidence": 0.82}
    base.update(entry)
    return {"generated_by": "auto_relationships", "inputs_hash": graph.inputs_hash, "input_key": "k", "relationships": [base]}


def test_only_fresh_publishable_high_confidence_inferred_edges_count():
    graph = build(*SHOP)

    def used(doc):
        inputs = select_nodes(graph, None)
        joins = collect_joins(graph, inputs, relationships_doc=None, inferred_doc=doc)
        return len(joins), inputs.inferred_used

    assert used(_inferred_doc(graph)) == (1, True)
    assert used({**_inferred_doc(graph), "inputs_hash": "stale"}) == (0, False)
    assert used({**_inferred_doc(graph), "generated_by": "someone"}) == (0, False)
    for bad in ({"band": "medium"}, {"publishable": False}, {"direction_known": False}):
        assert used(_inferred_doc(graph, **bad)) == (0, False), bad
    assert used(None) == (0, False)


def test_procedure_joins_count_only_when_seen_in_two_or_more_routines():
    graph = build(*SHOP, proc=(proc_edge("orders", "customer_id", "customer", "id"), proc_edge("order_item", "product_id", "product", "id", confidence="low", routines=1)))
    joins = collect_joins(graph, select_nodes(graph, None), relationships_doc=None, inferred_doc=None)
    assert [(j.a, j.b, j.kinds, j.child) for j in joins] == [("customer", "orders", ("procedure",), None)]
    _, adj, *_ = run(*SHOP, proc=(proc_edge("orders", "customer_id", "customer", "id"), proc_edge("order_item", "order_id", "orders", "id")))
    assert adj["orders"]["customer"].step("orders") is None  # direction unknown, so the route is not shaped by it


def test_the_same_join_from_two_sources_is_one_join_with_the_best_confidence_and_a_known_direction():
    graph = build(*SHOP, fks=(("orders", "customer_id", "customer", "id"),), proc=(proc_edge("customer", "id", "orders", "customer_id"),))
    doc = {"relationships": [{"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id", "confidence": 0.5}]}
    joins = collect_joins(graph, select_nodes(graph, None), relationships_doc=doc, inferred_doc=None)
    assert len(joins) == 1 and joins[0].confidence == 1.0 and joins[0].kinds == ("declared", "procedure", "relationships_file") and joins[0].child == "orders"


def test_self_references_do_not_connect_anything():
    graph = build(t("emp", "manager_id"), fks=(("emp", "manager_id", "emp", "id"),))
    inputs = select_nodes(graph, None)
    assert collect_joins(graph, inputs, relationships_doc=None, inferred_doc=None) == [] and inputs.counts["self_reference"] == 1


# ================================================================= Phase 7 (table profiles) feeds the paths


def _profiles(graph, **authority):
    tables = {q: {"authority": authority.get(q, "standalone"), "tags": authority.get(f"tags:{q}", [])} for q in graph.tables}
    return {"generated_by": "auto_table_profiles", "inputs_hash": graph.inputs_hash, "input_key": "p", "tables": tables}


COPIES = (t("customer"), t("customer_bak"), t("orders", "customer_id"), t("invoice", "order_id"), t("tmp_stage", "order_id", "invoice_id"))
COPY_FKS = (("orders", "customer_id", "customer", "id"), ("orders", "customer_id", "customer_bak", "id"),
            ("invoice", "order_id", "orders", "id"), ("tmp_stage", "order_id", "orders", "id"), ("tmp_stage", "invoice_id", "invoice", "id"))


def test_a_copy_of_another_table_is_left_out_of_every_route_but_still_exists_in_the_catalog():
    graph = build(*COPIES, fks=COPY_FKS)
    without, *_ = run(*COPIES, fks=COPY_FKS)
    with_profiles, _, inputs, _ = run(*COPIES, fks=COPY_FKS, profiles=_profiles(graph, customer_bak="secondary"))
    assert ("customer_bak", "invoice") in without and not any("customer_bak" in k for k in with_profiles)
    assert inputs.profiles_used and inputs.counts["secondary_copy"] == 1 and "customer_bak" not in inputs.nodes


def test_a_temp_table_can_end_a_route_but_never_bridge_two_tables():
    graph = build(*COPIES, fks=COPY_FKS)
    prof = _profiles(graph, **{"tags:tmp_stage": ["temp"]})
    pairs, _, inputs, _ = run(*COPIES, fks=COPY_FKS, profiles=prof)
    assert inputs.blocked_as_bridge == {"tmp_stage"}
    assert all("tmp_stage" not in r.tables[1:-1] for rs in pairs.values() for r in rs)  # never in the middle


def test_stale_or_foreign_profiles_change_nothing():
    graph = build(*COPIES, fks=COPY_FKS)
    for bad in ({**_profiles(graph, customer_bak="secondary"), "inputs_hash": "old"}, {**_profiles(graph, customer_bak="secondary"), "generated_by": "me"}, None, {"tables": []}):
        inputs = select_nodes(graph, bad)
        assert "customer_bak" in inputs.nodes and not inputs.profiles_used


# ================================================================= real catalogs


def _real(name):
    s = json.loads((FIX / f"{name}.schema.json").read_text(encoding="utf-8"))
    i = json.loads((FIX / f"{name}.inventory.json").read_text(encoding="utf-8"))
    graph = graph_from_documents(s, i)
    inputs = select_nodes(graph, None)
    adj = build_links(collect_joins(graph, inputs, relationships_doc=None, inferred_doc=None))
    return graph, inputs, adj


@pytest.mark.parametrize("name", ["pagila", "sakila", "chinook", "northwind", "employees"])
def test_every_route_on_a_real_catalog_is_real_and_no_route_uses_a_view(name):
    graph, inputs, adj = _real(name)
    p = Params(max_depth=4)
    pairs, _ = find_paths(adj, set(), p)
    assert pairs and check_routes(pairs, adj, inputs.nodes, p) == []
    views = {q for q, tb in graph.tables.items() if tb.is_view}
    assert not any(set(r.tables) & views for rs in pairs.values() for r in rs)


@pytest.mark.parametrize("name", ["pagila", "sakila"])
def test_the_textbook_rental_chain_is_the_best_route_between_customer_and_film(name):
    _, _, adj = _real(name)
    pairs, _ = find_paths(adj, set(), Params())
    best = pairs[("customer", "film")][0]
    assert best.tables == ("customer", "rental", "inventory", "film") and best.score == 0.729


def test_chinook_siblings_and_chains_are_named_correctly():
    _, _, adj = _real("chinook")
    pairs, _ = find_paths(adj, set(), Params())
    assert pairs[("artist", "track")][0].tables == ("artist", "album", "track") and pairs[("artist", "track")][0].shape == "chain"
    assert pairs[("invoice_line", "playlist_track")][0].shape == "siblings"


def test_summary_counts_describe_the_graph():
    _, _inputs, adj = _real("sakila")
    pairs, stats = find_paths(adj, set(), Params())
    s = summary_counts(adj, pairs, stats, Params())
    assert s["tables_in_graph"] == len(adj) and s["links"] == sum(len(n) for n in adj.values()) // 2 and s["pairs"] == len(pairs) and sum(s["shapes"].values()) == s["routes"]


def test_a_huge_hub_does_not_blow_up_the_search_and_is_never_a_bridge():
    n = 1500
    kids = tuple(t(f"k{i}", "hub_id") for i in range(n))
    graph = build(t("hub"), *kids, fks=tuple((f"k{i}", "hub_id", "hub", "id") for i in range(n)))
    inputs = select_nodes(graph, None)
    adj = build_links(collect_joins(graph, inputs, relationships_doc=None, inferred_doc=None))
    start = time.monotonic()
    pairs, _ = find_paths(adj, set(), Params())
    assert time.monotonic() - start < 20 and pairs == {}  # damped far below the floor, and fast because the hub's list is never read


# ================================================================= the file


def test_content_hash_ignores_timestamps_and_notices_content():
    assert content_hash({"a": 1, "generated_at": "x"}) == content_hash({"a": 1, "generated_at": "y"}) != content_hash({"a": 2})
    assert content_hash(None) is None and content_hash([1]) is None and lk.CURATED_DEFAULT == 0.9


@pytest.fixture()
def paths_db(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs)
    return dbs


def put(dbs: Path, *tables, fks=(), proc=(), extra: dict | None = None):
    schema_doc, inventory, evidence = documents(*tables, fks=fks, proc=proc)
    folder = dbs / "pg_live" / "schema"
    (folder / "schema.json").write_text(json.dumps(schema_doc), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    sp = folder / "sp_evidence.json"
    if evidence is not None:
        sp.write_text(json.dumps(evidence), encoding="utf-8")
    elif sp.exists():
        sp.unlink()
    for name, doc in (extra or {}).items():
        (folder / name).write_text(json.dumps(doc), encoding="utf-8")


def read(dbs: Path):
    return json.loads((dbs / "pg_live" / PATHS_REL).read_text(encoding="utf-8"))


def _doc(dbs: Path, **kw):
    graph = load_graph("pg_live", dbs)
    inputs = select_nodes(graph, None)
    adj = build_links(collect_joins(graph, inputs, relationships_doc=None, inferred_doc=None))
    p = Params()
    pairs, st = find_paths(adj, set(), p)
    key = input_key(graph, p, relationships_hash=None, inferred_key=None, profiles_key=None)
    return graph, paths_document(graph, pairs, adj, p, summary_counts(adj, pairs, st, p), {"nodes": len(inputs.nodes)}, key)


def test_the_file_lists_each_route_with_its_columns_and_evidence(paths_db: Path):
    put(paths_db, *SHOP, fks=SHOP_FKS)
    _, doc = _doc(paths_db)
    assert write_paths_file("pg_live", doc, databases_dir=paths_db) == {"written": True, "unchanged": False, "skipped": None}
    out = read(paths_db)
    pair = next(p for p in out["pairs"] if (p["a"], p["b"]) == ("customer", "order_item"))
    assert out["generated_by"] == GENERATED_BY and pair["hops"] == 2 and pair["score"] == 0.9
    route = pair["routes"][0]
    assert route["tables"] == ["customer", "orders", "order_item"] and route["shape"] == "chain"
    assert [j["from"] for j in route["joins"]] == ["customer.id", "orders.id"] and [j["to"] for j in route["joins"]] == ["orders.customer_id", "order_item.order_id"]
    assert route["joins"][0]["evidence"] == ["declared"] and route["joins"][0]["direction"] == "down" and route["joins"][1]["confidence"] == 1.0


def test_an_unchanged_input_is_not_rewritten_and_a_changed_one_is(paths_db: Path):
    put(paths_db, *SHOP, fks=SHOP_FKS)
    write_paths_file("pg_live", _doc(paths_db)[1], databases_dir=paths_db)
    path = paths_db / "pg_live" / PATHS_REL
    before = path.stat().st_mtime_ns
    assert write_paths_file("pg_live", _doc(paths_db)[1], databases_dir=paths_db)["unchanged"] is True and path.stat().st_mtime_ns == before
    put(paths_db, *SHOP, t("extra", "region_id"), fks=(*SHOP_FKS, ("extra", "region_id", "region", "id")))
    assert write_paths_file("pg_live", _doc(paths_db)[1], databases_dir=paths_db)["written"] is True
    g = load_graph("pg_live", paths_db)
    assert input_key(g, Params(), relationships_hash="a", inferred_key=None, profiles_key=None) != input_key(g, Params(), relationships_hash="b", inferred_key=None, profiles_key=None)
    assert input_key(g, Params(), relationships_hash=None, inferred_key=None, profiles_key=None) != input_key(g, Params(max_depth=4), relationships_hash=None, inferred_key=None, profiles_key=None)


def test_a_curated_file_is_never_overwritten_and_pinned_or_missing_folders_are_skipped(paths_db: Path):
    put(paths_db, *SHOP, fks=SHOP_FKS)
    path = paths_db / "pg_live" / PATHS_REL
    path.write_text('{"mine": true}', encoding="utf-8")
    doc = _doc(paths_db)[1]
    out = write_paths_file("pg_live", doc, databases_dir=paths_db)
    assert out["written"] is False and "left untouched" in out["skipped"] and json.loads(path.read_text()) == {"mine": True}
    path.write_text("not json", encoding="utf-8")
    assert write_paths_file("pg_live", doc, databases_dir=paths_db)["written"] is False
    assert "pinned" in write_paths_file("erp_main", doc, databases_dir=paths_db)["skipped"]
    assert "does not exist" in write_paths_file("ghost_db", doc, databases_dir=paths_db)["skipped"] and not (paths_db / "ghost_db").exists()


# ================================================================= the runner and the sync hook


@pytest.mark.asyncio
async def test_the_runner_reports_counts_only_and_writes_the_file(paths_db: Path):
    put(paths_db, *SHOP, fks=SHOP_FKS)
    report = await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db)
    assert report["ok"] and report["pairs"] > 0 and report["file"]["written"] is True and report["nodes"] == len(SHOP)
    assert "order_item" not in json.dumps(report)  # no table names in the report


@pytest.mark.asyncio
async def test_the_runner_uses_the_relationships_file_and_fresh_profiles_and_rewrites_when_they_change(paths_db: Path):
    put(paths_db, *COPIES, fks=())
    folder = paths_db / "pg_live" / "schema"
    rel = {"generated_by": "someone", "relationships": [
        {"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id"},
        {"from_table": "orders", "from_column": "customer_id", "to_table": "customer_bak", "to_column": "id"},
        {"from_table": "invoice", "from_column": "order_id", "to_table": "orders", "to_column": "id"},
    ]}
    (folder / "relationships.json").write_text(json.dumps(rel), encoding="utf-8")
    first = await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db)
    assert first["ok"] and first["file"]["written"] and ("customer", "invoice") in {(p["a"], p["b"]) for p in read(paths_db)["pairs"]}
    again = await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db)
    assert again["file"]["unchanged"] is True
    rel["relationships"].pop()
    (folder / "relationships.json").write_text(json.dumps(rel), encoding="utf-8")
    assert (await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db))["file"]["written"] is True
    graph = load_graph("pg_live", paths_db)
    (folder / "table_profiles.json").write_text(json.dumps(_profiles(graph, customer_bak="secondary")), encoding="utf-8")
    report = await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db)
    doc = read(paths_db)
    assert report["table_profiles_used"] is True and doc["inputs"]["excluded_tables"] == {"secondary_copy": 1} and doc["inputs"]["edges_dropped"] == {"not_a_node": 1}
    assert not any("customer_bak" in (p["a"], p["b"]) for p in doc["pairs"])


@pytest.mark.asyncio
async def test_the_runner_skips_pinned_databases_and_databases_without_a_catalog(paths_db: Path):
    assert "pinned" in (await pp.run_paths_phase(db_id="erp_main", databases_dir=paths_db))["skipped"]
    report = await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db)
    assert report["ok"] and "no schema.json" in report["skipped"] and not (paths_db / "pg_live" / PATHS_REL).exists()


@pytest.mark.asyncio
async def test_a_blown_time_budget_reports_and_the_worker_never_writes_afterwards(paths_db: Path, monkeypatch):
    put(paths_db, *SHOP, fks=SHOP_FKS)
    real = pp.find_paths

    def slow(adj, blocked, params, *, should_stop=None):
        time.sleep(0.4)
        return real(adj, blocked, params, should_stop=should_stop)

    monkeypatch.setattr(pp, "find_paths", slow)
    report = await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db, budget_seconds=0.05)
    assert report["ok"] is False and "time budget" in report["skipped"]
    await asyncio.sleep(0.8)  # the worker finishes its stage, sees the stop signal at the first table and exits
    assert not (paths_db / "pg_live" / PATHS_REL).exists()


@pytest.mark.asyncio
async def test_a_route_that_fails_the_check_is_never_written(paths_db: Path, monkeypatch):
    put(paths_db, *SHOP, fks=SHOP_FKS)
    monkeypatch.setattr(pp, "check_routes", lambda *a, **k: ["x|y: a route uses a table or join that is not in the trusted graph"])
    report = await pp.run_paths_phase(db_id="pg_live", databases_dir=paths_db)
    assert report["ok"] is False and "route check failed" in report["error"] and not (paths_db / "pg_live" / PATHS_REL).exists()


def _flags(*on):
    return lambda flag, db: flag in on


@pytest.mark.asyncio
async def test_sync_without_the_flag_is_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and "join_paths" not in result and not (dbs / "pg_live" / PATHS_REL).exists()


@pytest.mark.asyncio
async def test_sync_with_the_flag_writes_the_file_and_adds_a_small_report(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_join_paths"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["join_paths"]["ok"] and (dbs / "pg_live" / PATHS_REL).exists()
    assert read(dbs)["generated_by"] == GENERATED_BY and set(result["join_paths"]) >= {"pairs", "routes", "links", "hubs", "file"}


@pytest.mark.asyncio
async def test_a_crash_in_the_phase_never_fails_the_sync(sqlite_db: Path, dbs: Path, monkeypatch):
    from unittest.mock import AsyncMock

    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_join_paths"))
    monkeypatch.setattr(pp, "run_paths_phase", AsyncMock(side_effect=RuntimeError("boom")))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["join_paths"]["ok"] is False and "boom" in result["join_paths"]["error"]


def test_the_flag_exists_is_off_in_the_shipped_defaults_and_the_template_has_no_paths_file():
    from src.utils import feature_flags as ff

    assert "auto_join_paths" in ff.DB_FEATURE_FLAGS
    cfg = yaml.safe_load((ROOT / "config" / "feature_flags.yaml").read_text(encoding="utf-8"))
    assert not cfg["db_defaults"]["auto_join_paths"] and cfg["db_features"]["_example_db"]["auto_join_paths"] is False
    assert not (ROOT / "databases" / "_template" / PATHS_REL).exists()
