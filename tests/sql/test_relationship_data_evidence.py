"""Release 2: aggregate-only validation of likely joins (``src/sql/relationships/data_evidence.py``).

A real SQLite database is used wherever a statement is executed, so the generated SQL is run, not just parsed. Nothing here names a
real customer database: the schemas are small synthetic ones.
"""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlglot
from sqlglot import exp

import src.sql.relationships.data_evidence as de
from src.sql import prompt_builder as pb
from src.sql.relationships.candidates import WEIGHTS, Edge, noisy_or
from src.sql.relationships.context import prepare
from src.sql.relationships.detectors import detect
from src.sql.relationships.gates import resolve
from src.sql.relationships.model import graph_from_documents
from src.sql.relationships.writer import (
    APPLIED_REL,
    HIGH_THRESHOLD,
    INFERRED_REL,
    input_key,
    select_applied,
    write_relationship_files,
)
from src.sql.sa import relationships_phase as rp
from tests.sql.relationships_fixtures import build, documents, proc_edge, table
from tests.sql.test_auto_procedures import (  # noqa: F401  (shared fixtures: a real SQLite file and the sync arguments)
    _sync_args,
    sqlite_db,
)
from tests.sql.test_catalog_extraction import (
    dbs,  # noqa: F401  (shared fixture: a temporary databases folder with a pg_live template)
)

INT, TXT = "INTEGER", "VARCHAR(30)"
DB = "pg_live"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------------------------- a real database to ask


