# ruff: noqa: F811
"""SQLRequestContext, SQL normalisation, the type validator, and their wiring into the repair loop."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.sql import generation
from src.sql import pipeline as sql_pipeline
from src.sql import schema_retrieval as sr
from src.sql.dialects import get_dialect_profile
from src.sql.pipeline import SQLRetriever
from src.sql.request_context import SQLRequestContext, build_request_context
from src.sql.safety.sql_normalize import dialect_syntax_fix, normalize_sql
from src.sql.safety.type_check import check_types, family
from tests.sql.test_sql_retrieval import _router_returning, live_db  # noqa: F401  (shared fixtures)

TYPES = {
    "types": {"active": "char(1)", "type_id": "int", "label": "varchar(50)", "created": "datetime"},
    "main_t": {"type_id": "int", "qn": "varchar(20)", "record_deleted": "char(1)", "flag": "bit", "qty": "decimal(10,2)"},
}
ENUMS = {"types": {"active": ["Y", "N"]}}


# ================================================================= context


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "shop"
    (folder / "schema").mkdir(parents=True)
    cols = lambda *c: [{"name": n, "type": t, **({"enum_values": e} if e else {})} for n, t, e in c]
    (folder / "schema" / "schema.json").write_text(json.dumps({"tables": [
        {"name": "main_t", "columns": cols(("qn", "varchar(20)", None), ("record_deleted", "char(1)", None))},
        {"name": "dbo.types", "columns": cols(("active", "char(1)", ["Y", "N"]), ("type_id", "int", None))},
    ]}), encoding="utf-8")
    monkeypatch.setattr(sr, "_route_bridge_mode", lambda db_id: "apply")
    return tmp_path


def test_the_context_resolves_types_enums_specs_and_the_dialect_once(dbs: Path):
    ctx = build_request_context("shop", get_dialect_profile("mssql"), databases_dir=dbs)
    assert ctx.db_id == "shop" and ctx.sqlglot_dialect == "tsql" and ctx.strict is True
    assert ctx.column_types["main_t"]["record_deleted"] == "char(1)"
    assert ctx.column_types["dbo.types"] == ctx.column_types["types"]  # qualified and bare names both resolve
    assert ctx.column_enums["types"]["active"] == ["Y", "N"]
    assert "main_t" in ctx.soft_delete_specs and ctx.soft_delete_specs["main_t"].column == "record_deleted"
    assert ctx.summary() == {"db_id": "shop", "dialect": "tsql", "strict": True, "relationships": 0, "soft_delete_tables": 1, "typed_tables": 3}


def test_the_context_is_immutable_and_cached_types_follow_the_file(dbs: Path):
    ctx = build_request_context("shop", get_dialect_profile("mssql"), databases_dir=dbs)
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass
        ctx.strict = False  # type: ignore[misc]
    path = dbs / "shop" / "schema" / "schema.json"
    path.write_text(json.dumps({"tables": [{"name": "other", "columns": [{"name": "x", "type": "int"}]}]}), encoding="utf-8")
    import os

    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert set(build_request_context("shop", get_dialect_profile("mssql"), databases_dir=dbs).column_types) == {"other"}


def test_building_a_context_never_raises_whatever_is_missing(tmp_path: Path):
    ctx = build_request_context("ghost", get_dialect_profile("mysql"), databases_dir=tmp_path)
    assert ctx.column_types == {} and ctx.soft_delete_specs == {} and ctx.database is None
    assert build_request_context("Bad Id!", get_dialect_profile("mysql"), databases_dir=tmp_path).column_types == {}
    assert build_request_context("", get_dialect_profile("mysql"), databases_dir=tmp_path).db_id == "erp_main"


@pytest.mark.parametrize(("db_id", "mode", "strict"), [("shop", "apply", True), ("shop", "shadow", False), ("shop", None, False), ("erp_main", "apply", False)])
def test_strict_mode_is_for_non_erp_databases_in_apply_mode_only(dbs: Path, monkeypatch: pytest.MonkeyPatch, db_id, mode, strict):
    monkeypatch.setattr(sr, "_route_bridge_mode", lambda d: mode)
    assert build_request_context(db_id, get_dialect_profile("mssql"), databases_dir=dbs).strict is strict


def test_a_failing_flag_lookup_means_not_strict(dbs: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(db_id):
        raise RuntimeError("flags unreadable")

    monkeypatch.setattr(sr, "_route_bridge_mode", boom)
    assert build_request_context("shop", get_dialect_profile("mssql"), databases_dir=dbs).strict is False


@pytest.mark.parametrize(("engine", "limit"), [("mssql", "TOP 5"), ("oracle", "FETCH FIRST 5 ROWS ONLY"), ("postgresql", "LIMIT 5")])
def test_the_dialect_contract_comes_from_the_context(engine, limit):
    ctx = SQLRequestContext("shop", get_dialect_profile(engine))
    assert limit in ctx.dialect_contract() and get_dialect_profile(engine).name in ctx.dialect_contract()
    assert SQLRequestContext("erp_main", get_dialect_profile("mysql")).dialect_contract() == ""


def test_the_repair_context_uses_the_request_context_when_given():
    ctx = SQLRequestContext("shop", get_dialect_profile("mssql"))
    retriever = SimpleNamespace(db_id="shop", _dialect=get_dialect_profile("mysql"))  # the retriever says MySQL; the context wins
    assert "SQL Server" in generation._repair_join_context(retriever, ["a"], {}, ctx)
    assert "MySQL" in generation._repair_join_context(retriever, ["a"], {})  # callers without a context: as before


@pytest.mark.asyncio
async def test_the_retriever_builds_one_context_and_hands_it_to_the_repair_loop(live_db, monkeypatch: pytest.MonkeyPatch):
    seen = {}

    async def fake(retriever, query, schema, ctx=None):
        seen["ctx"] = ctx
        return []

    monkeypatch.setattr(sql_pipeline, "execute_with_delta_repair", fake)
    retriever = SQLRetriever(_router_returning("SELECT 1"))
    await retriever._retrieve_with_delta_repair("q", "CREATE TABLE customers (id INT);")
    ctx = seen.get("ctx")
    assert isinstance(ctx, SQLRequestContext) and ctx.db_id == retriever.db_id and ctx.dialect is retriever._dialect


# ================================================================= normalisation


@pytest.mark.parametrize(("dialect", "sql", "expected"), [
    ("tsql", "SELECT a FROM t LIMIT 10", "SELECT TOP 10 a FROM t"),
    ("tsql", "SELECT CAST(a AS INT), b::int FROM t", "SELECT CAST(a AS INTEGER), CAST(b AS INTEGER) FROM t"),
    ("oracle", "SELECT a FROM t LIMIT 5", "SELECT a FROM t FETCH FIRST 5 ROWS ONLY"),
    ("tsql", "SELECT TOP 5 a FROM t", "SELECT TOP 5 a FROM t"),
    ("mysql", "SELECT a FROM t LIMIT 5", "SELECT a FROM t LIMIT 5"),
])
def test_a_statement_is_written_the_way_the_target_dialect_spells_it(dialect, sql, expected):
    assert normalize_sql(sql, dialect) == expected
    assert normalize_sql(expected, dialect) == expected  # idempotent


@pytest.mark.parametrize("bad", ["SELEC ((", "", "SELECT 1; SELECT 2", "not sql at all"])
def test_anything_unparseable_is_returned_unchanged(bad):
    assert normalize_sql(bad, "tsql") == bad


@pytest.mark.parametrize(("dialect", "failed", "fixed"), [
    ("tsql", "SELECT a FROM t LIMIT 10", "SELECT TOP 10 a FROM t"),
    ("tsql", "SELECT `a` FROM `t`", "SELECT [a] FROM [t]"),
    ("postgres", "SELECT [a] FROM [t]", 'SELECT "a" FROM "t"'),
    ("mysql", "SELECT TOP 3 a FROM t", "SELECT a FROM t LIMIT 3"),
    ("sqlite", "SELECT TOP 2 name FROM customers", "SELECT name FROM customers LIMIT 2"),
])
def test_a_syntax_error_is_cured_by_respelling_in_the_target_dialect(dialect, failed, fixed):
    assert dialect_syntax_fix(failed, dialect) == fixed


@pytest.mark.parametrize(("dialect", "sql"), [("tsql", "SELECT TOP 5 a FROM t"), ("tsql", "SELEC (("), ("mysql", "SELECT 1")])
def test_nothing_to_respell_gives_none_so_the_model_is_asked(dialect, sql):
    assert dialect_syntax_fix(sql, dialect) is None


# ================================================================= type validator


def issues(sql, dialect="tsql", enums=ENUMS):
    return check_types(sql, TYPES, dialect, enums)


def kinds(sql, dialect="tsql", enums=ENUMS):
    return sorted((i.kind, i.critical) for i in issues(sql, dialect, enums))


@pytest.mark.parametrize(("typ", "fam"), [("bigint(20) UNSIGNED", "number"), ("tinyint(1)", "number"), ("decimal(10,2)", "number"), ("datetime2", "date"), ("timestamp", "date"),
                                          ("nvarchar(max)", "text"), ("char(1)", "text"), ("bit", "bit"), ("boolean", "bit"), ("geometry", None), ("", None)])
def test_column_type_families(typ, fam):
    assert family(typ) == fam


def test_a_number_column_compared_with_text_is_certain_to_fail_on_strict_dialects_only():
    sql = "SELECT 1 FROM main_t m WHERE m.type_id = 'Y'"
    assert kinds(sql, "tsql") == [("number_vs_text", True)]
    assert kinds(sql, "postgres") == [("number_vs_text", True)] and kinds(sql, "oracle") == [("number_vs_text", True)]
    assert kinds(sql, "mysql") == [("number_vs_text", False)] and kinds(sql, "sqlite") == [("number_vs_text", False)]


@pytest.mark.parametrize("fine", [
    "SELECT 1 FROM main_t m WHERE m.type_id = '5'", "SELECT 1 FROM main_t m WHERE m.type_id = 5", "SELECT 1 FROM main_t m WHERE m.qty > -3.5",
    "SELECT 1 FROM main_t m WHERE m.qn = 'ABC-1'", "SELECT 1 FROM types t WHERE t.created >= '2026-01-31'", "SELECT 1 FROM types t WHERE t.created >= '31/01/2026'",
    "SELECT 1 FROM types t WHERE t.created >= '20260131'", "SELECT 1 FROM main_t m WHERE m.flag = 1", "SELECT 1 FROM main_t m WHERE m.flag = '0'",
    "SELECT 1 FROM main_t m JOIN types t ON m.type_id = t.type_id", "SELECT 1 FROM types t WHERE t.active IN ('Y', 'n')", "SELECT 1 FROM types t WHERE t.label LIKE 'a%'",
])
def test_comparisons_that_are_fine_raise_nothing(fine):
    assert issues(fine) == []


def test_the_reported_failure_shapes():
    assert kinds("SELECT 1 FROM types t WHERE t.active = 1") == [("bad_flag", False)]  # char(1) holding Y/N, compared with 1
    assert kinds("SELECT 1 FROM types t WHERE t.active = 'Z'") == [("bad_flag", False)]
    assert kinds("SELECT 1 FROM main_t m WHERE m.record_deleted = 0") == [("text_vs_number", False)]
    assert kinds("SELECT 1 FROM main_t m WHERE m.qn = 93633") == [("text_vs_number", False)]
    assert kinds("SELECT 1 FROM types t WHERE t.created >= 'last month'") == [("bad_date", True)]
    assert kinds("SELECT 1 FROM main_t m WHERE m.flag = 'x'") == [("number_vs_text", True)]
    assert kinds("SELECT 1 FROM main_t m WHERE m.flag = 7") == [("bad_flag", False)]


def test_columns_of_different_families_are_flagged_when_equated():
    assert kinds("SELECT 1 FROM main_t m JOIN types t ON m.qn = t.type_id") == [("join_type", False)]
    assert kinds("SELECT 1 FROM main_t m JOIN types t ON m.qn = t.created") == [("join_type", False)]
    assert kinds("SELECT 1 FROM main_t m JOIN types t ON m.flag = t.type_id") == []  # a bit flag against a number is normal


def test_aliases_unqualified_columns_and_unknown_names_are_resolved_safely():
    assert kinds("SELECT 1 FROM main_t AS x WHERE x.type_id = 'Y'") == [("number_vs_text", True)]
    assert kinds("SELECT 1 FROM main_t WHERE qty = 'abc'") == [("number_vs_text", True)]
    assert kinds("SELECT 1 FROM main_t m JOIN types t ON m.type_id = t.type_id WHERE type_id = 'Y'") == []  # two owners: not guessed
    assert kinds("SELECT 1 FROM nowhere n WHERE n.x = 'Y'") == []
    assert kinds("SELECT 1 FROM main_t m WHERE m.unknown_col = 'Y'") == []


def test_no_types_unparseable_sql_and_repeats_give_a_quiet_answer():
    assert check_types("SELECT 1 FROM t WHERE x = 'Y'", {}, "tsql") == [] and check_types("SELEC ((", TYPES, "tsql") == []
    assert len(issues("SELECT 1 FROM main_t m WHERE m.type_id = 'Y' OR m.type_id = 'Y'")) == 1


# ================================================================= wired into the repair loop


def ctx_for(engine="sqlite", strict=True, types=None):
    return SQLRequestContext("shop", get_dialect_profile(engine), column_types=types if types is not None else TYPES, column_enums=ENUMS, strict=strict)


def test_normalisation_and_type_results_apply_only_in_strict_mode():
    assert generation._shape(ctx_for("mssql"), "SELECT a FROM t LIMIT 5") == "SELECT TOP 5 a FROM t"
    assert generation._shape(ctx_for("mssql", strict=False), "SELECT a FROM t LIMIT 5") == "SELECT a FROM t LIMIT 5"
    res = generation._type_results(ctx_for("mssql"), "SELECT 1 FROM main_t m WHERE m.type_id = 'Y' AND m.record_deleted = 0")
    assert sorted((r.severity.name, r.context["type_issue"]) for r in res) == [("CRITICAL", "number_vs_text"), ("WARNING", "text_vs_number")]
    assert all(not r.passed for r in res)
    assert generation._type_results(ctx_for("mssql", strict=False), "SELECT 1 FROM main_t m WHERE m.type_id = 'Y'") == []


def counting(router):
    calls: list[int] = []
    inner = router.chat

    async def chat(*a, **k):
        calls.append(1)
        return await inner(*a, **k)

    router.chat = chat
    return calls


async def run(router, ctx, monkeypatch, sql_text="CREATE TABLE customers (id INTEGER, name TEXT, city TEXT);"):
    retriever = SQLRetriever(router)
    monkeypatch.setattr(sql_pipeline, "build_request_context", lambda db_id, dialect: ctx)
    return await retriever.retrieve("customers"), retriever


@pytest.mark.asyncio
async def test_borrowed_syntax_is_respelled_without_a_model_call_in_strict_mode(live_db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(generation, "is_strict", lambda db_id: True)
    router = _router_returning("SELECT TOP 2 name FROM customers")  # T-SQL spelling on a SQLite database
    calls = counting(router)
    chunks, _ = await run(router, ctx_for("sqlite"), monkeypatch)
    assert len(calls) == 1 and "Acme" in chunks[0].chunk.content  # one generation call, no repair, the rows came back


@pytest.mark.asyncio
async def test_the_same_statement_outside_strict_mode_is_dropped_exactly_as_before(live_db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(generation, "is_strict", lambda db_id: False)
    router = _router_returning("SELECT TOP 2 name FROM customers")
    chunks, _ = await run(router, ctx_for("sqlite", strict=False), monkeypatch)
    assert "Acme" not in "".join(c.chunk.content for c in chunks)


@pytest.mark.asyncio
async def test_a_syntax_error_at_the_database_is_respelled_without_a_model_call_in_strict_mode(live_db, monkeypatch: pytest.MonkeyPatch):
    """The database rejects a statement the parser accepted: the deterministic route runs before any model call."""
    real = generation.run_readonly_query
    attempts: list[str] = []

    async def picky(sql, *a, **k):
        attempts.append(sql)
        if "WEIRD" in sql.upper():
            raise RuntimeError('near "WEIRD": syntax error')
        return await real(sql, *a, **k)

    monkeypatch.setattr(generation, "run_readonly_query", picky)
    monkeypatch.setattr(generation, "dialect_syntax_fix", lambda sql, d: "SELECT name FROM customers" if "WEIRD" in sql.upper() else None)
    router = _router_returning("SELECT name FROM customers WEIRD")
    calls = counting(router)
    chunks, _ = await run(router, ctx_for("sqlite"), monkeypatch)
    assert len(calls) == 1 and attempts[-1] == "SELECT name FROM customers" and "Acme" in chunks[0].chunk.content
    attempts.clear()
    SQLRetriever.clear_result_cache()  # the first answer is cached; ask again for real
    router2 = _router_returning("SELECT name FROM customers WEIRD")
    calls2 = counting(router2)
    await run(router2, ctx_for("sqlite", strict=False), monkeypatch)
    assert len(calls2) > 1  # outside strict mode the error goes to the model, as before


ORDERS = {"orders": {"id": "integer", "order_date": "date"}}


@pytest.mark.asyncio
async def test_a_type_problem_makes_an_empty_answer_suspicious_and_is_repaired_in_strict_mode_only(live_db, monkeypatch: pytest.MonkeyPatch):
    sql = "SELECT id FROM orders WHERE order_date >= 'last month'"  # a date column against text with no date in it; nothing matches
    router = _router_returning(sql)
    strict_calls = counting(router)
    await run(router, ctx_for("sqlite", types=ORDERS), monkeypatch)
    assert len(strict_calls) > 1
    router2 = _router_returning(sql)
    plain_calls = counting(router2)
    await run(router2, ctx_for("sqlite", strict=False, types=ORDERS), monkeypatch)
    assert len(plain_calls) == 1


@pytest.mark.asyncio
async def test_a_certain_type_error_is_stopped_before_the_database_in_strict_mode(live_db, monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    real_log = generation._log_pipeline_event
    monkeypatch.setattr(generation, "_log_pipeline_event", lambda name, *a, **k: events.append(name) or real_log(name, *a, **k))
    ran: list[str] = []
    real = generation.run_readonly_query

    async def spy(sql, *a, **k):
        ran.append(sql)
        return await real(sql, *a, **k)

    monkeypatch.setattr(generation, "run_readonly_query", spy)
    types = {"customers": {"id": "integer", "name": "varchar(50)", "city": "varchar(20)"}}
    await run(_router_returning("SELECT id FROM customers WHERE id = 'abc'"), ctx_for("mssql", types=types), monkeypatch)
    assert "semantic_validation_failed" in events and not any("'abc'" in q for q in ran)  # never sent to the database
