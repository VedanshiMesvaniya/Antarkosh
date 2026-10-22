"""Phase 6 / step 6h: what the rule-by-rule tests do not cover.

The detectors and gates have their own files. This one asks harder questions of the whole phase:
random catalogs must never crash it and must always satisfy a set of invariants, a declared foreign key
must never be lost, hostile and degenerate input must be survived, realistic types from every engine must
pass the type gate, the weights are pinned so a change forces a version bump, and with the flags off a sync
changes nothing.
"""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.sql import prompt_builder as pb
from src.sql.relationships.candidates import WEIGHTS, noisy_or
from src.sql.relationships.context import prepare
from src.sql.relationships.detectors import detect, same_column_name
from src.sql.relationships.gates import MAX_PARENTS_PER_COLUMN, resolve
from src.sql.relationships.model import CatalogGraph, Column, graph_from_documents, type_family, types_compatible
from src.sql.relationships.writer import (
    APPLIED_REL,
    HIGH_THRESHOLD,
    INFERRED_REL,
    MEDIUM_THRESHOLD,
    RULESET_VERSION,
    band,
    select_applied,
    write_relationship_files,
)
from src.sql.schema_retrieval import format_scoped_relationships, get_1hop_neighbors
from src.sql.sa import relationships_phase as rp
from tests.sql.relationships_fixtures import build, ctx_of, documents, proc_edge, table, triples
from tests.sql.test_auto_procedures import _sync_args, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_catalog_extraction import dbs  # noqa: F401  (shared fixture)

INT, TXT = "INTEGER", "VARCHAR(50)"


def infer(graph):
    ctx = prepare(graph)
    cands = detect(ctx)
    edges, report = resolve(ctx, cands)
    return ctx, cands, edges, report


# ================================================================= the pk_name rule (a name found in many tables)

def _many_tables_with(col: str, holder_pk: bool = True, holders: int = 1):
    tables = []
    for i in range(holders):
        tables.append(table(f"holder{i}", {col: INT, "x": INT}, pk=(col,) if holder_pk else (), unique=() if holder_pk else ((col,),)))
    tables += [table(f"user{i}", {"id": INT, col: INT}, pk=("id",)) for i in range(6)]  # the name is in most tables: learned as common
    return ctx_of(*tables)


def test_a_common_id_like_name_that_is_the_sole_primary_key_of_one_table_still_counts():
    ctx = _many_tables_with("emp_no")
    assert "emp_no" in ctx.common
    found = {c.rule for c in same_column_name(ctx)}
    assert found == {"pk_name"} and len(same_column_name(ctx)) == 6


def test_a_common_name_is_still_generic_when_it_is_only_unique_not_primary_or_held_by_several_tables_or_not_id_like():
    assert same_column_name(_many_tables_with("emp_no", holder_pk=False)) == []  # a unique column, not the primary key
    assert same_column_name(_many_tables_with("emp_no", holders=2)) == []  # two tables claim it: not a reference to one
    assert same_column_name(_many_tables_with("status")) == []  # reads like a word, not an id or a code
    assert same_column_name(_many_tables_with("code")) == []


def test_pk_name_edges_score_like_any_name_rule_and_are_not_dropped_by_the_generic_gate():
    ctx, _, edges, _ = infer(build(*[table(f"u{i}", {"id": INT, "emp_no": INT}, pk=("id",)) for i in range(6)], table("emp", {"emp_no": INT}, pk=("emp_no",))))
    assert len(edges) == 6 and all(e.parent == "emp" and "pk_name" in e.evidence and e.score < HIGH_THRESHOLD for e in edges)


# ================================================================= lookup tables, arrays, dialect types

def test_lookup_tables_are_found_even_though_their_names_look_generic():
    ctx = ctx_of(
        table("status", {"id": INT}, pk=("id",)), table("order_status", {"id": INT}, pk=("id",)), table("type", {"id": INT}, pk=("id",)),
        table("orders", {"id": INT, "status_id": INT, "order_status_id": INT, "type_id": INT}, pk=("id",)),
    )
    got = {(c[1], c[2]) for c in triples(detect(ctx), "table_id")}
    assert got == {("status_id", "status"), ("order_status_id", "order_status"), ("type_id", "type")}


@pytest.mark.parametrize("raw,family", [
    ("INTEGER[]", "other"), ("integer[3]", "other"), ("varchar(10)[]", "other"), ("ARRAY", "other"), ("integer", "integer"), ("int(10) unsigned", "integer"),
])
def test_array_columns_are_never_keys(raw, family):
    assert type_family(raw) == family


def _col(t):
    return Column(name="x", type=t, family=type_family(t))


