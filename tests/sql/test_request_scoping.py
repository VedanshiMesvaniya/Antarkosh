"""Per-request database scoping: the validator uses THIS database's relationships, repair knows the dialect, synthesis keeps a slot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.sql import generation
from src.sql import pipeline as sql_pipeline
from src.sql.dialects import get_dialect_profile
from src.sql.repair.delta_repair import (
    MAX_TOTAL_REPAIR_TOKENS,
    build_delta_repair_payload,
    count_tokens,
)
from src.sql.safety.result_validator import ResultValidator
from src.sql.soft_delete_spec import SoftDeleteSpec, classify_column, enforce
from src.utils.query_budget import QueryBudgetController

RELS = [{"from_table": "sqts_main_lear", "to_table": "sqts_user_profiles", "from_column": "sqts_user_profile_id", "to_column": "sqts_user_profile_id"}]
TABLES = ["sqts_main_lear", "sqts_user_profiles"]


# ================================================================= validator scoping


def test_a_join_that_is_in_this_databases_relationships_is_not_called_disconnected():
    own = ResultValidator({}, relationships=RELS).join_path_validator.validate("SELECT 1", TABLES)
    assert own.passed and not own.context.get("isolated_tables")


def test_without_relationships_the_old_default_database_fallback_is_unchanged():
    old = ResultValidator({}).join_path_validator.validate("SELECT 1", TABLES)  # no relationships passed: default (ERP) ones
    assert not old.passed and set(old.context["isolated_tables"]) == set(TABLES)


def test_a_database_with_no_relationships_is_not_checked_against_another_databases():
    res = ResultValidator({}, relationships=[]).join_path_validator.validate("SELECT 1", TABLES)
    assert res.passed  # unmapped schema: nothing to validate, and never the ERP graph


def test_the_pipeline_builds_its_validator_from_the_selected_database(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []
    monkeypatch.setattr(sql_pipeline, "get_raw_relationships", lambda db_id=None: seen.append(db_id) or RELS)
    retriever = sql_pipeline.SQLRetriever(SimpleNamespace(), db_id="qts_ais")
    assert "qts_ais" in seen
    assert retriever._result_validator.join_path_validator.validate("SELECT 1", TABLES).passed


# ================================================================= soft-delete flags


@pytest.mark.parametrize(("name", "type_text", "kind"), [
    ("record_deleted", "char(1)", "char_flag"), ("RecordDeleted", "CHAR(1)", "char_flag"), ("row_deleted", "bit", "int_flag"),
    ("entry_removed", "boolean", "boolean"), ("deleted", "char(1)", "char_flag"), ("is_deleted", "bit", "int_flag"),
])
def test_a_noun_deleted_flag_is_recognised_by_its_type(name, type_text, kind):
    assert classify_column(name, type_text, nullable=False) == kind


@pytest.mark.parametrize(("name", "type_text"), [
    ("rows_deleted", "int"), ("items_deleted", "bigint"), ("rows_deleted", "varchar(50)"), ("undeleted", "char(1)"),
    ("not_deleted", "char(1)"), ("record_deleted", "datetime"), ("deleted_by", "char(1)"),
])
def test_counts_names_and_inverted_names_are_not_taken_for_flags(name, type_text):
    assert classify_column(name, type_text, nullable=True) is None


def spec(kind):
    return {"t": SoftDeleteSpec("t", "record_deleted", kind, "discovered")}


def active_sql(where, kind="char_flag", intent="ACTIVE_ONLY"):
    sql, _ = enforce(f"SELECT t.a FROM t AS t JOIN u ON t.k = u.k WHERE {where}", intent, "tsql", spec(kind))
    return sql[sql.index("WHERE"):]


CANON_CHAR = "WHERE (t.record_deleted IN ('N', 'n') OR t.record_deleted IS NULL)"


@pytest.mark.parametrize("wrong", ["t.record_deleted IS NULL", "t.record_deleted = 0", "t.record_deleted = '0'", "t.record_deleted = false"])
def test_a_wrong_form_on_a_char_flag_is_replaced_by_the_real_condition(wrong):
    assert active_sql(wrong) == CANON_CHAR


@pytest.mark.parametrize("right", ["t.record_deleted = 'N'", "t.record_deleted IN ('N', 'n')", "(t.record_deleted IN ('N','n') OR t.record_deleted IS NULL)"])
def test_a_right_form_is_left_exactly_as_it_was(right):
    assert active_sql(right).count("record_deleted") == right.count("record_deleted") and "(corrected)" not in active_sql(right)


def test_other_flag_kinds_keep_their_own_forms_and_still_reject_wrong_ones():
    assert active_sql("t.record_deleted = 0", "int_flag") == "WHERE t.record_deleted = 0"
    assert active_sql("t.record_deleted = 'N'", "int_flag") == "WHERE (t.record_deleted = 0 OR t.record_deleted IS NULL)"
    assert active_sql("t.record_deleted IS NULL", "int_flag") == "WHERE (t.record_deleted = 0 OR t.record_deleted IS NULL)"
    assert active_sql("t.record_deleted IS NULL", "timestamp") == "WHERE t.record_deleted IS NULL"
    assert active_sql("t.record_deleted = 0", "timestamp") == "WHERE t.record_deleted IS NULL"


def test_enforcement_is_idempotent_and_a_guessed_deleted_form_is_corrected_too():
    once = active_sql("t.record_deleted = 0")
    again, _ = enforce(f"SELECT t.a FROM t AS t JOIN u ON t.k = u.k {once}", "ACTIVE_ONLY", "tsql", spec("char_flag"))
    assert again[again.index("WHERE"):] == once
    assert active_sql("t.record_deleted = 1", intent="DELETED_ONLY") == "WHERE t.record_deleted IN ('Y', 'y')"


def test_a_wrong_form_in_a_left_join_on_clause_is_corrected_in_place():
    sql, notes = enforce("SELECT t.a FROM u LEFT JOIN t AS t ON t.k = u.k AND t.record_deleted = 0", "ACTIVE_ONLY", "tsql", spec("char_flag"))
    assert "t.record_deleted = 0" not in sql and "IN ('N', 'n')" in sql and any("corrected" in n for n in notes)


# ================================================================= dialect in repair


@pytest.mark.parametrize(("engine", "limit"), [("mssql", "TOP 5"), ("oracle", "FETCH FIRST 5 ROWS ONLY"), ("postgresql", "LIMIT 5"), ("mysql", "LIMIT 5"), ("sqlite", "LIMIT 5")])
def test_the_repair_context_names_the_dialect_and_how_it_writes_a_row_limit(engine, limit):
    retriever = SimpleNamespace(db_id="qts_ais", _dialect=get_dialect_profile(engine))
    contract = generation._dialect_contract(retriever)
    assert get_dialect_profile(engine).name in contract and limit in contract
    assert count_tokens(contract) <= 40
    messages = build_delta_repair_payload("SELECT 1", "boom", "sql_error", {}, "q", join_context=contract)
    assert contract in messages[1]["content"] + messages[0]["content"]
    assert sum(count_tokens(m["content"]) for m in messages) <= MAX_TOTAL_REPAIR_TOKENS + 80  # join context has its own allowance


def test_erp_main_and_broken_retrievers_get_no_contract_and_never_raise():
    assert generation._dialect_contract(SimpleNamespace(db_id="erp_main", _dialect=get_dialect_profile("mysql"))) == ""
    assert generation._dialect_contract(SimpleNamespace(db_id="x")) == ""
    assert generation._dialect_contract(None) == ""


def test_the_contract_reaches_every_repair_call_through_the_shared_context_helper(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(generation, "build_repair_join_context", lambda *a, **k: "KNOWN JOINS")
    retriever = SimpleNamespace(db_id="qts_ais", _dialect=get_dialect_profile("mssql"))
    text = generation._repair_join_context(retriever, ["a"], {})
    assert text.startswith("KNOWN JOINS") and text.endswith("A row limit is written as: SELECT TOP 5 x FROM t.")
    assert "Target database: SQL Server (T-SQL)" in generation._advisory_join_context(retriever, ["a"], {}, [])


# ================================================================= budget keeps a slot for the answer


def spent(llm_calls: int, **kw) -> QueryBudgetController:
    ctrl = QueryBudgetController(query_id="q", **kw)
    ctrl.llm_calls = llm_calls
    return ctrl


def test_the_call_that_writes_the_answer_is_not_blocked_by_the_call_count():
    ctrl = spent(4)  # one generation plus three repairs: every call used
    assert ctrl.can_proceed() is False and ctrl.can_proceed(is_repair=True) is False
    assert ctrl.can_proceed(is_final=True) is True


def test_the_token_ceiling_still_stops_even_the_answer_call():
    ctrl = spent(1, max_tokens=1000, hard_limit=1000)
    ctrl.total_tokens = 5000
    assert ctrl.can_proceed(is_final=True) is False and ctrl.can_proceed() is False


def test_everything_else_keeps_its_caps_exactly():
    assert spent(3).can_proceed() is True and spent(4).can_proceed() is False
    ctrl = spent(1)
    ctrl.repair_attempts = ctrl.max_repairs
    assert ctrl.can_proceed(is_repair=True) is False and ctrl.can_proceed(is_final=True, is_repair=True) is False


def test_only_the_synthesis_tasks_count_as_the_final_answer():
    from src.utils.query_budget import FINAL_ANSWER_TASKS

    assert FINAL_ANSWER_TASKS == {"synthesis", "micro_synthesis"}


@pytest.mark.asyncio
async def test_the_router_lets_the_synthesis_stream_through_an_exhausted_call_budget(monkeypatch: pytest.MonkeyPatch):
    from src.core import provider_client as pc
    from src.utils import query_budget as qb

    ctrl = spent(4)
    monkeypatch.setattr(pc, "get_current_budget_controller", lambda: ctrl)
    checks: dict[str, bool] = {}
    real = ctrl.can_proceed
    monkeypatch.setattr(ctrl, "can_proceed", lambda is_repair=False, is_final=False: checks.__setitem__("final", is_final) or real(is_repair, is_final))
    router = pc.ProviderRouter(preferred_provider="auto")
    try:
        async for _ in router.chat_stream("synthesis", [{"role": "user", "content": "hi"}]):
            break
    except qb.QueryBudgetExceededError:
        pytest.fail("synthesis stream should pass through exhausted call budget")
    except Exception:
        pass
    assert checks.get("final") is True
    with pytest.raises(qb.QueryBudgetExceededError):
        async for _ in router.chat_stream("general_qa", [{"role": "user", "content": "hi"}]):
            break