def _shop_tables(*, status_rows: int = 3) -> tuple[dict[str, Any], ...]:
    return (
        table("region", {"id": INT, "name": TXT}, pk=("id",), rows=30),
        table("plant", {"id": INT, "region_id": INT, "name": TXT}, pk=("id",), rows=40),
        table("vendor", {"id": INT, "name": TXT}, pk=("id",), rows=50),
        table("status", {"id": INT, "name": TXT}, pk=("id",), rows=status_rows),
        table("orders", {"id": INT, "plant_id": INT, "vendor_id": INT, "status_id": INT, "note": TXT}, pk=("id",), rows=200),
        table("po", {"id": INT, "po_no": TXT}, pk=("id",), unique=(("po_no",),), rows=100),
        table("invoice", {"id": INT, "po_number": TXT}, pk=("id",), rows=100),
    )


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table region(id integer primary key, name text);
        create table plant(id integer primary key, region_id integer, name text);
        create table vendor(id integer primary key, name text);
        create table status(id integer primary key, name text);
        create table orders(id integer primary key, plant_id integer, vendor_id integer, status_id integer, note text);
        create table po(id integer primary key, po_no text unique);
        create table invoice(id integer primary key, po_number text);
        """
    )
    con.executemany("insert into region values (?, ?)", [(i, f"r{i}") for i in range(1, 31)])
    con.executemany("insert into plant values (?, ?, ?)", [(i, (i % 30) + 1, f"p{i}") for i in range(1, 41)])
    con.executemany("insert into vendor values (?, ?)", [(i, f"v{i}") for i in range(1, 51)])
    con.executemany("insert into status values (?, ?)", [(i, f"s{i}") for i in range(1, 4)])
    # plant_id exists in plant; vendor_id (1000+) exists in no vendor row; status_id has three values, all in status
    con.executemany("insert into orders values (?, ?, ?, ?, ?)", [(i, (i % 40) + 1, 1000 + (i % 80), (i % 3) + 1, None) for i in range(1, 201)])
    con.executemany("insert into po values (?, ?)", [(i, f"P{i}") for i in range(1, 101)])
    con.executemany("insert into invoice values (?, ?)", [(i, f"P{(i % 100) + 1}") for i in range(1, 101)])
    con.commit()
    con.close()


def _sqlite_runner(path: Path, log: list[str] | None = None):
    async def run(sql: str, *, max_rows: int, db_id: str) -> list[dict[str, Any]]:
        if log is not None:
            log.append(sql)

        def work() -> list[dict[str, Any]]:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only, like the application's own connector
            try:
                cur = con.execute(sql)
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row, strict=True)) for row in cur.fetchmany(max_rows)]
            finally:
                con.close()

        return await asyncio.to_thread(work)

    return run


def _graph(*tables: dict[str, Any], **kw: Any):
    schema_doc, inventory, evidence = documents(*tables, **kw)
    schema_doc["engine"] = "sqlite"
    graph = graph_from_documents(schema_doc, inventory, evidence, db_id=DB)
    assert graph is not None
    return graph


def _edges(graph):
    ctx = prepare(graph)
    edges, _ = resolve(ctx, detect(ctx))
    return edges


def _by_pair(edges: list[Edge]) -> dict[tuple[str, str, str, str], Edge]:
    return {e.key: e for e in edges}


def _write_catalog(dbs: Path, *tables: dict[str, Any], **kw: Any) -> None:
    schema_doc, inventory, evidence = documents(*tables, **kw)
    schema_doc["engine"] = "sqlite"
    folder = dbs / DB / "schema"
    (folder / "schema.json").write_text(json.dumps(schema_doc), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    if evidence is not None:
        (folder / "sp_evidence.json").write_text(json.dumps(evidence), encoding="utf-8")


def _read(dbs: Path, rel: str) -> Any:
    return json.loads((dbs / DB / rel).read_text(encoding="utf-8"))


def _pairs(doc: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    return {(r["from_table"], r["from_column"], r["to_table"], r["to_column"]) for r in doc["relationships"]}


ORDERS_PLANT = ("orders", "plant_id", "plant", "id")
ORDERS_VENDOR = ("orders", "vendor_id", "vendor", "id")
ORDERS_STATUS = ("orders", "status_id", "status", "id")
PLANT_REGION = ("plant", "region_id", "region", "id")
INVOICE_PO = ("invoice", "po_number", "po", "po_no")


# ----------------------------------------------------------------------------------------------- the statement


@pytest.mark.parametrize("dialect", ["mysql", "postgres", "tsql", "oracle", "sqlite"])
def test_the_probe_is_one_select_of_four_counts_in_every_dialect(dialect: str) -> None:
    g = build(*_shop_tables())
    child, parent = g.tables["orders"], g.tables["plant"]
    sql = de.build_probe_sql(child, "plant_id", parent, "id", dialect)
    tree = sqlglot.parse_one(sql, read=dialect)
    assert isinstance(tree, exp.Select)
    assert [s.alias for s in tree.expressions] == ["total_rows", "nonnull_rows", "child_distinct", "matched_distinct"]
    assert all(isinstance(s.this, exp.Count) for s in tree.expressions)  # counts only: no column or value is ever selected bare
    assert str(de.SAMPLE_ROWS) in sql  # the child side is bounded: LIMIT / TOP / FETCH FIRST


def test_identifiers_are_quoted_for_the_dialect_and_hostile_names_stay_inside_the_quotes() -> None:
    hostile = table('x"; DROP TABLE y; --', {"id": INT, "k": INT}, pk=("id",))
    g = build(hostile, table("p", {"id": INT}, pk=("id",)))
    child = g.tables['x"; DROP TABLE y; --']
    for dialect in ("mysql", "postgres", "tsql", "oracle", "sqlite"):
        sql = de.build_probe_sql(child, "k", g.tables["p"], "id", dialect)
        tree = sqlglot.parse_one(sql, read=dialect)
        assert isinstance(tree, exp.Select) and not tree.find(exp.Drop) and len(sqlglot.parse(sql, read=dialect)) == 1
    assert "`" in de.build_probe_sql(child, "k", g.tables["p"], "id", "mysql") and "[" in de.build_probe_sql(child, "k", g.tables["p"], "id", "tsql")


def test_the_probe_runs_and_returns_exactly_four_integers(tmp_path: Path) -> None:
    db = tmp_path / "shop.db"
    _make_db(db)
    g = build(*_shop_tables())
    sql = de.build_probe_sql(g.tables["orders"], "plant_id", g.tables["plant"], "id", "sqlite")
    rows = asyncio.run(_sqlite_runner(db)(sql, max_rows=1, db_id=DB))
    assert rows == [{"total_rows": 200, "nonnull_rows": 200, "child_distinct": 40, "matched_distinct": 40}]
    sql = de.build_probe_sql(g.tables["orders"], "vendor_id", g.tables["vendor"], "id", "sqlite")
    assert asyncio.run(_sqlite_runner(db)(sql, max_rows=1, db_id=DB)) == [{"total_rows": 200, "nonnull_rows": 200, "child_distinct": 80, "matched_distinct": 0}]


def test_counts_are_read_by_lower_case_name_and_inconsistent_results_are_refused() -> None:
    upper = [{"TOTAL_ROWS": 10, "NONNULL_ROWS": 9, "CHILD_DISTINCT": 5, "MATCHED_DISTINCT": 5}]  # Oracle upper-cases column names
    assert de._parse_counts(upper) == (10, 9, 5, 5)
    assert de._parse_counts([{"total_rows": "10", "nonnull_rows": "9", "child_distinct": "5", "matched_distinct": "4"}]) == (10, 9, 5, 4)
    for bad in (
        [], [{}], None, "x", [upper[0], upper[0]],
        [{"total_rows": 10, "nonnull_rows": 9, "child_distinct": 5, "matched_distinct": 6}],  # more matched than distinct
        [{"total_rows": 5, "nonnull_rows": 9, "child_distinct": 5, "matched_distinct": 5}],  # more non-null than rows
        [{"total_rows": -1, "nonnull_rows": 0, "child_distinct": 0, "matched_distinct": 0}],
        [{"total_rows": "x", "nonnull_rows": 1, "child_distinct": 1, "matched_distinct": 1}],
    ):
        assert de._parse_counts(bad) is None


# ----------------------------------------------------------------------------------------------- reading the counts


def _facts(total=1000, nonnull=1000, distinct=100, matched=100, parent_rows: int | None = 1000, unique: str | None = "primary") -> de.Facts:
    return de.Facts(total, nonnull, distinct, matched, unique, parent_rows)


def test_classification_boundaries() -> None:
    assert de.classify(_facts()) == "supported"
    assert de.classify(_facts(distinct=100, matched=98)) == "supported"  # 98% is the line
    assert de.classify(_facts(distinct=100, matched=97)) == "partial"  # orphans are common, but not proof
    assert de.classify(_facts(distinct=100, matched=49)) == "contradicted"
    assert de.classify(_facts(distinct=100, matched=50)) == "partial"
    assert de.classify(_facts(nonnull=29, distinct=29, matched=29)) == "insufficient"  # nearly empty
    assert de.classify(_facts(nonnull=0, distinct=0, matched=0)) == "insufficient"
    assert de.classify(_facts(distinct=1, matched=1)) == "insufficient"


def test_a_column_with_few_values_proves_a_key_only_when_it_covers_a_small_parent() -> None:
    # three values that all exist: chance against a big parent, informative against a three-row lookup table
    assert de.classify(_facts(total=200, nonnull=200, distinct=3, matched=3, parent_rows=3000)) == "partial"
    assert de.classify(_facts(total=200, nonnull=200, distinct=3, matched=3, parent_rows=3)) == "supported"
    assert de.classify(_facts(total=200, nonnull=200, distinct=3, matched=3, parent_rows=None)) == "partial"  # no estimate: not informative
    # a sample cut at the limit cannot say how much of the parent is used
    assert de.classify(_facts(total=de.SAMPLE_ROWS, nonnull=de.SAMPLE_ROWS, distinct=3, matched=3, parent_rows=3)) == "partial"
    assert de.classify(_facts(distinct=3, matched=0)) == "contradicted"


def test_derived_numbers() -> None:
    f = _facts(total=200, nonnull=150, distinct=150, matched=120)
    assert f.null_ratio == 0.25 and f.coverage == 0.8 and f.cardinality_estimate == "one_to_one"
    assert _facts(total=200, nonnull=200, distinct=40, matched=40).cardinality_estimate == "many_to_one"
    assert _facts(total=de.SAMPLE_ROWS, nonnull=de.SAMPLE_ROWS, distinct=de.SAMPLE_ROWS, matched=1).cardinality_estimate == "unknown"
    facts = f.as_dict()
    assert all(isinstance(v, (int, float, str, bool, type(None))) for v in facts.values())  # numbers and labels, nothing else


# ----------------------------------------------------------------------------------------------- the short list


def test_names_alone_never_publish_and_data_alone_cannot_either() -> None:
    assert noisy_or({"data_match": WEIGHTS["data_match"]}) < HIGH_THRESHOLD
    g = build(*_shop_tables())
    assert select_applied(g, _edges(g))[0] == []  # the names alone: the baseline publishes nothing
    # the same column name in two tables, no reference-shaped name: nothing is proposed, so nothing is shortlisted
    g2 = build(table("a", {"id": INT, "code": TXT}, pk=("id",), rows=100), table("b", {"id": INT, "code": TXT}, unique=(("code",),), pk=("id",), rows=100))
    assert de.shortlist(g2, _edges(g2)) == []


def test_the_short_list_is_only_name_proposed_edges_with_a_key_parent_and_a_usable_type() -> None:
    g = build(*_shop_tables())
    keys = {e.key for e in de.shortlist(g, _edges(g))}
    assert keys == {ORDERS_PLANT, ORDERS_VENDOR, ORDERS_STATUS, PLANT_REGION, INVOICE_PO}


def test_declared_and_procedure_joins_are_facts_and_are_never_queried() -> None:
    g = build(*_shop_tables(), fks=(ORDERS_PLANT,), proc=(proc_edge(*PLANT_REGION),))
    keys = {e.key for e in de.shortlist(g, _edges(g))}
    assert ORDERS_PLANT not in keys and PLANT_REGION not in keys and ORDERS_VENDOR in keys


def test_views_other_schemas_and_unknown_direction_are_not_queried() -> None:
    g = build(*_shop_tables(), table("v_orders", {"id": INT, "plant_id": INT}, kind="view"))
    assert all(e.child != "v_orders" for e in de.shortlist(g, _edges(g)))
    multi = build(*_shop_tables(), multi_schema=True)  # several schemas and no answering schema: nothing could be published anyway
    assert de.shortlist(multi, _edges(multi)) == []
    scoped = build(
        table("plant", {"id": INT}, pk=("id",), schema="a", rows=40), table("orders", {"id": INT, "plant_id": INT}, pk=("id",), schema="b", rows=200),
        multi_schema=True, answering_schema="a",
    )
    assert de.shortlist(scoped, _edges(scoped)) == []  # one end is outside the schema the prompts see


def test_the_short_list_is_ordered_for_promotion_first_and_bounded() -> None:
    g = build(*_shop_tables())
    edges = _edges(g)
    assert [e.key for e in de.shortlist(g, edges)][-1] == INVOICE_PO  # the lowest score last
    assert len(de.shortlist(g, edges, limit=2)) == 2
    soft_high = Edge("orders", "plant_id", "plant", "id", {"same_name": 0.55, "comment": 0.15, "junction": 0.20, "indexed": 0.05})
    assert soft_high.score >= HIGH_THRESHOLD
    ranked = de.shortlist(g, [*edges, soft_high])
    assert ranked[-1] is soft_high  # a soft edge already published can only be refuted: it goes after the ones data could promote


def test_a_date_or_unknown_typed_column_is_never_compared() -> None:
    g = build(table("t", {"id": INT, "ts_id": "TIMESTAMP"}, pk=("id",), rows=50), table("ts", {"id": "TIMESTAMP"}, pk=("id",), rows=50))
    assert de.shortlist(g, _edges(g)) == []


# ----------------------------------------------------------------------------------------------- applying what was found


def _entry(status: str) -> dict[str, Any]:
    return {"status": status, "checked_at": de._stamp(NOW), "facts": {"coverage": 1.0}}


def test_supported_data_lifts_a_name_edge_past_the_line_and_the_rest_leave_it_alone() -> None:
    for status, lifted in (("supported", True), ("partial", False), ("insufficient", False), ("error", False)):
        e = Edge("invoice", "po_number", "po", "po_no", {"code_like": 0.45})
        out = de.apply_evidence([e], {de.pair_key(e): _entry(status)})
        assert (e.score >= HIGH_THRESHOLD) is lifted and out["promoted"] == int(lifted)
        assert e.data_check == status and ("data_match" in e.evidence) is lifted


def test_contradicting_data_blocks_a_soft_edge_but_never_a_declared_or_procedure_one() -> None:
    g = build(*_shop_tables())
    soft = Edge("orders", "plant_id", "plant", "id", {"same_name": 0.55, "comment": 0.15, "junction": 0.20, "indexed": 0.05})
    assert select_applied(g, [soft])[0] == [soft]
    soft.data_check = "contradicted"
    chosen, held = select_applied(g, [soft])
    assert chosen == [] and held["data_contradicted"] == 1
    declared = Edge("orders", "plant_id", "plant", "id", {"declared": 1.0}, data_check="contradicted")
    proc = Edge("orders", "vendor_id", "vendor", "id", {"proc": 0.9}, data_check="contradicted")
    assert select_applied(g, [declared, proc])[0] == [declared, proc]


def test_data_can_choose_between_parents_only_when_it_refutes_every_other() -> None:
    def trio(*statuses: str) -> list[Edge]:
        edges = [Edge("c", "party_id", p, "id", {"same_name": 0.55}, ambiguous=True) for p in ("a", "b", "c2")[: len(statuses)]]
        de.apply_evidence(edges, {de.pair_key(e): _entry(s) for e, s in zip(edges, statuses, strict=True)})
        return edges

    one = trio("supported", "contradicted", "contradicted")
    assert [e.ambiguous for e in one] == [False, True, True]  # the supported one is no longer ambiguous
    assert [e.ambiguous for e in trio("supported", "supported")] == [True, True]  # two parents fit: still ambiguous
    assert [e.ambiguous for e in trio("supported", "partial")] == [True, True]  # a rival that is not refuted keeps it open
    assert [e.ambiguous for e in trio("supported", "insufficient")] == [True, True]
    assert [e.ambiguous for e in trio("contradicted", "contradicted")] == [True, True]


def test_the_decision_key_is_empty_without_checks_and_changes_with_them() -> None:
    e = Edge("a", "x_id", "b", "id", {"table_id": 0.6})
    assert de.decision_key([e]) == ""
    e.data_check = "supported"
    k1 = de.decision_key([e])
    e.data_check = "contradicted"
    assert k1 and de.decision_key([e]) not in ("", k1)


# ----------------------------------------------------------------------------------------------- collecting, with a real database


def _collect(graph, edges, runner, folder: Path, **kw: Any) -> de.Outcome:
    return asyncio.run(de.collect_evidence(DB, graph, edges, runner=runner, databases_dir=folder, now=kw.pop("now", NOW), **kw))


@pytest.fixture()
def shop(tmp_path: Path):
    db = tmp_path / "shop.db"
    _make_db(db)
    root = tmp_path / "dbs"
    (root / DB / "schema").mkdir(parents=True)
    return db, root


def test_the_data_decides_each_pair_and_lifts_only_the_proven_ones(shop) -> None:
    db, root = shop
    graph = _graph(*_shop_tables())
    edges = _edges(graph)
    seen: list[str] = []
    out = _collect(graph, edges, _sqlite_runner(db, seen), root)
    by = _by_pair(edges)
    assert by[ORDERS_PLANT].data_check == "supported" and by[ORDERS_PLANT].score >= HIGH_THRESHOLD  # 40 distinct values, all in plant
    assert by[PLANT_REGION].data_check == "supported"
    assert by[INVOICE_PO].data_check == "supported" and by[INVOICE_PO].score >= HIGH_THRESHOLD  # a business key: text codes, no "_id"
    assert by[ORDERS_VENDOR].data_check == "contradicted" and by[ORDERS_VENDOR].score < HIGH_THRESHOLD  # 80 values, none in vendor
    assert by[ORDERS_STATUS].data_check == "supported"  # 3 values that cover the whole 3-row lookup table
    assert (out.report["supported"], out.report["contradicted"], out.report["queried"], out.report["promoted"]) == (4, 1, 5, 4)
    chosen, held = select_applied(graph, edges)
    assert {e.key for e in chosen} == {ORDERS_PLANT, PLANT_REGION, INVOICE_PO, ORDERS_STATUS}
    assert held == {"below_threshold": 1}  # the contradicted edge stays out
    assert len(seen) == 5 and all(s.startswith("SELECT COUNT(*) AS total_rows") for s in seen)


def test_a_small_lookup_that_is_only_a_slice_of_a_big_parent_stays_unpublished(shop) -> None:
    db, root = shop
    graph = _graph(*_shop_tables(status_rows=3000))  # the catalog says status is large: three values prove nothing
    edges = _edges(graph)
    _collect(graph, edges, _sqlite_runner(db), root)
    assert _by_pair(edges)[ORDERS_STATUS].data_check == "partial"
    assert ORDERS_STATUS not in {e.key for e in select_applied(graph, edges)[0]}


def test_aggregates_only_nothing_that_looks_like_a_value_reaches_the_cache_file(shop) -> None:
    db, root = shop
    graph = _graph(*_shop_tables())
    _collect(graph, _edges(graph), _sqlite_runner(db), root)
    raw = (root / DB / de.EVIDENCE_REL).read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert doc["generated_by"] == de.GENERATED_BY and len(doc["entries"]) == 5
    for entry in doc["entries"].values():
        assert set(entry["facts"]) <= {
            "sample_rows", "truncated", "null_ratio", "child_distinct", "matched_distinct", "coverage", "parent_unique",
            "parent_rows_estimate", "parent_usage", "cardinality_estimate", "direction",
        }
    assert not any(token in raw for token in ("P1", "r1", "p1", "v1"))  # none of the values stored in the data tables


def test_an_unchanged_schema_costs_no_query_on_the_next_sync(shop) -> None:
    db, root = shop
    graph = _graph(*_shop_tables())
    _collect(graph, _edges(graph), _sqlite_runner(db), root)
    seen: list[str] = []
    again = _edges(graph)
    out = _collect(graph, again, _sqlite_runner(db, seen), root, now=NOW + timedelta(days=1))
    assert seen == [] and out.report["cached"] == 5 and out.report["queried"] == 0
    assert _by_pair(again)[ORDERS_PLANT].data_check == "supported"  # the cached decision is applied to the new edges


def test_a_changed_table_or_an_old_entry_is_checked_again_and_others_are_not(shop) -> None:
    db, root = shop
    graph = _graph(*_shop_tables())
    _collect(graph, _edges(graph), _sqlite_runner(db), root)
    changed = _graph(*(t if t["name"] != "plant" else table("plant", {"id": INT, "region_id": INT, "name": TXT, "extra": TXT}, pk=("id",), rows=41) for t in _shop_tables()))
    seen: list[str] = []
    _collect(changed, _edges(changed), _sqlite_runner(db, seen), root, now=NOW + timedelta(days=1))
    assert len(seen) == 2  # the two pairs that involve plant; the rest stay cached
    seen.clear()
    _collect(changed, _edges(changed), _sqlite_runner(db, seen), root, now=NOW + timedelta(days=de.CACHE_TTL_DAYS + 1))
    assert len(seen) == 5  # data drifts: everything is looked at again after the cache lifetime


def test_failures_are_remembered_briefly_and_by_class_name_only(shop) -> None:
    _db, root = shop
    graph = _graph(*_shop_tables())

    async def boom(sql: str, *, max_rows: int, db_id: str):
        raise RuntimeError("Data truncated for value 'ACME-SECRET-123'")  # a database message can quote a value

    edges = _edges(graph)
    out = _collect(graph, edges, boom, root)
    raw = (root / DB / de.EVIDENCE_REL).read_text(encoding="utf-8")
    assert "ACME-SECRET-123" not in raw and "RuntimeError" in raw and "ACME-SECRET-123" not in json.dumps(out.report)
    assert all(e.data_check == "error" for e in edges if e.key in {ORDERS_PLANT})
    assert all(e.score < HIGH_THRESHOLD for e in edges)  # an unreadable database proves nothing


def test_repeated_failures_stop_the_checks(shop, monkeypatch: pytest.MonkeyPatch) -> None:
    _db, root = shop
    monkeypatch.setattr(de, "MAX_CONSECUTIVE_ERRORS", 2)
    graph = _graph(*_shop_tables())
    calls = 0

    async def refuse(sql: str, *, max_rows: int, db_id: str):
        nonlocal calls
        calls += 1
        raise PermissionError

    out = _collect(graph, _edges(graph), refuse, root)
    assert calls == 2 and out.report["stopped"] == "repeated failures" and out.report["not_checked"] == 3


def test_the_query_count_and_candidate_budgets_are_respected(shop, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = shop
    graph = _graph(*_shop_tables())
    monkeypatch.setattr(de, "MAX_QUERIES", 2)
    seen: list[str] = []
    out = _collect(graph, _edges(graph), _sqlite_runner(db, seen), root)
    assert len(seen) == 2 and out.report["stopped"] == "query budget used" and out.report["not_checked"] == 3
    monkeypatch.setattr(de, "MAX_QUERIES", 150)
    monkeypatch.setattr(de, "MAX_CANDIDATES", 1)
    assert _collect(graph, _edges(graph), _sqlite_runner(db), root, now=NOW + timedelta(days=30)).report["shortlisted"] == 1


def test_the_time_budget_and_a_slow_query_are_respected(shop, monkeypatch: pytest.MonkeyPatch) -> None:
    _db, root = shop
    graph = _graph(*_shop_tables())

    async def slow(sql: str, *, max_rows: int, db_id: str):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(de, "QUERY_TIMEOUT_SECONDS", 0.05)
    edges = _edges(graph)
    out = _collect(graph, edges, slow, root)
    assert out.report["errors"] >= 1 and all(e.score < HIGH_THRESHOLD for e in edges)
    monkeypatch.setattr(de, "DATA_BUDGET_SECONDS", 0.0)
    out = _collect(graph, _edges(graph), slow, root, now=NOW + timedelta(days=2))
    assert out.report["stopped"] == "time budget used" and out.report["queried"] == 0


def test_a_file_the_phase_did_not_write_is_never_read_or_overwritten(shop) -> None:
    db, root = shop
    path = root / DB / de.EVIDENCE_REL
    path.write_text('{"mine": true}', encoding="utf-8")
    graph = _graph(*_shop_tables())
    out = _collect(graph, _edges(graph), _sqlite_runner(db), root)
    assert path.read_text(encoding="utf-8") == '{"mine": true}' and "left untouched" in out.report["cache"] and out.report["supported"] == 4


def test_an_engine_that_is_unknown_or_a_schema_with_nothing_to_check_asks_nothing(shop) -> None:
    db, root = shop
    seen: list[str] = []
    graph = _graph(*_shop_tables())
    graph.engine = "somethingelse"
    out = _collect(graph, _edges(graph), _sqlite_runner(db, seen), root)
    assert seen == [] and out.report["skipped"] == "the database engine is unknown"
    empty = _graph(table("a", {"id": INT}, pk=("id",)), table("b", {"id": INT}, pk=("id",)))
    out = _collect(empty, _edges(empty), _sqlite_runner(db, seen), root)
    assert seen == [] and out.report["skipped"] == "no pair needs a data check"


def test_the_clients_error_log_is_redacted_during_a_probe_and_nothing_else(shop, caplog: pytest.LogCaptureFixture) -> None:
    _db, root = shop
    graph = _graph(*_shop_tables())
    client_log = logging.getLogger("src.core.db_client")

    async def noisy(sql: str, *, max_rows: int, db_id: str):
        client_log.error("SQL execution failed: Data truncated for value 'ACME-SECRET-123'")  # what the real client logs on an error
        raise RuntimeError("x")

    with caplog.at_level(logging.ERROR, logger="src.core.db_client"):
        _collect(graph, _edges(graph), noisy, root)
        client_log.error("SQL execution failed: an ordinary user query failed")  # outside a probe: untouched
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ACME-SECRET-123" not in text and "details withheld" in text and "an ordinary user query failed" in text


# ----------------------------------------------------------------------------------------------- the phase and the files


def _flow(dbs: Path, db: Path, *, apply: bool = True, validate: bool = True, runner: Any = None, tables=None) -> dict[str, Any]:
    _write_catalog(dbs, *(tables or _shop_tables()))
    return asyncio.run(rp.run_relationships_phase(db_id=DB, apply=apply, validate=validate, runner=runner or _sqlite_runner(db), databases_dir=dbs))


@pytest.fixture()
def flow_env(dbs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs)
    db = tmp_path / "shop.db"
    _make_db(db)
    pb.clear_prompt_caches()
    yield dbs, db
    pb.clear_prompt_caches()


def test_without_the_flag_the_database_is_never_touched_and_nothing_is_published(flow_env) -> None:
    dbs, db = flow_env
    seen: list[str] = []
    report = _flow(dbs, db, validate=False, runner=_sqlite_runner(db, seen))
    assert seen == [] and "validation" not in report and report["applied_edges"] == 0 and report["medium"] == 5
    assert not (dbs / DB / de.EVIDENCE_REL).exists() and _read(dbs, APPLIED_REL) == {"relationships": []}


def test_with_the_flag_proven_name_edges_are_published_and_the_review_file_keeps_the_counts(flow_env) -> None:
    dbs, db = flow_env
    report = _flow(dbs, db)
    assert report["validation"]["supported"] == 4 and report["validation"]["contradicted"] == 1 and report["validation"]["promoted"] == 4
    assert report["applied_edges"] == 4 and report["high"] == 4 and report["held_back"] == {"below_threshold": 1}
    assert _pairs(_read(dbs, APPLIED_REL)) == {ORDERS_PLANT, PLANT_REGION, INVOICE_PO, ORDERS_STATUS}
    review = {(r["from_table"], r["from_column"], r["to_table"], r["to_column"]): r for r in _read(dbs, INFERRED_REL)["relationships"]}
    assert review[ORDERS_VENDOR]["data_check"] == "contradicted" and review[ORDERS_VENDOR]["publishable"] is False
    assert review[ORDERS_PLANT]["data_check"] == "supported" and review[ORDERS_PLANT]["publishable"] is True
    assert review[ORDERS_PLANT]["data_evidence"]["coverage"] == 1.0
    assert {"rule": "data_match", "weight": 0.55} in review[ORDERS_PLANT]["evidence"]
    assert _read(dbs, INFERRED_REL)["stats"]["data_supported"] == 4


def test_the_prompt_facing_file_never_carries_the_data_facts(flow_env) -> None:
    dbs, db = flow_env
    _flow(dbs, db)
    applied = _read(dbs, APPLIED_REL)
    assert all("data_check" not in r and "data_evidence" not in r for r in applied["relationships"])
    assert len(pb.get_raw_relationships(DB)) == 4  # and the prompt layer sees the published edges after the caches were cleared


def test_review_mode_shows_what_would_be_published_without_publishing_it(flow_env) -> None:
    dbs, db = flow_env
    report = _flow(dbs, db, apply=False)
    assert report["mode"] == "review" and report["applied_edges"] == 0 and _read(dbs, APPLIED_REL) == {"relationships": []}
    assert sum(1 for r in _read(dbs, INFERRED_REL)["relationships"] if r["publishable"]) == 4


def test_a_database_that_cannot_be_read_leaves_exactly_the_old_result(flow_env) -> None:
    dbs, db = flow_env

    async def down(sql: str, *, max_rows: int, db_id: str):
        raise ConnectionError

    without = _flow(dbs, db, validate=False)
    base = _pairs(_read(dbs, APPLIED_REL))
    with_failure = _flow(dbs, db, runner=down)
    assert with_failure["validation"]["errors"] >= 1 and _pairs(_read(dbs, APPLIED_REL)) == base == set()
    assert with_failure["medium"] == without["medium"] and with_failure["applied_edges"] == without["applied_edges"]


def test_a_crash_in_the_checks_never_fails_the_phase(flow_env, monkeypatch: pytest.MonkeyPatch) -> None:
    dbs, db = flow_env
    monkeypatch.setattr(de, "collect_evidence", AsyncMock(side_effect=RuntimeError("boom")))
    report = _flow(dbs, db)
    assert report["ok"] is True and report["validation"] == {"ok": False, "error": "RuntimeError"} and report["applied_edges"] == 0


def test_a_curated_relationships_file_is_still_never_replaced(flow_env) -> None:
    dbs, db = flow_env
    curated = {"relationships": [{"from_table": "orders", "from_column": "plant_id", "to_table": "plant", "to_column": "id"}]}
    (dbs / DB / "schema" / "relationships.json").write_text(json.dumps(curated), encoding="utf-8")
    report = _flow(dbs, db)
    assert _read(dbs, APPLIED_REL) == curated and "curated" in json.dumps(report["files"])


def test_declared_keys_are_published_as_before_and_never_queried(flow_env) -> None:
    dbs, db = flow_env
    seen: list[str] = []
    _write_catalog(dbs, *_shop_tables(), fks=(ORDERS_PLANT,))
    asyncio.run(rp.run_relationships_phase(db_id=DB, apply=True, validate=True, runner=_sqlite_runner(db, seen), databases_dir=dbs))
    assert ORDERS_PLANT in _pairs(_read(dbs, APPLIED_REL)) and all("plant_id" not in s for s in seen)


def test_a_second_sync_with_nothing_new_rewrites_nothing_and_asks_nothing(flow_env) -> None:
    dbs, db = flow_env
    _flow(dbs, db)
    before = (dbs / DB / INFERRED_REL).read_bytes(), (dbs / DB / APPLIED_REL).read_bytes()
    seen: list[str] = []
    report = _flow(dbs, db, runner=_sqlite_runner(db, seen))
    assert seen == [] and report["files"]["unchanged"] == [INFERRED_REL, APPLIED_REL]
    assert (dbs / DB / INFERRED_REL).read_bytes(), before and (dbs / DB / INFERRED_REL).read_bytes() == before[0]


def test_trusted_edges_published_from_data_become_multi_hop_routes_automatically(flow_env) -> None:
    from src.sql.sa.paths_phase import run_paths_phase

    dbs, db = flow_env
    _flow(dbs, db)
    report = asyncio.run(run_paths_phase(db_id=DB, databases_dir=dbs))
    assert report["ok"] is True
    routes = json.dumps(_read(dbs, "schema/join_paths.json"))
    assert "orders" in routes and "region" in routes and "plant" in routes  # orders -> plant -> region exists only through the two data-proven edges


def test_a_contradicted_edge_never_reaches_route_construction(flow_env) -> None:
    from src.sql.sa.paths_phase import run_paths_phase

    dbs, db = flow_env
    _flow(dbs, db)
    asyncio.run(run_paths_phase(db_id=DB, databases_dir=dbs))
    routes = json.dumps(_read(dbs, "schema/join_paths.json"))
    assert "vendor" not in routes  # the only edge to vendor was refuted by the data


def test_the_timeout_semantics_of_the_cpu_stages_are_unchanged(flow_env, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    dbs, db = flow_env
    real = rp.detect
    monkeypatch.setattr(rp, "detect", lambda ctx: (time.sleep(0.4), real(ctx))[1])
    seen: list[str] = []
    _write_catalog(dbs, *_shop_tables())
    report = asyncio.run(rp.run_relationships_phase(db_id=DB, apply=True, validate=True, runner=_sqlite_runner(db, seen), databases_dir=dbs, budget_seconds=0.05))
    assert report["ok"] is False and "time budget" in report["skipped"] and seen == []

    async def settle() -> None:
        await asyncio.sleep(0.8)

    asyncio.run(settle())
    assert not (dbs / DB / INFERRED_REL).exists()


# ----------------------------------------------------------------------------------------------- the writer keys and the flag


def test_the_input_key_is_exactly_the_old_one_without_data_and_new_with_it() -> None:
    import hashlib

    from src.sql.relationships.writer import MAX_INFERRED_EDGES, RULESET_VERSION

    g = build(*_shop_tables())
    old = hashlib.sha256(json.dumps([g.inputs_hash, RULESET_VERSION, "inferred", 0.7, 40, MAX_INFERRED_EDGES]).encode("utf-8")).hexdigest()[:16]
    assert input_key(g, "inferred", 0.7, 40) == old == input_key(g, "inferred", 0.7, 40, "")
    assert input_key(g, "inferred", 0.7, 40, "abc") != old


def test_edges_the_data_never_saw_write_the_same_files_as_before(dbs: Path) -> None:
    g = build(*_shop_tables())
    edges = _edges(g)
    (dbs / DB / "schema").mkdir(parents=True, exist_ok=True)
    write_relationship_files(DB, g, edges, {}, apply=True, databases_dir=dbs)
    doc = _read(dbs, INFERRED_REL)
    assert all("data_check" not in r for r in doc["relationships"]) and not any(k.startswith("data_") for k in doc["stats"])


def test_the_flag_is_registered_off_by_default_and_changes_the_resync_plan() -> None:
    from src.sql.sync import resync
    from src.utils.feature_flags import DB_FEATURE_FLAGS, DEFAULT_NEW_DB_FLAGS, OFF_BY_DEFAULT_FLAGS

    assert "auto_relationships_data_apply" in DB_FEATURE_FLAGS and "auto_relationships_data_apply" in OFF_BY_DEFAULT_FLAGS
    assert DEFAULT_NEW_DB_FLAGS["auto_relationships_data_apply"] is False
    assert ("auto_relationships_data_apply", "relationships_data_apply") in resync._SWITCHES  # switching it changes the sync plan: no stale skip


@pytest.mark.asyncio
@pytest.mark.parametrize(("flags", "expected"), [
    (("auto_catalog", "auto_relationships"), None),
    (("auto_catalog", "auto_relationships", "auto_relationships_data_apply"), True),
    (("auto_catalog", "auto_relationships_data_apply"), "never_called"),  # the validation flag alone does nothing: Phase 6 itself is off
])
async def test_the_sync_asks_for_validation_only_when_the_flag_is_on(sqlite_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch, flags, expected) -> None:
    from src.sql.schema_retrieval import sync_live_schema

    fake = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(rp, "run_relationships_phase", fake)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in flags)
    result = await sync_live_schema(db_id=DB, **_sync_args())
    assert result["status"] == "ok"
    if expected == "never_called":
        fake.assert_not_called()
        return
    fake.assert_awaited_once()
    kwargs = fake.await_args.kwargs
    assert kwargs["db_id"] == DB and kwargs["apply"] is False
    assert kwargs.get("validate") is expected  # absent (None) unless the flag is on: every existing call keeps its exact shape