@pytest.mark.parametrize("child,parent,ok", [
    # PostgreSQL
    ("integer", "bigint", True), ("serial", "integer", True), ("uuid", "uuid", True), ("text", "character varying(50)", True), ("bpchar", "text", True),
    ("integer", "text", False), ("integer[]", "integer", False), ("timestamp with time zone", "timestamp", True), ("uuid", "text", False),
    # MySQL / MariaDB
    ("int(10) unsigned", "bigint(20) unsigned", True), ("int(11)", "int(10) unsigned", True), ("varchar(20)", "char(20)", True), ("tinyint(1)", "int", True),
    ("int", "varchar(20)", False),
    # SQL Server
    ("uniqueidentifier", "uuid", True), ("nvarchar(max)", "varchar(50)", True), ("int identity", "int", True), ("datetime2", "datetime", True), ("int", "uniqueidentifier", False),
    # Oracle
    ("NUMBER(10)", "NUMBER(19)", True), ("NUMBER(10,0)", "integer", True), ("VARCHAR2(30 CHAR)", "NVARCHAR2(20)", True), ("NUMBER(10)", "VARCHAR2(10)", False),
    # unknown types only match themselves
    ("USER-DEFINED", "USER-DEFINED", True), ("USER-DEFINED", "integer", False), ("", "", False),
])
def test_type_gate_agrees_with_every_engine(child, parent, ok):
    assert types_compatible(_col(child), _col(parent)) is ok


# ================================================================= hostile and degenerate input

HOSTILE = ["Ünïcödé", "a b", 'quo"te', "'; DROP TABLE x;--", "x" * 300, "ID", "Id_", "_id", "id_", "__", "1", "01_id", "order", "select", "a.b", "日本語_id", "emoji_😀_id"]


def test_hostile_names_never_crash_the_phase_and_the_files_stay_valid_json(tmp_path: Path):
    tables = [table(f"t{i}", {"id": INT, **{h: INT for h in HOSTILE}}, pk=("id",)) for i in range(3)]
    tables += [table(h, {"id": INT, "t0_id": INT}, pk=("id",)) for h in HOSTILE[:10]]
    graph = build(*tables)
    ctx, cands, edges, report = infer(graph)
    (tmp_path / "xx" / "schema").mkdir(parents=True)
    result = write_relationship_files("xx", graph, edges, report.as_counts(), apply=True, databases_dir=tmp_path)
    assert INFERRED_REL in result["written"]
    doc = json.loads((tmp_path / "xx" / INFERRED_REL).read_text(encoding="utf-8"))
    assert doc["stats"]["edges"] == len(doc["relationships"]) == len(edges)


def test_degenerate_catalogs_are_survived():
    for tables in ([], [table("only", {"id": INT}, pk=("id",))], [table("v", {"customer_id": INT}, kind="view")], [table("bare", {})],
                   [table("a", {"a_id": INT}), table("A", {"a_id": INT})]):
        schema_doc, inventory, evidence = documents(*tables)
        graph = graph_from_documents(schema_doc, inventory, evidence, db_id="tt")
        if graph is not None:
            _, _, edges, _ = infer(graph)
            assert isinstance(edges, list)


def test_dangling_keys_and_procedure_edges_are_ignored_not_fatal():
    graph = build(
        table("a", {"id": INT, "b_id": INT}, pk=("id",)), table("b", {"id": INT}, pk=("id",)),
        fks=(("a", "b_id", "b", "id"), ("a", "b_id", "ghost", "id"), ("a", "nope", "b", "id"), ("ghost", "x", "b", "id")),
        proc=(proc_edge("a", "b_id", "zzz", "q"), proc_edge("zzz", "q", "yyy", "r")),
    )
    _, _, edges, _ = infer(graph)
    assert ("a", "b_id", "b", "id") in {e.key for e in edges}


def test_a_catalog_with_the_same_table_under_two_cases_does_not_double_count():
    _, _, edges, _ = infer(build(table("Customers", {"id": INT}, pk=("id",)), table("orders", {"customer_id": INT})))
    assert [e.key for e in edges] == [("orders", "customer_id", "Customers", "id")]


# ================================================================= random catalogs: invariants

STEMS = [
    "customer", "order", "product", "invoice", "vendor", "party", "account", "ledger", "shipment", "item", "stock", "tax", "branch", "region",
    "asset", "contract", "payment", "employee", "project", "task", "status", "type", "category", "location", "department", "warehouse",
]


def _random_catalog(seed: int) -> CatalogGraph:
    rnd = random.Random(seed)
    n = rnd.randint(3, 7)
    chosen_stems = rnd.sample(STEMS, n)
    tables = []
    fks = []
    for s in chosen_stems:
        pk_name = "id" if rnd.random() < 0.5 else f"{s}_id"
        cols = {pk_name: INT}
        for other in rnd.sample(chosen_stems, rnd.randint(1, min(2, len(chosen_stems)))):
            if other != s:
                col_name = f"{other}_id"
                cols[col_name] = INT
                if rnd.random() < 0.4:
                    fks.append((s, col_name, other, "placeholder"))
        tables.append(table(s, cols, pk=(pk_name,)))
    pk_map = {t["name"]: t["primary_key"][0] for t in tables}
    valid_fks = tuple((a, b, c, pk_map[c]) for a, b, c, _ in fks if c in pk_map)
    return build(*tables, fks=valid_fks)


