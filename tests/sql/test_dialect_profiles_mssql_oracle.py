"""SQL Server and Oracle dialect profiles: catalog SQL shape, executor transform, sync wiring.

These run offline. They prove the catalog queries parse and survive the executor's
row-cap rewrite, return the column names the shared splitter expects, and that
sync_live_schema produces correct chunks for each engine from catalog-shaped rows.
They do NOT prove the SQL runs on a live SQL Server / Oracle instance.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlglot
from sqlglot import exp

from src.core.config import settings
from src.sql.dialects import DIALECTS, get_dialect_profile
from src.sql.generation import format_schema_rows
from src.sql.safety.sql_safety import check_cartesian_explosion

NEW_ENGINES = ("mssql", "oracle")
EXPECTED_SCHEMA_ALIASES = ["table_name", "column_name", "data_type", "column_comment"]
EXPECTED_FK_ALIASES = ["table_name", "column_name", "referenced_table_name", "referenced_column_name"]
ROW_CAP_SYNTAX = {"mssql": "TOP 20000", "oracle": "FETCH FIRST 20000 ROWS ONLY"}


def _aliases(sql: str, dialect: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read=dialect)
    return [e.alias_or_name for e in tree.expressions]


@pytest.mark.parametrize("engine", NEW_ENGINES)
def test_registered_and_consistent(engine):
    profile = get_dialect_profile(engine)
    assert DIALECTS[engine] is profile and profile.key == engine
    assert profile.name and profile.date_functions.strip() and profile.schema_query.strip() and profile.fk_query.strip()


@pytest.mark.parametrize("engine", NEW_ENGINES)
def test_catalog_queries_return_the_shared_column_names(engine):
    p = get_dialect_profile(engine)
    assert _aliases(p.schema_query, p.sqlglot_dialect) == EXPECTED_SCHEMA_ALIASES
    assert _aliases(p.fk_query, p.sqlglot_dialect) == EXPECTED_FK_ALIASES


@pytest.mark.parametrize("engine", NEW_ENGINES)
@pytest.mark.parametrize("which", ["schema_query", "fk_query"])
def test_catalog_queries_are_single_read_only_selects_and_survive_the_row_cap_rewrite(engine, which):
    p = get_dialect_profile(engine)
    sql = getattr(p, which)

    assert not sql.strip().endswith(";")  # Oracle rejects a trailing semicolon
    statements = sqlglot.parse(sql, read=p.sqlglot_dialect)
    assert len(statements) == 1 and isinstance(statements[0], exp.Select)
    assert not list(statements[0].find_all((exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Command)))
    assert check_cartesian_explosion(sql, dialect=p.sqlglot_dialect)[0] is False  # not clamped to 100 rows

    # Same rewrite run_readonly_query applies: parse, add the row cap, re-serialise.
    tree = sqlglot.parse_one(sql, read=p.sqlglot_dialect)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(20000)))
    rewritten = tree.sql(dialect=p.sqlglot_dialect)
    assert ROW_CAP_SYNTAX[engine] in rewritten
    sqlglot.parse_one(rewritten, read=p.sqlglot_dialect)  # still valid after the rewrite


def test_mssql_quotes_reserved_word_and_is_scoped_to_default_schema():
    p = get_dialect_profile("mssql")
    assert "[class]" in p.schema_query  # CLASS is reserved; unbracketed it breaks on a real server
    assert "SCHEMA_ID()" in p.schema_query and "SCHEMA_ID()" in p.fk_query
    assert "is_ms_shipped = 0" in p.schema_query


def test_oracle_is_scoped_to_current_schema_and_skips_recycle_bin():
    p = get_dialect_profile("oracle")
    assert p.schema_query.count("SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')") == 1
    assert p.fk_query.count("SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')") == 1
    assert "BIN$" in p.schema_query


@pytest.mark.parametrize("engine,needle", [("mssql", "TOP"), ("oracle", "FETCH FIRST")])
def test_prompt_dialect_hint_tells_the_model_how_to_limit_rows(engine, needle):
    assert needle in get_dialect_profile(engine).date_functions


@pytest.mark.parametrize("engine", NEW_ENGINES)
def test_format_schema_rows_groups_columns_and_keeps_comments(engine):
    rows = [
        {"table_name": "ORDERS", "column_name": "ID", "data_type": "int", "column_comment": "Primary key"},
        {"table_name": "ORDERS", "column_name": "TOTAL", "data_type": "decimal", "column_comment": None},
        {"table_name": "ITEMS", "column_name": "ID", "data_type": "int", "column_comment": ""},
    ]
    text = format_schema_rows(get_dialect_profile(engine), rows)
    assert "TABLE ORDERS (" in text and "TABLE ITEMS (" in text
    assert "ID int  -- Primary key" in text and "TOTAL decimal" in text


# ------------------------------------------------------------------ end-to-end sync (mocked catalog)

CATALOG = {
    "oracle": (
        "hr",
        [
            {"table_name": "EMPLOYEES", "column_name": "EMPLOYEE_ID", "data_type": "NUMBER", "column_comment": "Primary key"},
            {"table_name": "EMPLOYEES", "column_name": "DEPARTMENT_ID", "data_type": "NUMBER", "column_comment": None},
            {"table_name": "DEPARTMENTS", "column_name": "DEPARTMENT_ID", "data_type": "NUMBER", "column_comment": None},
        ],
        [{"table_name": "EMPLOYEES", "column_name": "DEPARTMENT_ID",
          "referenced_table_name": "DEPARTMENTS", "referenced_column_name": "DEPARTMENT_ID"}],
        "FOREIGN KEY (DEPARTMENT_ID) REFERENCES DEPARTMENTS(DEPARTMENT_ID)",
    ),
    "mssql": (
        "awdb",
        [
            {"table_name": "Orders", "column_name": "OrderID", "data_type": "int", "column_comment": "Order number"},
            {"table_name": "Orders", "column_name": "CustomerID", "data_type": "int", "column_comment": None},
            {"table_name": "Customers", "column_name": "CustomerID", "data_type": "int", "column_comment": None},
        ],
        [{"table_name": "Orders", "column_name": "CustomerID",
          "referenced_table_name": "Customers", "referenced_column_name": "CustomerID"}],
        "FOREIGN KEY (CustomerID) REFERENCES Customers(CustomerID)",
    ),
}


@pytest.fixture(autouse=True)
def _no_auto_schema_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests check the sync's core behaviour. The per-database schema-intelligence flags default to ON for databases
    without their own entry (``db_defaults``), and each phase would add its own catalog queries, so pin them off here."""
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: False)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", NEW_ENGINES)
async def test_sync_live_schema_builds_correct_chunks_for_engine(engine, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    db_id, column_rows, fk_rows, fk_line = CATALOG[engine]
    monkeypatch.setattr(settings, "db_engine", "mysql")  # global default must NOT decide the dialect
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": engine})
    profile = get_dialect_profile(engine)
    seen: list[tuple[str, Any]] = []

    async def fake_run(sql: str, *a: Any, **kw: Any):
        seen.append((sql, kw.get("db_id")))
        return column_rows if sql == profile.schema_query else fk_rows

    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", fake_run)

    captured: list[Any] = []
    store = AsyncMock()
    store.upsert = AsyncMock(side_effect=lambda chunks, *args: captured.extend(chunks))
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(return_value=([[0.1] * 8] * 2, [None] * 2))

    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id=db_id)

    assert result["status"] == "ok" and result["tables_synced"] == 2
    assert [s for s, _ in seen] == [profile.schema_query, profile.fk_query]
    assert all(d == db_id for _, d in seen)
    names = sorted(result["table_names"])
    assert names == sorted({r["table_name"] for r in column_rows})  # identifier case kept as the catalog stores it
    by_id = {c.chunk_id: c for c in captured}
    child = next(n for n in names if n in (c["table_name"] for c in fk_rows))
    assert f"{db_id}_schema_{child}" in by_id
    assert fk_line in by_id[f"{db_id}_schema_{child}"].content
    assert all(c.db_id == db_id for c in captured)
