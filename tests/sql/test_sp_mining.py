"""Auto-schema Phase 5: learn joins, tables and string values from stored procedures.

Offline. Fixture procedures are written to look like real T-SQL / PL/pgSQL / MySQL / PL/SQL, not taken from
a client database, so real-world accuracy is shown by the counts in the sync report, not by these tests.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.sql.mining import evidence as ev
from src.sql.mining.catalog_index import CatalogIndex
from src.sql.mining.extractor import extract_routine_facts
from src.sql.mining.splitter import split_statements
from src.sql.sa import mining_phase as mp
from src.sql.sa.routines import Routine
from tests.sql.sp_mining_fixtures import HARD_TSQL, SAMPLES
from tests.sql.test_auto_procedures import (  # noqa: F401  (shared fixtures)
    _sync_args,
    dbs,
    sqlite_db,
)


def facts_of(text: str, engine: str, catalog: CatalogIndex | None = None):
    split = split_statements(text, engine)
    return split, extract_routine_facts(split.trees, engine, catalog)


def edge(a: str, b: str):
    (ta, ca), (tb, cb) = a.rsplit(".", 1), b.rsplit(".", 1)
    return tuple(sorted(((ta, ca), (tb, cb))))


# ================================================================= the four dialects


def test_tsql_procedure_with_temp_table_if_block_dynamic_sql_and_case():
    split, f = facts_of(SAMPLES["mssql"], "mssql")
    assert (split.parsed, split.found, split.dynamic) == (4, 4, 1)
    assert f.reads == {"dbo.party", "dbo.po_detail", "dbo.po_head"} and f.writes == {"dbo.po_head"}  # #tmp is not a table
    assert f.joins == {edge("dbo.po_detail.po_id", "dbo.po_head.po_id"), edge("dbo.party.party_id", "dbo.po_head.party_id")}
    assert set(f.values[("dbo.po_head", "po_type")]) == {"R", "S"}  # IN (...) and CASE labels
    assert ("dbo.po_head", "status") not in f.values  # compared with @status, a variable, not a constant


def test_postgresql_function_body_in_dollar_quotes():
    split, f = facts_of(SAMPLES["postgresql"], "postgresql")
    assert split.parsed == split.found == 5
    assert f.reads == {"sales.customers", "sales.orders"} and f.writes == {"sales.orders"}  # tmp_o and v_cnt are not tables
    assert f.joins == {edge("sales.customers.id", "sales.orders.customer_id")}
    assert set(f.values[("sales.orders", "status")]) == {"paid"}


def test_plpgsql_perform_is_read_as_a_select():
    body = "CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN PERFORM 1 FROM sales.customers c JOIN sales.orders o ON o.customer_id = c.id WHERE c.tier = 'gold'; END $$"
    split, f = facts_of(body, "postgresql")
    assert split.parsed == split.found == 1 and f.reads == {"sales.customers", "sales.orders"}
    assert f.joins == {edge("sales.customers.id", "sales.orders.customer_id")} and set(f.values[("sales.customers", "tier")]) == {"gold"}


def test_mysql_procedure_select_into_a_variable_is_not_a_table():
    split, f = facts_of(SAMPLES["mysql"], "mysql")
    assert split.parsed == split.found == 3
    assert f.reads == {"order_lines", "orders"} and f.writes == {"order_log", "orders"}
    assert f.joins == {edge("order_lines.order_id", "orders.id")}
    assert set(f.values[("order_lines", "kind")]) == {"A", "B"} and set(f.values[("orders", "status")]) == {"CLOSED"}


def test_oracle_cursor_loop_and_dml():
    split, f = facts_of(SAMPLES["oracle"], "oracle")
    assert split.parsed == split.found == 3
    assert f.reads == {"hr.departments", "hr.employees"} and f.writes == {"hr.audit_log", "hr.employees"}
    assert f.joins == {edge("hr.departments.department_id", "hr.employees.department_id")}
    assert set(f.values[("hr.employees", "job_id")]) == {"SA_REP", "IT_PROG"}


# ================================================================= messy T-SQL


def test_no_semicolons_nolock_hints_table_variable_and_top():
    split, f = facts_of(HARD_TSQL["nosemi_nolock"], "mssql")
    assert split.parsed == split.found == 3
    assert f.reads == {"dbo.item_master", "dbo.stock"}  # @t is a table variable
    assert f.joins == {edge("dbo.item_master.item_id", "dbo.stock.item_id")}
    assert set(f.values[("dbo.item_master", "item_type")]) == {"FG", "RM"}


def test_cte_names_and_derived_tables_are_not_tables():
    split, f = facts_of(HARD_TSQL["cte_union"], "mssql")
    assert split.parsed == split.found == 1
    assert f.reads == {"dbo.party", "dbo.sales_invoice", "dbo.sales_return"}  # not inv / ret / x
    assert f.joins == set()  # the only join goes through a derived table, which is not resolved


def test_update_and_delete_with_an_alias_target_resolve_to_the_real_table():
    split, f = facts_of(HARD_TSQL["update_from_join"], "mssql")
    assert split.parsed == split.found == 2
    assert f.writes == {"dbo.po_head", "dbo.grn_detail"} and "po" not in f.writes and "d" not in f.reads
    assert f.joins == {edge("dbo.po_detail.po_id", "dbo.po_head.po_id"), edge("dbo.grn_detail.grn_id", "dbo.grn_head.grn_id")}
    assert ("dbo.po_head", "status") in f.values and "CLOSED" in f.values[("dbo.po_head", "status")]


def test_merge_with_an_insert_inside_it_and_a_set_assignment_is_not_a_join():
    split, f = facts_of(HARD_TSQL["merge_trycatch"], "mssql")
    assert split.parsed == split.found == 2
    assert f.writes == {"dbo.item_price", "dbo.error_log"} and f.reads == {"dbo.item_price_stage"}
    assert f.joins == {edge("dbo.item_price.item_id", "dbo.item_price_stage.item_id"), edge("dbo.item_price.price_list", "dbo.item_price_stage.price_list")}
    assert edge("dbo.item_price.price", "dbo.item_price_stage.price") not in f.joins  # that is `SET tgt.price = src.price`


def test_dynamic_sql_is_counted_but_never_analysed_and_select_into_a_temp_table_is_not_a_table():
    split, f = facts_of(HARD_TSQL["dynamic_and_cursor"], "mssql")
    assert split.dynamic == 1 and split.parsed == split.found == 3
    assert f.reads == {"dbo.department", "dbo.employee"} and f.writes == set()
    assert ("dbo.employee", "active") in f.values and ("dbo.department", "dept_type") in f.values


def test_correlated_subquery_and_outer_apply():
    split, f = facts_of(HARD_TSQL["outer_apply_scalar_sub"], "mssql")
    assert split.parsed == split.found == 1
    assert f.joins == {edge("dbo.customer.cust_id", "dbo.sales_order.cust_id")} and ("dbo.customer", "cust_type") in f.values


def test_every_statement_in_all_ten_sample_procedures_is_parsed():
    totals = [split_statements(t, e) for e, t in SAMPLES.items()] + [split_statements(t, "mssql") for t in HARD_TSQL.values()]
    assert sum(s.found for s in totals) == sum(s.parsed for s in totals) > 20 and all(s.error is None for s in totals)


# ================================================================= splitter details


def test_a_case_expression_and_update_set_are_not_cut_in_half():
    # If CASE .. END or UPDATE .. SET ended the statement early, the `b = 'B'` assignment after them would be lost.
    split, f = facts_of("CREATE PROC p AS UPDATE t SET kind = CASE WHEN a > 1 THEN 1 ELSE 2 END, b = 'B' WHERE t.id = 1 AND t.k = 'K'", "mssql")
    assert split.parsed == split.found == 1 and f.writes == {"t"}
    assert set(f.values[("t", "b")]) == {"B"} and set(f.values[("t", "k")]) == {"K"}


@pytest.mark.parametrize("literal", ["OPEN", "END", "SET", "BEGIN", "IF", "GO", "DECLARE", "EXEC", "RETURN"])
def test_a_string_literal_or_quoted_name_that_spells_a_block_word_never_ends_a_statement(literal: str):
    split, f = facts_of(f"CREATE PROC p AS SELECT * FROM po_head WHERE po_head.status = '{literal}' AND po_head.[{literal}] = 1", "mssql")
    assert split.parsed == split.found == 1 and set(f.values[("po_head", "status")]) == {literal}


def test_a_procedure_parameter_in_a_predicate_is_not_mistaken_for_a_column_join():
    _, f = facts_of("CREATE PROCEDURE close_order(IN o INT) BEGIN UPDATE orders SET status = 'CLOSED' WHERE id = o; END", "mysql")
    assert f.joins == set() and set(f.values[("orders", "status")]) == {"CLOSED"}


def test_tokenizer_problems_skip_the_routine_only():
    split = split_statements("CREATE PROC p AS SELECT 'unterminated FROM t", "mssql")
    assert split.error and split.found == 0 and split.trees == []


def test_unknown_engine_and_the_time_and_count_budgets():
    assert split_statements("SELECT 1", "cobol").error
    past = time.monotonic() - 1
    assert split_statements(SAMPLES["mssql"], "mssql", deadline=past).truncated is True
    capped = split_statements(HARD_TSQL["update_from_join"], "mssql", max_statements=1)
    assert capped.truncated is True and capped.found == 1


def test_no_statement_text_survives_in_the_facts():
    _, f = facts_of(SAMPLES["mssql"], "mssql")
    blob = json.dumps([sorted(f.reads), sorted(f.writes), [list(map(list, e)) for e in f.joins], [[k, sorted(v)] for k, v in f.values.items()]])
    assert "SELECT" not in blob.upper().replace("SELECTED", "") and "FROM" not in blob.upper()


# ================================================================= the catalog


CATALOG_DOC = {
    "default_schema": "dbo",
    "tables": [
        {"name": "po_head", "schema": "dbo", "qualified_name": "dbo.po_head", "columns": [{"name": "po_id"}, {"name": "party_id"}, {"name": "status"}, {"name": "po_type"}]},
        {"name": "po_detail", "schema": "dbo", "qualified_name": "dbo.po_detail", "columns": [{"name": "po_id"}, {"name": "qty"}, {"name": "rate"}]},
        {"name": "party", "schema": "dbo", "qualified_name": "dbo.party", "columns": [{"name": "party_id"}, {"name": "party_name"}]},
        {"name": "audit", "schema": "hr", "qualified_name": "hr.audit", "columns": [{"name": "id"}]},
        {"name": "audit", "schema": "dbo", "qualified_name": "dbo.audit", "columns": [{"name": "id"}]},
    ],
    "relationships": [{"from_schema": "dbo", "from_table": "po_detail", "from_column": "po_id", "to_schema": "dbo", "to_table": "po_head", "to_column": "po_id"}],
}


def test_catalog_index_resolves_names_case_insensitively_and_by_default_schema():
    c = CatalogIndex.from_document(CATALOG_DOC)
    assert c.resolve_table("DBO.PO_Head") == "dbo.po_head" and c.resolve_table("po_head") == "dbo.po_head"
    assert c.resolve_table("audit") == "dbo.audit"  # same bare name in two schemas: the default schema wins
    assert c.resolve_table("hr.audit") == "hr.audit" and c.resolve_table("nope") is None
    assert c.has_column("dbo.po_head", "STATUS") and not c.has_column("dbo.po_head", "nope")
    assert c.is_declared_fk(("dbo.po_head", "po_id"), ("dbo.po_detail", "po_id")) and not c.is_declared_fk(("dbo.party", "party_id"), ("dbo.po_head", "po_id"))


def test_catalog_index_without_a_default_schema_refuses_to_guess_between_schemas():
    doc = {**CATALOG_DOC, "default_schema": None}
    assert CatalogIndex.from_document(doc).resolve_table("audit") is None


def test_a_missing_broken_or_empty_catalog_means_unvalidated_mining(tmp_path: Path):
    assert CatalogIndex.from_schema_json(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert CatalogIndex.from_schema_json(bad) is None
    assert CatalogIndex.from_document({"tables": []}) is None  # the empty template


def test_with_a_catalog_unknown_tables_and_columns_are_dropped_and_counted():
    c = CatalogIndex.from_document(CATALOG_DOC)
    _, f = facts_of(SAMPLES["mssql"], "mssql", c)
    assert f.reads == {"dbo.party", "dbo.po_detail", "dbo.po_head"} and f.unknown_tables == 0
    _, g = facts_of("CREATE PROC p AS SELECT * FROM dbo.po_head h JOIN dbo.mystery m ON m.id = h.po_id WHERE h.status = 'A'", "mssql", c)
    assert g.reads == {"dbo.po_head"} and g.unknown_tables == 1 and g.joins == set()


def test_unqualified_columns_are_resolved_only_when_exactly_one_table_can_own_them():
    c = CatalogIndex.from_document(CATALOG_DOC)
    _, one = facts_of("CREATE PROC p AS SELECT * FROM dbo.po_head WHERE status = 'OPEN'", "mssql", c)
    assert set(one.values[("dbo.po_head", "status")]) == {"OPEN"}
    _, shared = facts_of("CREATE PROC p AS SELECT * FROM dbo.po_head h JOIN dbo.po_detail d ON d.po_id = h.po_id WHERE po_id = 'X'", "mssql", c)
    assert ("dbo.po_head", "po_id") not in shared.values and ("dbo.po_detail", "po_id") not in shared.values  # both own po_id
    # without a catalog a single-table statement is unambiguous, a two-table one is not
    _, bare = facts_of("CREATE PROC p AS SELECT * FROM po_head WHERE status = 'OPEN'", "mssql")
    assert ("po_head", "status") in bare.values
    _, two = facts_of("CREATE PROC p AS SELECT * FROM a JOIN b ON a.x = b.x WHERE status = 'OPEN'", "mssql")
    assert not two.values


def test_a_self_join_on_different_columns_is_kept_and_on_the_same_column_is_not():
    _, f = facts_of("CREATE PROC p AS SELECT * FROM emp e JOIN emp m ON e.manager_id = m.emp_id JOIN emp x ON x.emp_id = e.emp_id", "mssql")
    assert edge("emp.emp_id", "emp.manager_id") in f.joins and len(f.joins) == 1


def test_values_are_short_single_line_strings_only():
    _, f = facts_of(f"CREATE PROC p AS SELECT * FROM t WHERE t.a = '{'x' * 41}' AND t.b = 'ok' AND t.c IN ('a', 'b')", "mssql")
    assert set(f.values) == {("t", "b"), ("t", "c")}


# ================================================================= evidence


def _facts(reads=(), writes=(), joins=(), values=None, unknown=0):
    from src.sql.mining.extractor import RoutineFacts

    f = RoutineFacts(reads=set(reads), writes=set(writes), joins=set(joins), unknown_tables=unknown)
    f.join_counts = {e: 1 for e in joins}
    f.values = {k: dict(v) for k, v in (values or {}).items()}
    return f


E_PO = edge("dbo.po_detail.po_id", "dbo.po_head.po_id")
E_PARTY = edge("dbo.party.party_id", "dbo.po_head.party_id")


def test_confidence_two_routines_or_a_declared_foreign_key_is_high_once_is_low():
    e = ev.Evidence(None)
    e.add_routine(2, 2, 0, _facts(joins=[E_PO, E_PARTY]))
    e.add_routine(1, 1, 0, _facts(joins=[E_PARTY]))
    by = {(x["a"]["table"], x["b"]["table"]): x for x in e.document("d", "mssql")["join_edges"]}
    assert by[("dbo.party", "dbo.po_head")]["confidence"] == "high" and by[("dbo.party", "dbo.po_head")]["routines"] == 2
    assert by[("dbo.po_detail", "dbo.po_head")]["confidence"] == "low"
    declared = ev.Evidence(CatalogIndex.from_document(CATALOG_DOC))
    declared.add_routine(1, 1, 0, _facts(joins=[E_PO, E_PARTY]))
    doc = declared.document("d", "mssql")
    flags = {(x["a"]["table"]): (x["confidence"], x["declared_fk"]) for x in doc["join_edges"]}
    assert flags["dbo.po_detail"] == ("high", True) and flags["dbo.party"] == ("low", False)
    assert doc["validated_against_catalog"] is True


def test_the_document_counts_routines_statements_and_skips_columns_that_are_not_enumerations():
    e = ev.Evidence(None)
    e.add_routine(3, 2, 1, _facts(reads=["a"], writes=["b"], values={("a", "kind"): {"X": 2, "Y": 1}}), truncated=False)
    e.add_routine(1, 0, 0, _facts(reads=["a"]))
    many = {("a", "name"): {f"v{i}": 1 for i in range(21)}}
    e.add_routine(1, 1, 0, _facts(values=many))
    doc = e.document("d", "mssql")
    assert doc["stats"] | {} == {**doc["stats"], "routines_total": 3, "routines_parsed": 2, "routines_without_sql": 1,
                                  "statements_found": 5, "statements_parsed": 3, "dynamic_sql": 1}
    assert doc["tables"] == {"a": {"read_in": 2, "written_in": 0}, "b": {"read_in": 0, "written_in": 1}}
    assert [r["column"] for r in doc["enum_values"]] == ["kind"]  # 'name' has 21 distinct values
    assert doc["enum_values"][0]["values"] == [{"value": "X", "count": 2}, {"value": "Y", "count": 1}]


def test_the_document_is_deterministic_and_capped(monkeypatch):
    monkeypatch.setattr(ev, "MAX_EDGES", 1)
    e = ev.Evidence(None)
    e.add_routine(1, 1, 0, _facts(joins=[E_PO, E_PARTY]))
    first = e.document("d", "mssql")
    assert len(first["join_edges"]) == 1 and first["join_edges"] == e.document("d", "mssql")["join_edges"]


def test_summary_is_counts_only():
    e = ev.Evidence(None)
    e.add_routine(1, 1, 0, _facts(joins=[E_PO], values={("a", "k"): {"X": 1}}))
    s = ev.summary(e.document("d", "mssql"))
    assert s["joins_low"] == 1 and s["joins_high"] == 0 and s["value_columns"] == 1
    assert all(not isinstance(v, (list, dict)) for v in s.values())


def test_the_evidence_file_is_written_only_into_an_existing_folder_and_never_over_a_foreign_file(dbs: Path):
    doc = ev.Evidence(None).document("pg_live", "sqlite")
    assert ev.write_evidence("pg_live", doc, dbs)["written"] == ["schema/sp_evidence.json"]
    assert ev.write_evidence("pg_live", doc, dbs)["written"] == ["schema/sp_evidence.json"]  # ours: replaced
    (dbs / "pg_live/schema/sp_evidence.json").write_text('{"mine": 1}', encoding="utf-8")
    assert "left untouched" in ev.write_evidence("pg_live", doc, dbs)["skipped"]["schema/sp_evidence.json"]
    assert json.loads((dbs / "pg_live/schema/sp_evidence.json").read_text()) == {"mine": 1}
    assert "does not exist" in ev.write_evidence("nope_db", doc, dbs)["skipped"]["all"]
    assert "pinned" in ev.write_evidence("erp_main", doc, dbs)["skipped"]["all"] and not (dbs / "erp_main").exists()


# ================================================================= the phase


def _db_routine(name: str, text: str) -> Routine:
    return Routine("dbo", name, "PROCEDURE", "database", text)


@pytest.mark.asyncio
async def test_the_phase_mines_routines_in_a_thread_and_writes_counts_only_to_the_report(dbs: Path):
    routines = [_db_routine("a", SAMPLES["mssql"]), _db_routine("b", HARD_TSQL["update_from_join"]), Routine("dbo", "hidden", "PROCEDURE", "database", None, "hidden")]
    report = await mp.run_mining_phase(db_id="pg_live", engine_key="mssql", routines=routines, databases_dir=dbs)
    assert report["ok"] and report["routines_total"] == 2 and report["statements_found"] == report["statements_parsed"] == 6
    assert report["validated_against_catalog"] is False and report["files"]["written"] == ["schema/sp_evidence.json"]
    assert all(not isinstance(v, (list, dict)) for k, v in report.items() if k != "files")
    doc = json.loads((dbs / "pg_live/schema/sp_evidence.json").read_text())
    assert doc["generated_by"] == "auto_sp_mining" and any(e["a"]["table"] == "dbo.po_detail" for e in doc["join_edges"])
    text = json.dumps(doc).upper()
    assert "CREATE PROC" not in text and "INNER JOIN" not in text and "BEGIN" not in text  # facts, not code


@pytest.mark.asyncio
async def test_the_phase_validates_against_the_generated_catalog_when_there_is_one(dbs: Path):
    (dbs / "pg_live/schema/schema.json").write_text(json.dumps(CATALOG_DOC), encoding="utf-8")
    report = await mp.run_mining_phase(db_id="pg_live", engine_key="mssql", routines=[_db_routine("a", SAMPLES["mssql"])], databases_dir=dbs)
    assert report["validated_against_catalog"] is True and report["joins_high"] == 1 and report["joins_low"] == 1  # the declared FK is high
    doc = json.loads((dbs / "pg_live/schema/sp_evidence.json").read_text())
    assert {tuple(sorted((e["a"]["table"], e["b"]["table"]))): e["declared_fk"] for e in doc["join_edges"]}[("dbo.po_detail", "dbo.po_head")] is True


@pytest.mark.asyncio
async def test_no_readable_routines_still_records_an_honest_empty_result(dbs: Path):
    report = await mp.run_mining_phase(db_id="pg_live", engine_key="mssql", routines=[], databases_dir=dbs)
    assert report["ok"] and report["routines_total"] == 0 and report["joins_high"] == report["joins_low"] == 0


@pytest.mark.asyncio
async def test_the_time_budget_stops_mining_and_says_so(dbs: Path):
    report = await mp.run_mining_phase(db_id="pg_live", engine_key="mssql", routines=[_db_routine("a", SAMPLES["mssql"])], databases_dir=dbs, budget_seconds=-1)
    assert report["ok"] and report["truncated"] is True and report["routines_total"] == 0


@pytest.mark.asyncio
async def test_erp_main_is_never_mined_and_an_untested_sqlglot_is_refused(dbs: Path, monkeypatch):
    assert (await mp.run_mining_phase(db_id="erp_main", engine_key="mysql", routines=[_db_routine("a", SAMPLES["mssql"])]))["skipped"] == "pinned database"
    monkeypatch.setattr(mp, "sqlglot_supported", lambda: False)
    report = await mp.run_mining_phase(db_id="pg_live", engine_key="mssql", routines=[_db_routine("a", SAMPLES["mssql"])], databases_dir=dbs)
    assert report["ok"] is False and "not the tested major version" in report["skipped"] and not (dbs / "pg_live/schema/sp_evidence.json").exists()


def test_the_sqlglot_version_in_use_is_supported():
    assert mp.sqlglot_supported() is True


# ================================================================= inside the real sync

SQLITE_PROC = "CREATE PROCEDURE p AS BEGIN SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.status = 'paid'; END"


def _flags(*on):
    return lambda flag, db: flag in on


@pytest.mark.asyncio
async def test_sync_with_both_flags_mines_the_procedures_folder_and_returns_counts_only(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_procedures", "auto_sp_mining"))
    (dbs / "pg_live" / "procedures").mkdir(exist_ok=True)
    (dbs / "pg_live" / "procedures" / "orders.sql").write_text(SQLITE_PROC, encoding="utf-8")
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["procedures"]["mode"] == "full"
    assert result["sp_evidence"]["ok"] and result["sp_evidence"]["statements_parsed"] == 1 and result["sp_evidence"]["joins_low"] == 1
    assert "SELECT" not in json.dumps(result).upper().replace("SELECTED", "") or "SELECT" not in json.dumps(result["sp_evidence"]).upper()
    assert (dbs / "pg_live/schema/sp_evidence.json").exists()


@pytest.mark.asyncio
async def test_sync_without_the_mining_flag_is_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_procedures"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert "sp_evidence" in result is False or "sp_evidence" not in result
    assert not (dbs / "pg_live/schema/sp_evidence.json").exists()


@pytest.mark.asyncio
async def test_mining_needs_the_procedure_phase_and_a_crash_in_it_never_fails_the_sync(sqlite_db: Path, dbs: Path, monkeypatch):
    from unittest.mock import AsyncMock

    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_sp_mining"))  # procedures flag off
    assert "sp_evidence" not in await sync_live_schema(db_id="pg_live", **_sync_args())

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_procedures", "auto_sp_mining"))
    monkeypatch.setattr(mp, "run_mining_phase", AsyncMock(side_effect=RuntimeError("boom")))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["sp_evidence"]["ok"] is False and "boom" in result["sp_evidence"]["error"]


def test_the_flag_exists_and_erp_main_is_pinned_off():
    from src.utils.feature_flags import DB_FEATURE_FLAGS, is_db_feature_enabled

    assert "auto_sp_mining" in DB_FEATURE_FLAGS and is_db_feature_enabled("auto_sp_mining", "erp_main") is False
