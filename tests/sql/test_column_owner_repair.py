"""Column-owner repair context and one-line column comments.

* A column comment with a line break must not stop a table from compacting (``flatten_comment``).
* When the model uses a column on the wrong table, the repair step is shown which tables really have that column and how they
  link to the tables the query uses. Off unless the Phase 12 prompt flags are in apply mode; never for ``erp_main``.

Schemas are synthetic and generic: nothing here is tied to one database or one question.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.provider_client import ProviderRouter
from src.sql import generation, pipeline
from src.sql.dialects import get_dialect_profile
from src.sql.repair import column_owners as co
from src.sql.schema_compactor import compact_ddl, flatten_comment
from src.sql.schema_retrieval import _split_mysql_tables

DB = "owner_db"


# ===================================================================== one-line comments


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("Quote number", "Quote number"),
        ("Quote\nnumber", "Quote number"),
        ("Quote\r\nnumber", "Quote number"),
        ("a\n\n  b \r c", "a b c"),
        ("keeps  inner  spaces", "keeps  inner  spaces"),
        (12, "12"),
    ],
)
def test_flatten_comment(raw: Any, expected: str) -> None:
    assert flatten_comment(raw) == expected


def _rows(comment: str | None) -> list[dict[str, Any]]:
    cols = [("rec_id", "int", None), ("qn_number", "nvarchar", comment), ("prinbr", "nchar", None), ("price", "smallint", None)]
    return [{"table_name": "cost", "column_name": c, "data_type": t, "column_comment": m} for c, t, m in cols]


@pytest.mark.parametrize("engine", ["mssql", "mysql", "postgresql", "oracle"])
def test_a_multi_line_comment_no_longer_stops_the_table_from_compacting(engine: str) -> None:
    profile = get_dialect_profile(engine)
    for text in (generation.format_schema_rows(profile, _rows("Quote number\nsecond line")), _split_mysql_tables(_rows("Quote\r\nnumber"))["cost"]):
        out = compact_ddl(text, dialect=profile.sqlglot_dialect)
        assert out.startswith("cost: ") and "TABLE" not in out  # compacted, not the raw DDL
        assert "prinbr(varchar)" in out and "price(int)" in out


def test_ordinary_comments_produce_exactly_the_text_they_always_did() -> None:
    profile = get_dialect_profile("mssql")
    text = generation.format_schema_rows(profile, _rows("Quote number"))
    assert "  qn_number nvarchar  -- Quote number,\n" in text
    other = _split_mysql_tables(_rows("Quote number"))["cost"]
    assert [ln.strip() for ln in text.splitlines()] == [ln.strip() for ln in other.splitlines()]  # same lines (indent differs, as before)


# ===================================================================== reading the validator's message


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Column 'PRINBR' does not exist in table 'Lear_Cost'. Available columns: ['a']", ("prinbr", "lear_cost")),
        ("Column 'x' does not exist on table 't'. Available columns: a, b", ("x", "t")),
        ("Column 'x' not found in any of the query's tables (a, b).", ("x", None)),
        ("Table 'x' does not exist in schema.", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_missing_column(error: str | None, expected: Any) -> None:
    assert co.parse_missing_column(error) == expected


# ===================================================================== choosing the tables

ERR = "Column 'prinbr' does not exist in table 'lear_cost'. Available columns: ['price', 'qn_number']"


def _live() -> dict[str, list[str]]:
    """lear_cost has qn_number but no prinbr; main_lear links the two; deleted/created are in every table."""
    audit = ["deleted", "created_at"]
    live = {
        "lear_cost": ["lear_cost_id", "qn_number", "price", *audit],
        "supplier_codes": ["prinbr", "supplier_code", *audit],
        "main_lear": ["qn_number", "prinbr", "plant", *audit],
        "param_cost": ["rec_id", "qn_number", "price", "prinbr", *audit],
        "unrelated_a": ["prinbr", "memo", *audit],
        "unrelated_b": ["prinbr", "remark", *audit],
        "main_lear_bak": ["qn_number", "prinbr", *audit],
    }
    for i in range(30):  # a larger schema, so audit columns are plainly "in most tables"
        live[f"filler_{i}"] = [f"f{i}", *audit]
    return live


def test_the_block_names_the_tables_that_really_have_the_column_best_linked_first() -> None:
    out = co.build_column_owner_block(ERR, _live(), ["lear_cost", "supplier_codes"])
    lines = out.splitlines()
    assert lines[0] == "Where column 'prinbr' exists (it is not in lear_cost):"
    assert lines[1].startswith("- supplier_codes: prinbr, supplier_code") and lines[1].endswith("(already in the query)")
    owners = [ln.split(":")[0][2:] for ln in lines[1:-1]]
    # tables that share a rare column with lear_cost (qn_number) are chosen; tables that share nothing are left out
    assert owners[0] == "supplier_codes" and set(owners[1:]) == {"main_lear", "param_cost", "main_lear_bak"}
    assert "unrelated_a" not in out and "unrelated_b" not in out
    assert "(shares qn_number with lear_cost)" in next(ln for ln in lines if ln.startswith("- main_lear:"))
    assert lines[-1].startswith("Join tables only through columns they really share")


def test_a_column_found_in_nearly_every_table_never_makes_a_table_a_bridge() -> None:
    out = co.build_column_owner_block(ERR, _live(), ["lear_cost", "supplier_codes"])
    assert "deleted" not in out.split("main_lear:")[1].splitlines()[0].split("(shares")[1]  # only the rare column is named as shared


def test_copies_are_never_offered_and_the_block_is_capped() -> None:
    out = co.build_column_owner_block(ERR, _live(), ["lear_cost", "supplier_codes"], skip_tables=["main_lear_bak"])
    assert "main_lear_bak" not in out
    assert len([ln for ln in out.splitlines() if ln.startswith("- ")]) <= co.MAX_OWNERS
    wide = {"t": ["x"], "o": [f"c{i}" for i in range(40)] + ["x"]}
    line = co.build_column_owner_block("Column 'x' does not exist in table 't'.", wide, ["t"]).splitlines()[1]
    assert len(line.split(": ", 1)[1].split(", ")) <= co.MAX_COLUMNS


@pytest.mark.parametrize(
    "error",
    ["Table 'x' does not exist in schema.", "Column 'nowhere_col' does not exist in table 'lear_cost'.", "", None],
)
def test_nothing_is_said_when_there_is_nothing_useful(error: str | None) -> None:
    assert co.build_column_owner_block(error, _live(), ["lear_cost"]) == ""
    assert co.build_column_owner_block(ERR, {}, ["lear_cost"]) == ""


def test_the_block_is_deterministic() -> None:
    a = co.build_column_owner_block(ERR, _live(), ["lear_cost", "supplier_codes"])
    b = co.build_column_owner_block(ERR, dict(reversed(list(_live().items()))), ["supplier_codes", "lear_cost"])
    assert a == b


# ===================================================================== flags and the repair loop


@pytest.fixture()
def flags(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, bool] = {}
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: state.get(f, False) and db != "erp_main")

    def set_flags(inject: bool = False, shadow: bool = False) -> None:
        state["auto_prompt_inject"], state["auto_prompt_shadow"] = inject, shadow

    set_flags()
    return set_flags


def _live_text() -> str:
    return "\n\n".join(f"TABLE {t} (\n" + ",\n".join(f"  {c} varchar" for c in cols) + "\n)" for t, cols in _live().items())


def _retriever(monkeypatch: pytest.MonkeyPatch) -> Any:
    r = pipeline.SQLRetriever(router=MagicMock(spec=ProviderRouter), db_id="erp_main")
    r.db_id = DB
    r._router.chat = AsyncMock(return_value="")
    monkeypatch.setattr(pipeline.SQLRetriever, "_column_registry", {})
    monkeypatch.setattr(pipeline.SQLRetriever, "_full_schema_cache", {DB: _live_text()})
    co._INDEX_CACHE.clear()
    return r


def test_context_is_empty_unless_the_flags_are_in_apply_mode(flags, monkeypatch) -> None:
    r = _retriever(monkeypatch)
    for kwargs in ({}, {"shadow": True}):
        flags(**kwargs)
        assert asyncio.run(co.column_owner_context(r, ERR, ["lear_cost"])) == ""
    flags(inject=True)
    assert "main_lear" in asyncio.run(co.column_owner_context(r, ERR, ["lear_cost", "supplier_codes"]))
    r.db_id = "erp_main"
    assert asyncio.run(co.column_owner_context(r, ERR, ["lear_cost"])) == ""  # the pinned database is never touched


def test_context_never_raises(flags, monkeypatch) -> None:
    flags(inject=True)
    r = _retriever(monkeypatch)

    async def boom() -> str:
        raise RuntimeError("db down")

    r._fetch_full_schema = boom
    assert asyncio.run(co.column_owner_context(r, ERR, ["lear_cost"])) == ""
    assert asyncio.run(co.column_owner_context(object(), ERR, ["lear_cost"])) == ""


PROMPT = "TABLE lear_cost (\n  lear_cost_id int,\n  qn_number varchar,\n  price decimal\n)\n\nTABLE supplier_codes (\n  prinbr varchar,\n  supplier_code varchar\n)"
WRONG_SQL = "SELECT s.supplier_code, SUM(c.price) AS total FROM lear_cost AS c JOIN supplier_codes AS s ON c.prinbr = s.prinbr GROUP BY s.supplier_code"
GOOD_SQL = (
    "SELECT s.supplier_code, SUM(c.price) AS total FROM lear_cost AS c JOIN main_lear AS m ON c.qn_number = m.qn_number "
    "JOIN supplier_codes AS s ON m.prinbr = s.prinbr GROUP BY s.supplier_code"
)


async def _run_loop(r: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(generation, "run_readonly_query", AsyncMock(return_value=[{"supplier_code": "S1", "total": 5}]))
    r._generate_sql = AsyncMock(return_value=WRONG_SQL)
    r._router.chat = AsyncMock(return_value=f"```sql\n{GOOD_SQL}\n```")
    await generation.execute_with_delta_repair(r, "total cost per supplier", PROMPT)
    r._router.chat.assert_awaited()
    return r._router.chat.call_args_list[0].kwargs["messages"][1]["content"]


@pytest.mark.asyncio
async def test_the_repair_prompt_for_a_column_error_shows_where_the_column_lives(flags, monkeypatch) -> None:
    flags(inject=True)
    prompt = await _run_loop(_retriever(monkeypatch), monkeypatch)
    assert "Column 'prinbr' does not exist in table 'lear_cost'" in prompt
    assert "Where column 'prinbr' exists (it is not in lear_cost):" in prompt
    assert "- main_lear: prinbr, qn_number" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"shadow": True}])
async def test_with_the_flags_off_the_repair_prompt_is_exactly_what_it_was(flags, monkeypatch, kwargs) -> None:
    flags(**kwargs)
    prompt = await _run_loop(_retriever(monkeypatch), monkeypatch)
    assert "Where column" not in prompt and "main_lear" not in prompt
