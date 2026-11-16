# ruff: noqa: F811
"""Empty results: disconnected joins are repaired with unverified links, empties are not cached, stale windows are explained."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.sql import generation
from src.sql import schema_retrieval as sr
from src.sql.pipeline import SQLRetriever
from src.sql.repair import advisory_links as adv
from src.sql.safety import data_recency as dr
from src.sql.safety.empty_result_classifier import (
    SUSPICIOUS_EMPTY,
    VALID_EMPTY,
    classify_empty_result,
)
from tests.sql.test_sql_retrieval import _router_returning, live_db  # noqa: F401  (shared fixtures)

JOIN_SQL = "SELECT SUM(a.price) FROM sqts_cost_recovery a JOIN gen_supplier_codes g ON a.qn_number = g.prinbr WHERE a.deleted IS NULL"


# ================================================================= classifier


def test_a_filtered_join_is_valid_empty_as_before_and_suspicious_once_the_validator_saw_no_relationship():
    assert classify_empty_result(JOIN_SQL, "tsql") == VALID_EMPTY
    assert classify_empty_result(JOIN_SQL, "tsql", disconnected_tables=None) == VALID_EMPTY
    assert classify_empty_result(JOIN_SQL, "tsql", disconnected_tables=[]) == VALID_EMPTY
    assert classify_empty_result(JOIN_SQL, "tsql", disconnected_tables=["gen_supplier_codes"]) == SUSPICIOUS_EMPTY


def test_a_query_without_a_join_is_never_made_suspicious_by_the_validator_list():
    sql = "SELECT * FROM sqts_cost_recovery WHERE deleted IS NULL"
    assert classify_empty_result(sql, "tsql", disconnected_tables=["sqts_cost_recovery"]) == VALID_EMPTY


def test_disconnected_tables_are_read_from_failed_validation_results_only():
    failed = SimpleNamespace(passed=False, context={"isolated_tables": ["a", "b"], "unreachable": ["c"]})
    fine = SimpleNamespace(passed=True, context={"isolated_tables": ["z"]})
    assert generation._disconnected_tables([failed, fine]) == ["a", "b", "c"]
    assert generation._disconnected_tables([]) == [] and generation._disconnected_tables(None) == []
    assert generation._disconnected_tables([object()]) == []


# ================================================================= advisory links


def edge(ft, fc, tt, tc, conf=0.55, band="medium", **kw):
    return {"from_table": ft, "from_column": fc, "to_table": tt, "to_column": tc, "confidence": conf, "band": band,
            "direction_known": True, "ambiguous": False, "in_answering_scope": True, "publishable": False, **kw}


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "shop" / "schema").mkdir(parents=True)
    adv.clear_cache()
    monkeypatch.setattr(sr, "_route_bridge_mode", lambda db_id: "apply")
    return tmp_path


def write_inferred(dbs: Path, edges: list[dict], generated_by: str = "auto_relationships") -> None:
    (dbs / "shop" / "schema" / "inferred_relationships.json").write_text(
        json.dumps({"generated_by": generated_by, "relationships": edges}), encoding="utf-8"
    )
    adv.clear_cache()


def links(dbs, lone, used, **kw):
    return adv.build_advisory_links("shop", lone, used, databases_dir=dbs, **kw)


def test_a_bridge_table_is_suggested_when_two_query_tables_have_no_direct_link(dbs: Path):
    write_inferred(dbs, [edge("sqts_cost_recovery", "qn_number", "sqts_main_lear", "qn_number"), edge("sqts_main_lear", "prinbr", "gen_supplier_codes", "prinbr")])
    out = links(dbs, ["gen_supplier_codes", "sqts_cost_recovery"], ["sqts_cost_recovery", "gen_supplier_codes"])
    assert out.startswith(adv.HEADER)
    assert "- sqts_cost_recovery.qn_number = sqts_main_lear.qn_number" in out and "- sqts_main_lear.prinbr = gen_supplier_codes.prinbr" in out


def test_a_direct_inferred_link_is_preferred_over_a_longer_path(dbs: Path):
    write_inferred(dbs, [edge("a", "x", "b", "x", 0.5), edge("a", "y", "c", "y", 0.6), edge("c", "z", "b", "z", 0.6)])
    out = links(dbs, ["a"], ["a", "b"])
    assert out.splitlines()[1:] == ["- a.x = b.x"]


@pytest.mark.parametrize("bad", [
    edge("a", "x", "b", "x", 0.30, "low"),
    edge("a", "x", "b", "x", ambiguous=True),
    edge("a", "x", "b", "x", direction_known=False),
    edge("a", "x", "b", "x", in_answering_scope=False),
    edge("a", "x", "a", "y"),
])
def test_weak_ambiguous_or_out_of_scope_edges_are_never_suggested(dbs: Path, bad):
    write_inferred(dbs, [bad])
    assert links(dbs, ["a"], ["a", "b"]) == ""


def test_nothing_is_added_outside_apply_mode_for_erp_main_or_without_usable_files(dbs: Path, monkeypatch: pytest.MonkeyPatch):
    write_inferred(dbs, [edge("a", "x", "b", "x")])
    assert links(dbs, ["a"], ["a", "b"]) != ""
    monkeypatch.setattr(sr, "_route_bridge_mode", lambda db_id: "shadow")
    assert links(dbs, ["a"], ["a", "b"]) == ""
    monkeypatch.setattr(sr, "_route_bridge_mode", lambda db_id: "apply")
    assert adv.build_advisory_links("erp_main", ["a"], ["a", "b"], databases_dir=dbs) == ""
    write_inferred(dbs, [edge("a", "x", "b", "x")], generated_by="someone_else")
    assert links(dbs, ["a"], ["a", "b"]) == ""
    (dbs / "shop" / "schema" / "inferred_relationships.json").write_text("{broken", encoding="utf-8")
    adv.clear_cache()
    assert links(dbs, ["a"], ["a", "b"]) == ""
    assert adv.build_advisory_links("ghost", ["a"], ["a", "b"], databases_dir=dbs) == ""
    assert links(dbs, [], ["a", "b"]) == "" and links(dbs, ["a"], ["a"]) == ""


def test_the_suggestion_list_is_capped(dbs: Path):
    write_inferred(dbs, [edge("hub", f"c{i}", f"t{i}", f"c{i}") for i in range(10)])
    out = links(dbs, ["hub"], ["hub", *[f"t{i}" for i in range(10)]], max_lines=3)
    assert len(out.splitlines()) == 1 + 3


def test_the_repair_context_adds_the_links_only_when_tables_are_disconnected(dbs: Path, monkeypatch: pytest.MonkeyPatch):
    write_inferred(dbs, [edge("a", "x", "b", "x")])
    monkeypatch.setattr(generation, "_repair_join_context", lambda *a, **k: "KNOWN")
    retriever = SimpleNamespace(db_id="shop")
    monkeypatch.setattr(adv, "_load_adjacency", lambda db_id, databases_dir=None: {"a": [("b", "x", "x", 0.55)], "b": [("a", "x", "x", 0.55)]})
    assert generation._advisory_join_context(retriever, ["a", "b"], {}, []) == "KNOWN"
    both = generation._advisory_join_context(retriever, ["a", "b"], {}, ["a"])
    assert both.startswith("KNOWN") and "- a.x = b.x" in both


# ================================================================= caches (real SQLite)


@pytest.mark.asyncio
async def test_an_empty_answer_is_not_cached_but_a_real_one_still_is(live_db):
    empty_sql = "SELECT id FROM customers WHERE name = 'Nobody'"
    router = _router_returning(empty_sql)
    calls = []
    inner = router.chat

    async def counting(*a, **k):
        calls.append(1)
        return await inner(*a, **k)

    router.chat = counting
    retriever = SQLRetriever(router)
    await retriever.retrieve("find nobody")
    assert retriever.last_query_status == "empty_result" and len(SQLRetriever._result_cache) == 0
    first = len(calls)
    await retriever.retrieve("find nobody")
    assert len(calls) > first  # asked again: not served from the cache

    good = SQLRetriever(_router_returning("SELECT name FROM customers"))
    await good.retrieve("list customers")
    assert len(SQLRetriever._result_cache) == 1


# ================================================================= data recency


def test_relative_windows_are_found_in_every_dialect_and_fixed_dates_are_not():
    cases = {
        "tsql": "SELECT 1 FROM qc_main M WHERE M.qc_date >= DATEADD(DAY, -90, CAST(GETDATE() AS DATE))",
        "mysql": "SELECT 1 FROM t WHERE created_at >= NOW() - INTERVAL 30 DAY",
        "postgres": "SELECT 1 FROM t WHERE created_at BETWEEN CURRENT_DATE - INTERVAL '90 days' AND CURRENT_DATE",
        "oracle": "SELECT 1 FROM t WHERE created_at >= SYSDATE - 90",
        "sqlite": "SELECT 1 FROM t WHERE created_at >= date('now','-90 day')",
    }
    for dialect, sql in cases.items():
        found = dr.relative_date_columns(sql, dialect)
        assert [c for _, c in found] and found[0][1] in {"qc_date", "created_at"}, dialect
    assert dr.relative_date_columns("SELECT 1 FROM t WHERE d >= '2024-01-01'", "mysql") == []
    assert dr.relative_date_columns("SELECT 1 FROM t", "mysql") == [] and dr.relative_date_columns("SELEC ((", "mysql") == []
    assert dr.relative_date_columns("SELECT 1 FROM t WHERE amount > 5", "mysql") == []


def run(coro):
    return asyncio.run(coro)


def stub(value):
    seen: list[str] = []

    async def runner(sql, db_id):
        seen.append(sql)
        if isinstance(value, Exception):
            raise value
        return value

    runner.seen = seen  # type: ignore[attr-defined]
    return runner


SQL_WINDOW = "SELECT 1 FROM qc_main M WHERE M.qc_date >= DATEADD(DAY, -90, GETDATE())"


def test_the_note_says_where_the_data_ends_and_reads_one_max_per_column():
    runner = stub([{"m": date(2018, 12, 6)}])
    note = run(dr.data_recency_note(SQL_WINDOW, "tsql", "qts_ais", today=date(2026, 11, 5), runner=runner))
    assert "2018-12-06" in note and "qc_date" in note and "qc_main" in note and "2891 days" in note
    assert runner.seen == ["SELECT MAX([qc_date]) FROM qc_main"]


@pytest.mark.parametrize("value", [
    [{"m": date(2026, 11, 5)}],  # data is current
    [{"m": date(2026, 11, 4)}][:0],  # no rows
    [{"m": None}],
    [{"m": "not a date"}],
    RuntimeError("boom"),
    TimeoutError(),
])
def test_no_note_when_the_data_is_current_missing_unreadable_or_the_database_fails(value):
    assert run(dr.data_recency_note(SQL_WINDOW, "tsql", "x", today=date(2026, 11, 5), runner=stub(value))) == ""


def test_a_slow_database_cannot_hold_up_the_answer():
    async def slow(sql, db_id):
        await asyncio.sleep(5)
        return [{"m": date(2018, 1, 1)}]

    assert run(dr.data_recency_note(SQL_WINDOW, "tsql", "x", today=date(2026, 11, 5), runner=slow, timeout=0.05)) == ""


@pytest.mark.asyncio
async def test_an_empty_window_over_old_data_is_explained_in_the_answer_chunk(live_db):
    sql = "SELECT id FROM orders WHERE order_date >= date('now','-90 day')"
    retriever = SQLRetriever(_router_returning(sql))
    chunks = await retriever.retrieve("orders in the last 90 days")
    text = chunks[0].chunk.content
    assert "No matching records found in the database" in text
    assert "Data recency" in text and "2025-05-11" in text and "`order_date`" in text


@pytest.mark.asyncio
async def test_a_query_with_rows_gets_no_note_and_costs_no_extra_query(live_db):
    sql = "SELECT id FROM orders WHERE order_date >= date('now','-9000 day')"
    chunks = await SQLRetriever(_router_returning(sql)).retrieve("orders in the last 25 years")
    assert "Data recency" not in chunks[0].chunk.content
