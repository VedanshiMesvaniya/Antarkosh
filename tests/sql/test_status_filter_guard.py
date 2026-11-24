"""A status filter the question never asked for (Safeguard 4 of ``generate_sql``) and the soft-delete wording of the SQL prompt.

General rules only: the tables below are invented, and no rule depends on one database. A column called active, status or enabled is
a soft-delete column only when the database's own soft-delete metadata (here: monkeypatched specs) says so.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import src.sql.generation as gen
import src.sql.soft_delete_spec as ss
from src.sql.dialects.sqlite import SQLITE_PROFILE
from src.sql.prompt_builder import get_scoped_readability_rules

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
async def test_only_one_rewrite_is_made_and_a_stubborn_model_is_not_blocked() -> None:
    router = _router("SELECT id FROM suppliers WHERE active = 'Y'", "SELECT id FROM suppliers WHERE active = 'Y' AND country = 'DE'")
    sql, _cot, infra = await _generate(router, "list suppliers from Germany")
    assert infra is None and "country = 'DE'" in sql  # the rewrite is used as written; no third call
    assert router.chat.await_count == 2


@pytest.mark.asyncio
async def test_a_rewrite_that_gives_nothing_keeps_the_first_query() -> None:
    router = _router("SELECT id FROM suppliers WHERE active = 'Y'", "NO_SQL")
    sql, _cot, infra = await _generate(router, "list suppliers")
    assert infra is None and "active = 'Y'" in sql and router.chat.await_count == 2
    router = AsyncMock()
    router.chat.side_effect = ["```sql\nSELECT id FROM suppliers WHERE active = 'Y'\n```", RuntimeError("boom")]
    sql, _cot, infra = await _generate(router, "list suppliers")
    assert infra is None and "active = 'Y'" in sql  # an infrastructure failure during the rewrite never fails the answer


@pytest.mark.asyncio
async def test_an_earlier_repair_error_is_kept_beside_the_status_note() -> None:
    router = _router("SELECT id FROM suppliers WHERE active = 'Y'", "SELECT id FROM suppliers")
    await gen.generate_sql(router=router, query="list suppliers", schema=SCHEMA, dialect=SQLITE_PROFILE, db_id=DB, last_error="no such column: zzz")
    second = router.chat.await_args_list[1].kwargs["messages"][0]["content"]
    assert "no such column: zzz" in second and "suppliers.active" in second


# ------------------------------------------------------------------------------------------------ the prompt wording


def test_the_prompt_no_longer_tells_the_model_that_inactive_means_deleted() -> None:
    text = get_scoped_readability_rules("list suppliers from Germany", ["suppliers"], db_id=DB)
    rule = next(line for line in text.splitlines() if line.startswith("- Mandatory Soft-Delete Rule"))
    assert "only when this prompt supplies an exact table-specific soft-delete column and condition" in rule
    assert "Do not infer that active, status, enabled, or similar columns mean deleted records" in rule
    assert "only when the user explicitly asks for that status" in rule
    assert "Excluding soft-deleted/inactive records" not in rule
    assert "STRICTLY REQUIRED" in rule and "must never be omitted" in rule  # a supplied marker stays mandatory


def test_the_deleted_and_history_rules_are_unchanged() -> None:
    deleted = get_scoped_readability_rules("show me deleted suppliers", ["suppliers"], db_id=DB)
    history = get_scoped_readability_rules("supplier audit history", ["suppliers"], db_id=DB)
    assert "Mandatory Soft-Delete Rule (EXPLICIT DELETED QUERY)" in deleted and "Mandatory Soft-Delete Rule (AUDIT / HISTORY QUERY)" in history
