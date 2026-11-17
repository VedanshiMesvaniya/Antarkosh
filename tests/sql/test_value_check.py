# ruff: noqa: F811
"""A filter value that provably does not exist (active = '1' on a column holding 'Y'/'N') is repaired, not accepted as emptiness."""

from __future__ import annotations

import asyncio

import pytest

from src.sql.pipeline import SQLRetriever
from src.sql.safety import value_check as vc
from tests.sql.test_sql_retrieval import _router_returning, live_db  # noqa: F401  (shared fixtures)

SQL = "SELECT t.label, COUNT(l.qn) FROM main_t AS l JOIN types AS t ON l.type_id = t.type_id WHERE t.active = '1' GROUP BY t.label"


def run(coro):
    return asyncio.run(coro)


def rows_of(*values):
    async def runner(sql, db_id):
        runner.seen.append(sql)  # type: ignore[attr-defined]
        return [{"v": v} for v in values]

    runner.seen = []  # type: ignore[attr-defined]
    return runner


def test_equality_and_in_filters_are_found_with_their_real_table_through_aliases():
    got = {(t.name, c, tuple(v)) for t, c, v in vc.literal_filters(SQL, "tsql")}
    assert got == {("types", "active", ("1",))}
    got = vc.literal_filters("SELECT 1 FROM orders o WHERE o.status IN ('a','b') AND o.kind = 3", "mysql")
    assert {(c, tuple(v)) for _, c, v in got} == {("status", ("a", "b")), ("kind", ("3",))}


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM t",  # no WHERE
    "SELECT 1 FROM a JOIN b ON a.x = b.x WHERE a.y = b.y",  # column against column
    "SELECT 1 FROM t WHERE x > 5",  # not equality
    "SELECT 1 FROM a JOIN b ON a.i = b.i WHERE x = 'v'",  # unqualified column over several tables: owner unknown
    "SELEC ((",
])
def test_nothing_to_check_when_the_filter_is_not_a_literal_on_a_known_table(sql):
    assert vc.literal_filters(sql, "mysql") == []


def test_at_most_three_columns_are_checked():
    sql = "SELECT 1 FROM t WHERE a='1' AND b='2' AND c='3' AND d='4' AND e='5'"
    assert len(vc.literal_filters(sql, "mysql")) == vc.MAX_COLUMNS


def test_the_note_names_the_real_values_and_one_cheap_query_per_column():
    runner = rows_of("Y", "N")
    note, repairable = run(vc.value_mismatch_note(SQL, "tsql", "qts_ais", runner=runner))
    assert "`types.active` only holds 'N', 'Y'" in note and "filtered it with '1'" in note and repairable
    assert runner.seen == ["SELECT DISTINCT TOP 11 [active] FROM types"]


@pytest.mark.parametrize("values", [
    ("Y", "N", "1"),  # the literal exists
    ("y", "n", " 1 "),  # case and padding do not matter
    (1, 0),  # a number column holding the same value as the string literal
])
def test_a_literal_that_exists_is_never_flagged(values):
    assert run(vc.value_mismatch_note(SQL, "tsql", "x", runner=rows_of(*values))) == ("", False)


@pytest.mark.parametrize("values", [
    tuple(f"code{i}" for i in range(11)),  # too many values: not a code list, nothing is proven
    (),  # empty table: no information
    (None,),  # only NULLs
])
def test_no_note_when_the_column_is_not_a_small_code_list(values):
    assert run(vc.value_mismatch_note(SQL, "tsql", "x", runner=rows_of(*values))) == ("", False)


@pytest.mark.parametrize(("literal", "guess"), [("1", True), ("0", True), ("true", True), ("'Y'", False), ("-3", True), ("2.5", True), ("Boston", False), ("A-1", False)])
def test_only_a_flag_or_number_guess_forces_a_repair(literal, guess):
    sql = f"SELECT 1 FROM t WHERE status = '{literal.strip(chr(39))}'"
    note, repairable = run(vc.value_mismatch_note(sql, "mysql", "x", runner=rows_of("OPEN", "CLOSED")))
    assert note and repairable == (guess or literal == "'Y'")


def test_a_failing_or_slow_database_gives_no_note_and_never_raises():
    async def boom(sql, db_id):
        raise RuntimeError("no permission")

    async def slow(sql, db_id):
        await asyncio.sleep(5)
        return [{"v": "Y"}]

    assert run(vc.value_mismatch_note(SQL, "tsql", "x", runner=boom)) == ("", False)
    assert run(vc.value_mismatch_note(SQL, "tsql", "x", runner=slow, timeout=0.05)) == ("", False)
    assert run(vc.value_mismatch_note("SELEC ((", "tsql", "x", runner=boom)) == ("", False)


def counting(router):
    calls = []
    inner = router.chat

    async def chat(*a, **k):
        calls.append(1)
        return await inner(*a, **k)

    router.chat = chat
    return calls


@pytest.mark.asyncio
async def test_a_guessed_flag_value_is_repaired_and_the_real_values_explained_in_the_answer(live_db):
    router = _router_returning("SELECT id FROM customers WHERE city = '1'")
    calls = counting(router)
    chunks = await SQLRetriever(router).retrieve("customers in city 1")
    text = chunks[0].chunk.content
    assert len(calls) > 1  # one generation plus at least one repair attempt
    assert "`customers.city` only holds 'LA', 'NYC'" in text and "'1'" in text


@pytest.mark.asyncio
async def test_a_named_value_that_does_not_exist_is_explained_without_repair_calls(live_db):
    router = _router_returning("SELECT id FROM customers WHERE city = 'Boston'")
    calls = counting(router)
    chunks = await SQLRetriever(router).retrieve("customers in Boston")
    assert len(calls) == 1 and "`customers.city` only holds 'LA', 'NYC'" in chunks[0].chunk.content


@pytest.mark.asyncio
async def test_real_emptiness_is_left_alone_and_costs_no_repair(live_db):
    router = _router_returning("SELECT id FROM customers WHERE city = 'LA' AND name = 'Acme'")  # both values exist, no such row
    calls = counting(router)
    chunks = await SQLRetriever(router).retrieve("Acme customers in LA")
    assert len(calls) == 1 and "Value check" not in chunks[0].chunk.content


@pytest.mark.asyncio
async def test_a_query_with_rows_runs_no_value_check(live_db):
    chunks = await SQLRetriever(_router_returning("SELECT id FROM customers WHERE city = 'NYC'")).retrieve("customers in NYC")
    assert "Value check" not in chunks[0].chunk.content