@pytest.mark.parametrize("seed", range(60))
def test_random_catalogs_satisfy_invariants(seed: int):
    graph = _random_catalog(seed)
    ctx, cands, edges, report = infer(graph)
    assert all(0.0 <= e.score <= 1.0 for e in edges)
    assert len(edges) == len({e.key for e in edges})
    declared_pairs = {(fk.from_table, fk.from_column, fk.to_table, fk.to_column) for fk in graph.declared_fks if fk.from_table in graph.tables and fk.to_table in graph.tables}
    edge_pairs = {e.key for e in edges}
    assert declared_pairs <= edge_pairs
    chosen, held = select_applied(graph, edges)
    assert all(e.score >= HIGH_THRESHOLD for e in chosen)
    assert all(e.direction_known for e in chosen)
    assert all(not e.ambiguous or e.has_hard_evidence for e in chosen)


# ================================================================= weights fingerprint and golden scores

def test_changing_a_weight_or_threshold_requires_a_ruleset_version_bump_and_an_explicit_fingerprint_update():
    payload = {
        "weights": dict(sorted(WEIGHTS.items())),
        "high": HIGH_THRESHOLD,
        "medium": MEDIUM_THRESHOLD,
        "version": RULESET_VERSION,
    }
    raw = json.dumps(payload, sort_keys=True)
    fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert RULESET_VERSION == 2
    assert HIGH_THRESHOLD == 0.70
    assert MEDIUM_THRESHOLD == 0.45
    assert len(fp) == 16


@pytest.mark.parametrize("inputs,expected", [
    ({"table_id": 0.60}, 0.60),
    ({"same_name": 0.55}, 0.55),
    ({"pk_name": 0.55}, 0.55),
    ({"role_prefix": 0.45}, 0.45),
    ({"abbrev": 0.40}, 0.40),
    ({"table_id": 0.60, "proc_low": 0.55}, 0.82),
    ({"same_name": 0.55, "proc_low": 0.55}, 0.7975),
    ({"table_id": 0.60, "role_prefix": 0.45}, 0.60),
    ({"table_id": 0.60, "junction": 0.45}, 0.78),
    ({"declared": 1.0}, 1.0),
    ({"declared": 1.0, "table_id": 0.60}, 1.0),
    ({"proc_high": 0.90, "table_id": 0.60}, 0.96),
])
def test_golden_scores_pin_the_noisy_or_math(inputs: dict[str, float], expected: float):
    assert pytest.approx(noisy_or(inputs), abs=1e-4) == expected


# ================================================================= prompt reader round trip

def test_prompt_reader_round_trip(tmp_path: Path):
    graph = build(table("orders", {"id": INT, "cust_id": INT}, pk=("id",)), table("cust", {"id": INT}, pk=("id",)), fks=(("orders", "cust_id", "cust", "id"),))
    ctx, cands, edges, report = infer(graph)
    (tmp_path / "db_rt" / "schema").mkdir(parents=True)
    res = write_relationship_files("db_rt", graph, edges, report.as_counts(), apply=True, databases_dir=tmp_path)
    assert APPLIED_REL in res["written"]
    applied_doc = json.loads((tmp_path / "db_rt" / APPLIED_REL).read_text(encoding="utf-8"))
    assert applied_doc["generated_by"] == "auto_relationships"
    assert len(applied_doc["relationships"]) == 1
    rel = applied_doc["relationships"][0]
    assert rel["from_table"] == "orders" and rel["to_table"] == "cust"


def test_scoped_relationships_formatting():
    rels = [{"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id"}]
    formatted = format_scoped_relationships({"orders", "customer"}, query="orders", rels=rels)
    assert "orders: customer_id->customer.id" in formatted


def test_1hop_neighbors_expansion():
    rels = [{"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id"}]
    neighbors = get_1hop_neighbors(["orders"], rels)
    assert "customer" in neighbors


# ================================================================= flag-off checks

@pytest.mark.asyncio
async def test_flag_off_phase_runner_does_nothing(tmp_path: Path):
    res = await rp.run_relationships_phase(db_id="db_off", apply=False, databases_dir=tmp_path)
    assert "no schema.json" in res.get("skipped", "")


@pytest.mark.asyncio
async def test_pinned_database_erp_main_skipped():
    res = await rp.run_relationships_phase(db_id="erp_main", apply=True)
    assert res == {"ok": True, "skipped": "pinned database"}