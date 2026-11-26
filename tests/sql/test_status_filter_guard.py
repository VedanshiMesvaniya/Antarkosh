"""A status filter the question never asked for (Safeguard 4: it fails closed on every path) and the soft-delete wording of the SQL prompt.

General rules only: the tables below are invented, and no rule depends on one database. A column called active, status or enabled is
a soft-delete column only when the database's own soft-delete metadata (here: monkeypatched specs) says so.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.sql.generation as gen
import src.sql.soft_delete_spec as ss
from src.core import config, db_client
from src.core.provider_client import ProviderRouter
from src.sql.dialects.sqlite import SQLITE_PROFILE
from src.sql.pipeline import SQLRetriever
from src.sql.prompt_builder import get_scoped_readability_rules
from src.utils.stream_token_counter import TokenBudgetExceededError

DB = "status_guard_db"
SCHEMA = (
    "CREATE TABLE suppliers (id INT, name TEXT, country TEXT, active CHAR(1), status VARCHAR(10));\n"
    "CREATE TABLE invoices (id INT, supplier_id INT, is_deleted INT, active CHAR(1));"
)
MARKER = {"invoices": ss.SoftDeleteSpec("invoices", "is_deleted", "int_flag", "declared")}


@pytest.fixture(autouse=True)
def specs(monkeypatch: pytest.MonkeyPatch) -> dict[str, ss.SoftDeleteSpec]:
    """``invoices.is_deleted`` is the only verified soft-delete marker; ``active`` and ``status`` are ordinary columns."""
    monkeypatch.setattr(ss, "load_specs", lambda db_id=DB, **_k: MARKER)
    return MARKER


def _router(*replies: str) -> AsyncMock:
    router = AsyncMock()
    router.chat.side_effect = [f"```sql\n{r}\n```" for r in replies]
    return router


async def _generate(router: AsyncMock, query: str) -> tuple[str, str | None, str | None]:
    return await gen.generate_sql(router=router, query=query, schema=SCHEMA, dialect=SQLITE_PROFILE, db_id=DB)


def _flagged(sql: str, query: str) -> list[str]:
    return gen.unrequested_status_filters(sql, query, "sqlite", DB)


# ------------------------------------------------------------------------------------------------ the check on its own


def test_an_active_filter_nobody_asked_for_is_reported() -> None:
    assert _flagged("SELECT id FROM suppliers WHERE active = 'Y'", "list suppliers from Germany") == ["suppliers.active"]
    assert _flagged("SELECT id FROM suppliers s WHERE s.country = 'DE' AND s.active = 1", "list suppliers from Germany") == ["suppliers.active"]
    assert _flagged("SELECT id FROM suppliers WHERE status IN ('A', 'Y')", "suppliers") == ["suppliers.status"]
    assert _flagged("SELECT s.id FROM suppliers s JOIN invoices i ON i.supplier_id = s.id AND s.active = 'Y'", "suppliers with invoices") == ["suppliers.active"]
    assert _flagged("SELECT id FROM suppliers WHERE 'Y' = active", "suppliers") == ["suppliers.active"]
    assert _flagged("SELECT id FROM (SELECT id FROM suppliers WHERE enabled = 1) x", "suppliers") == ["suppliers.enabled"]


@pytest.mark.parametrize(
    "query",
    ["show active suppliers", "which suppliers are inactive", "suppliers whose status is blocked", "list the current suppliers",
     "enabled suppliers in Germany", "live suppliers"],
)
def test_a_question_that_names_a_status_may_use_it(query: str) -> None:
    assert _flagged("SELECT id FROM suppliers WHERE active = 'Y'", query) == []


def test_deleted_and_archived_questions_are_not_judged() -> None:
    assert _flagged("SELECT id FROM suppliers WHERE active = 'N'", "show deleted suppliers") == []
    assert _flagged("SELECT id FROM suppliers WHERE active = 'N'", "supplier history") == []


def test_a_verified_soft_delete_marker_and_a_business_status_are_never_reported() -> None:
    assert _flagged("SELECT id FROM invoices WHERE is_deleted = 0", "list invoices") == []
    assert _flagged("SELECT id FROM invoices WHERE active = 'Y'", "list invoices") == ["invoices.active"]  # is_deleted is the marker, not active
    assert _flagged("SELECT id FROM suppliers WHERE status = 'OPEN'", "list suppliers") == []  # a business value, not a yes/no state
    assert _flagged("SELECT id FROM suppliers WHERE active = country", "list suppliers") == []  # not a literal
    assert _flagged("SELECT id FROM suppliers WHERE active = 'Y' OR country = 'DE'", "list suppliers") == []  # only AND-ed conditions are read
    assert _flagged("SELECT id FROM suppliers WHERE country = 'DE'", "list suppliers") == []


def test_a_column_that_is_a_marker_in_one_shown_table_is_not_reported_when_the_table_is_unclear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "load_specs", lambda db_id=DB, **_k: {"invoices": ss.SoftDeleteSpec("invoices", "active", "char_flag", "curated")})
    assert _flagged("SELECT s.id FROM suppliers s JOIN invoices i ON i.supplier_id = s.id WHERE active = 'Y'", "list suppliers") == []
    assert _flagged("SELECT id FROM invoices WHERE active = 'Y'", "list invoices") == []  # curated as the marker of invoices


def test_the_check_never_raises_and_says_nothing_it_cannot_support() -> None:
    assert _flagged("", "q") == [] and _flagged("this is not sql", "q") == [] and _flagged("SELECT 1", "q") == []
    assert gen.unrequested_status_filters(None, "q", "sqlite", DB) == []  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ generation: one rewrite, never silent


@pytest.mark.asyncio
async def test_a_normal_lookup_never_runs_an_invented_active_filter() -> None:
    router = _router("SELECT id, name FROM suppliers WHERE country = 'DE' AND active = 'Y'", "SELECT id, name FROM suppliers WHERE country = 'DE'")
    sql, _cot, infra = await _generate(router, "list suppliers from Germany")
    assert infra is None and "active" not in sql.lower() and "country = 'DE'" in sql
    assert router.chat.await_count == 2
    retry_prompt = router.chat.await_args_list[1].kwargs["messages"][0]["content"]
    assert "suppliers.active" in retry_prompt and "without that condition" in retry_prompt  # the model is told what to remove; nothing is cut out silently


@pytest.mark.asyncio
async def test_a_question_without_the_filter_costs_no_extra_call() -> None:
    router = _router("SELECT id, name FROM suppliers WHERE country = 'DE'")
    sql, _cot, infra = await _generate(router, "list suppliers from Germany")
    assert infra is None and "suppliers" in sql and router.chat.await_count == 1


@pytest.mark.asyncio
async def test_an_explicit_active_request_may_use_the_active_column() -> None:
    router = _router("SELECT id, name FROM suppliers WHERE active = 'Y'")
    sql, _cot, infra = await _generate(router, "show active suppliers")
    assert infra is None and "active = 'Y'" in sql and router.chat.await_count == 1


@pytest.mark.asyncio
async def test_a_verified_soft_delete_marker_is_still_enforced_and_never_retried() -> None:
    router = _router("SELECT id FROM invoices")
    sql, _cot, infra = await _generate(router, "list invoices")
    assert infra is None and "is_deleted = 0" in sql  # the mandatory filter is added exactly as before
    assert router.chat.await_count == 1
    router = _router("SELECT id FROM invoices WHERE is_deleted = 0")
    sql, _cot, _ = await _generate(router, "list invoices")
    assert "is_deleted = 0" in sql and router.chat.await_count == 1
    router = _router("SELECT id FROM invoices WHERE active = 'Y'", "SELECT id FROM invoices")
    sql, _cot, _ = await _generate(router, "list invoices")  # the marker stays, only the invented active filter goes
    assert "is_deleted = 0" in sql and "active" not in sql.lower() and router.chat.await_count == 2


@pytest.mark.asyncio
async def test_a_rewrite_that_still_has_the_filter_is_not_executed() -> None:
    router = _router("SELECT id FROM suppliers WHERE active = 'Y'", "SELECT id FROM suppliers WHERE active = 'Y' AND country = 'DE'")
    sql, _cot, infra = await _generate(router, "list suppliers from Germany")
    assert sql == "" and infra == gen.STATUS_FILTER_ERROR  # fails closed: nothing to execute
    assert router.chat.await_count == 2  # one rewrite only, no third call


@pytest.mark.asyncio
async def test_a_rewrite_that_gives_nothing_does_not_execute_the_first_query() -> None:
    router = _router("SELECT id FROM suppliers WHERE active = 'Y'", "NO_SQL")
    sql, _cot, infra = await _generate(router, "list suppliers")
    assert sql == "" and infra == gen.STATUS_FILTER_ERROR and router.chat.await_count == 2
    router = AsyncMock()
    router.chat.side_effect = ["```sql\nSELECT id FROM suppliers WHERE active = 'Y'\n```", RuntimeError("boom")]
    sql, _cot, infra = await _generate(router, "list suppliers")
    assert sql == "" and infra == gen.STATUS_FILTER_ERROR  # a failure during the rewrite never lets the first query through


@pytest.mark.asyncio
async def test_an_earlier_repair_error_is_kept_beside_the_status_note() -> None:
    router = _router("SELECT id FROM suppliers WHERE active = 'Y'", "SELECT id FROM suppliers")
    await gen.generate_sql(router=router, query="list suppliers", schema=SCHEMA, dialect=SQLITE_PROFILE, db_id=DB, last_error="no such column: zzz")
    second = router.chat.await_args_list[1].kwargs["messages"][0]["content"]
    assert "no such column: zzz" in second and "suppliers.active" in second


# ------------------------------------------------------------------------------------------------ the other paths


async def _fallback(router: AsyncMock, query: str) -> tuple[str, str | None, str | None]:
    """The token-budget fallback: the first call exceeds the budget, so the compressed prompt makes the second."""
    router.chat.side_effect = [TokenBudgetExceededError("budget", 9000, 8000), *router.chat.side_effect]
    return await _generate(router, query)


@pytest.mark.asyncio
async def test_the_token_budget_fallback_rejects_an_unrequested_status_filter() -> None:
    router = _router("SELECT id FROM suppliers WHERE active = 'Y'")
    sql, _cot, infra = await _fallback(router, "list suppliers from Germany")
    assert sql == "" and infra == gen.STATUS_FILTER_ERROR
    assert router.chat.await_count == 2  # no budget is spent on a rewrite there


@pytest.mark.asyncio
async def test_the_token_budget_fallback_still_allows_an_explicit_status_and_still_adds_the_real_marker() -> None:
    sql, _cot, infra = await _fallback(_router("SELECT id FROM suppliers WHERE active = 'Y'"), "show active suppliers")
    assert infra is None and "active = 'Y'" in sql  # asked for: allowed
    sql, _cot, infra = await _fallback(_router("SELECT id FROM invoices"), "list invoices")
    assert infra is None and "is_deleted = 0" in sql  # the verified marker is applied automatically, as on the normal path


@pytest.fixture()
def supplier_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "status_guard.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT, country TEXT, active CHAR(1));"
        "INSERT INTO suppliers VALUES (1, 'Acme', 'DE', 'Y'), (2, 'Globex', 'FR', 'N');"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(config.settings, "db_engine", "sqlite", raising=False)
    monkeypatch.setattr(db_client, "DB_PATH", path, raising=False)
    SQLRetriever.clear_schema_cache()
    SQLRetriever.clear_result_cache()
    return path


async def _delta_repair(question: str, repaired: str) -> tuple[SQLRetriever, list[Any], list[str], AsyncMock]:
    """A first query that fails on a missing column, then a delta repair that returns ``repaired``; returns what ran."""
    router = MagicMock(spec=ProviderRouter)
    router.chat = AsyncMock(side_effect=["SELECT id, missing_col FROM suppliers", repaired])
    executed: list[str] = []
    real = gen.run_readonly_query

    async def spy(sql: str, **kwargs: Any) -> Any:
        executed.append(sql)
        return await real(sql, **kwargs)

    with patch("src.sql.generation.is_feature_enabled", side_effect=lambda flag: flag == "delta_repair_enabled"), \
            patch("src.sql.generation.run_readonly_query", spy):
        retriever = SQLRetriever(router=router)
        chunks = await retriever.retrieve(question)
    return retriever, chunks, executed, router.chat


@pytest.mark.asyncio
async def test_a_delta_repair_that_brings_the_status_filter_back_is_not_executed(supplier_db: Path) -> None:
    retriever, chunks, executed, chat = await _delta_repair("Show all supplier names", "SELECT id, name FROM suppliers WHERE active = 'Y'")
    assert chunks == [] and chat.await_count == 2
    assert retriever.last_infra_error == gen.STATUS_FILTER_ERROR and retriever.last_query_status == "failed"
    assert not [sql for sql in executed if "active" in sql.lower()]  # the repaired query never reached the database


@pytest.mark.asyncio
async def test_a_delta_repair_may_use_an_explicit_status_and_a_clean_repair_runs(supplier_db: Path) -> None:
    _r, chunks, executed, _chat = await _delta_repair("Show all active supplier names", "SELECT id, name FROM suppliers WHERE active = 'Y'")
    assert len(chunks) == 1 and "Acme" in chunks[0].chunk.content and "Globex" not in chunks[0].chunk.content
    retriever, chunks, executed, _chat = await _delta_repair("Show all supplier names", "SELECT id, name FROM suppliers")
    assert len(chunks) == 1 and "Globex" in chunks[0].chunk.content and retriever.last_infra_error is None


def test_the_answer_path_shows_a_plain_message_for_the_refusal() -> None:
    from src.pipeline import query as q

    assert q._sql_unavailable_message(gen.STATUS_FILTER_ERROR) == (
        "I could not generate a reliable query without an unrequested status filter. Please try again."
    )
    assert q._sql_unavailable_task(gen.STATUS_FILTER_ERROR) == "sql_status_filter"
    assert q._sql_unavailable_message("All providers exhausted") == q._SQL_UNAVAILABLE_MSG  # everything else keeps the old message


# ------------------------------------------------------------------------------------------------ the prompt wording


def test_the_prompt_no_longer_tells_the_model_that_inactive_means_deleted() -> None:
    text = get_scoped_readability_rules("list suppliers from Germany", ["suppliers"], db_id=DB)
    rule = next(line for line in text.splitlines() if line.startswith("- Mandatory Soft-Delete Rule"))
    assert "only when this prompt supplies an exact table-specific soft-delete column and condition" in rule
    assert "Do not infer that active, status, enabled, or similar columns mean deleted records" in rule
    assert "only when the user explicitly asks for that status" in rule
    assert "Excluding soft-deleted/inactive records" not in rule
    assert "STRICTLY REQUIRED" in rule and "must never be omitted" in rule  # a supplied marker stays mandatory


@pytest.mark.parametrize("db_id", ["erp_main", DB])
@pytest.mark.parametrize("question", ["list suppliers from Germany", "show active suppliers"])
def test_every_database_gets_the_same_default_soft_delete_rule(db_id: str, question: str) -> None:
    from src.sql.prompt_builder import build_sql_prompt

    prompt = build_sql_prompt(question, SCHEMA, MagicMock(name="PostgreSQL", date_functions=""), db_id=db_id)
    rule = (
        "Use only the exact table-specific soft-delete condition supplied below. If no condition is supplied for a table, "
        "do not invent a delete, active, status, enabled, or inactive filter."
    )
    assert prompt.count(rule) == 2  # the scope line and the default rule
    assert "ALWAYS filter out soft-deleted records by adding `alias.deleted_at IS NULL`" not in prompt
    assert "Filter out soft-deleted records using `WHERE alias.deleted_at IS NULL`" not in prompt


def test_the_deleted_and_history_rules_are_unchanged() -> None:
    deleted = get_scoped_readability_rules("show me deleted suppliers", ["suppliers"], db_id=DB)
    history = get_scoped_readability_rules("supplier audit history", ["suppliers"], db_id=DB)
    assert "Mandatory Soft-Delete Rule (EXPLICIT DELETED QUERY)" in deleted and "Mandatory Soft-Delete Rule (AUDIT / HISTORY QUERY)" in history
