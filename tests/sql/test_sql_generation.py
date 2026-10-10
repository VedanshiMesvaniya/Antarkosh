"""Tests for src.sql.generation and re-exports in src.stages.s12b_sql_retrieval."""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

import src.sql.generation as gen
import src.stages.s12b_sql_retrieval as s12b
from src.sql.dialects.mysql import MYSQL_PROFILE
from src.sql.dialects.sqlite import SQLITE_PROFILE


def test_reexport_object_identity():
    """All symbols in s12b must point to the identical objects in src.sql.generation."""
    symbols = [
        "UnsafeQueryError",
        "DISPLAY_HIDDEN_COLS",
        "_DISPLAY_HIDDEN_COLS",
        "filter_display_rows",
        "_filter_display_rows",
        "sanitize_cell_value",
        "_sanitize_cell_value",
        "sanitize_rows",
        "_sanitize_rows",
        "format_schema_rows",
        "format_fk_rows",
        "ABSTAIN_RE",
        "_ABSTAIN_RE",
        "FENCE_RE",
        "_FENCE_RE",
        "SQL_START_RE",
        "_SQL_START_RE",
        "unwrap_sql",
        "_unwrap_sql",
        "extract_cot_and_sql",
        "is_all_null",
        "_is_all_null",
        "is_aggregate_over_zero_rows",
        "_is_aggregate_over_zero_rows",
        "extract_table_names",
        "_extract_table_names",
        "MAX_DISPLAY_ROWS",
        "_MAX_DISPLAY_ROWS",
        "format_rows_as_markdown",
        "_format_rows_as_markdown",
        "DANGEROUS_FUNCTIONS",
        "_DANGEROUS_FUNCTIONS",
        "is_safe_read_query",
        "_is_safe_read_query",
        "fetch_sqlite_foreign_keys",
        "generate_sql",
        "execute_with_retry",
        "execute_with_delta_repair",
    ]
    for sym in symbols:
        assert hasattr(s12b, sym), f"s12b missing {sym}"
        assert hasattr(gen, sym), f"gen missing {sym}"
        assert getattr(s12b, sym) is getattr(gen, sym), f"{sym} identity mismatch"


def test_unwrap_sql_and_extract_cot():
    """Verify sql unwrapping and CoT separation across varied LLM outputs."""
    fence = chr(96) * 3
    # Fenced sql
    raw_fenced = f"{fence}sql\nSELECT * FROM customers;\n{fence}"
    assert gen.unwrap_sql(raw_fenced) == "SELECT * FROM customers;"

    # Markdown preamble
    raw_preamble = "Here is the SQL query: SELECT id FROM accounts WHERE active = 1"
    assert gen.unwrap_sql(raw_preamble) == "SELECT id FROM accounts WHERE active = 1"

    # CoT + Fenced SQL
    cot_raw = f"Thinking about users...\n{fence}sql\nSELECT id, name FROM users;\n{fence}"
    cot, sql = gen.extract_cot_and_sql(cot_raw)
    assert sql == "SELECT id, name FROM users;"
    assert "Thinking about users" in cot

    # JSON response
    json_raw = '{"reasoning": "Filter active users", "sql": "SELECT * FROM users WHERE active = 1"}'
    cot, sql = gen.extract_cot_and_sql(json_raw)
    assert sql == "SELECT * FROM users WHERE active = 1"
    assert "Filter active users" in cot


def test_format_schema_and_fk_rows():
    """Verify dialect-aware schema and FK row formatting."""
    # SQLite schema rows
    sqlite_rows = [
        {"name": "users", "sql": "CREATE TABLE users (id INT, name TEXT)"},
        {"name": "sqlite_sequence", "sql": "CREATE TABLE sqlite_sequence (name, seq)"},
    ]
    res_sqlite = gen.format_schema_rows(SQLITE_PROFILE, sqlite_rows)
    assert "CREATE TABLE users" in res_sqlite
    assert "sqlite_sequence" not in res_sqlite

    # MySQL schema rows
    mysql_rows = [
        {"table_name": "orders", "column_name": "id", "data_type": "int", "column_comment": "PK"},
        {"table_name": "orders", "column_name": "total", "data_type": "decimal(10,2)", "column_comment": ""},
    ]
    res_mysql = gen.format_schema_rows(MYSQL_PROFILE, mysql_rows)
    assert "TABLE orders (" in res_mysql
    assert "id int  -- PK" in res_mysql
    assert "total decimal(10,2)" in res_mysql

    # Foreign key rows
    fk_rows = [
        {"table_name": "orders", "column_name": "user_id", "referenced_table_name": "users", "referenced_column_name": "id"}
    ]
    fk_text = gen.format_fk_rows(fk_rows)
    assert "Foreign Keys:" in fk_text
    assert "orders.user_id -> users.id" in fk_text


def test_display_filter_and_sanitation():
    """Verify display metadata column filtering and type sanitation."""
    rows = [
        {
            "id": 101,
            "created_at": datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
            "updated_at": datetime.datetime(2025, 1, 2, 12, 0, tzinfo=datetime.UTC),
            "deleted_at": None,
            "name": "Widget A",
            "price": Decimal("24.50"),
            "data": b"blob_data",
        }
    ]
    filtered = gen.filter_display_rows(rows)
    assert len(filtered) == 1
    assert "id" not in filtered[0]
    assert "created_at" not in filtered[0]
    assert "name" in filtered[0]
    assert "price" in filtered[0]

    sanitized = gen.sanitize_rows(filtered)
    assert sanitized[0]["price"] == 24.5
    assert sanitized[0]["data"] == "blob_data"

    # All columns hidden fallback: never return empty table
    all_hidden = [{"id": 1, "created_at": "now"}]
    assert gen.filter_display_rows(all_hidden) == all_hidden


def test_is_safe_read_query():
    """Verify AST safety validator blocks stacked, destructive, and DoS queries."""
    # Safe queries
    assert gen.is_safe_read_query("SELECT * FROM users", SQLITE_PROFILE) is True
    assert gen.is_safe_read_query("SELECT id FROM a UNION ALL SELECT id FROM b", SQLITE_PROFILE) is True

    # Multi-statement / stacked queries
    assert gen.is_safe_read_query("SELECT 1; DROP TABLE users;", SQLITE_PROFILE) is False

    # Destructive queries
    assert gen.is_safe_read_query("DROP TABLE users", SQLITE_PROFILE) is False
    assert gen.is_safe_read_query("DELETE FROM users WHERE id = 1", SQLITE_PROFILE) is False
    assert gen.is_safe_read_query("INSERT INTO users VALUES (1, 'Eve')", SQLITE_PROFILE) is False

    # DoS and file exfiltration functions
    assert gen.is_safe_read_query("SELECT SLEEP(10)", SQLITE_PROFILE) is False
    assert gen.is_safe_read_query("SELECT BENCHMARK(1000000, MD5('x'))", SQLITE_PROFILE) is False
    assert gen.is_safe_read_query("SELECT LOAD_FILE('/etc/passwd')", SQLITE_PROFILE) is False
    assert gen.is_safe_read_query("SELECT * INTO OUTFILE '/tmp/out.csv' FROM users", SQLITE_PROFILE) is False


def test_aggregate_zero_row_detection():
    """Verify 0-row aggregate detection distinguishes empty aggregates from real NULL cells."""
    # Aggregate over empty table yields single NULL row
    assert gen.is_aggregate_over_zero_rows("SELECT SUM(amount) FROM orders WHERE 1=0", [{"total": None}], "sqlite") is True
    assert gen.is_aggregate_over_zero_rows("SELECT AVG(amount), COUNT(*) FROM orders WHERE 1=0", [{"avg": None, "cnt": None}], "sqlite") is True

    # Grouped aggregate returns empty list if no matches, or multiple rows
    assert gen.is_aggregate_over_zero_rows("SELECT user_id, SUM(amount) FROM orders GROUP BY user_id", [{"user_id": None, "sum": None}], "sqlite") is False

    # Ordinary SELECT where selected column value is NULL is NOT an aggregate over 0 rows
    assert gen.is_aggregate_over_zero_rows("SELECT notes FROM orders WHERE id = 1", [{"notes": None}], "sqlite") is False

    # All-null checks
    assert gen.is_all_null([{"a": None, "b": None}]) is True
    assert gen.is_all_null([{"a": 0}]) is False
    assert gen.is_all_null([]) is False


@pytest.mark.asyncio
async def test_generate_sql_success_and_soft_delete():
    """Verify generate_sql prompts router, parses response, and enforces soft delete."""
    mock_router = AsyncMock()
    mock_router.chat.return_value = "```sql\nSELECT id, name FROM party WHERE is_active = 1\n```"

    sql, _cot, infra = await gen.generate_sql(
        router=mock_router,
        query="List all active parties",
        schema="CREATE TABLE party (id INT, name TEXT, is_active INT, deleted_at TIMESTAMP);",
        dialect=SQLITE_PROFILE,
        db_id="erp_main",
    )
    assert "party" in sql
    assert "deleted_at IS NULL" in sql
    assert infra is None
    assert mock_router.chat.await_count == 1


@pytest.mark.asyncio
async def test_generate_sql_abstention():
    """Verify generate_sql respects model NO_SQL abstention sentinel."""
    mock_router = AsyncMock()
    mock_router.chat.return_value = "NO_SQL - the database schema has no relation to weather reports."

    sql, _cot, infra = await gen.generate_sql(
        router=mock_router,
        query="What is the weather today?",
        schema="CREATE TABLE users (id INT, name TEXT);",
        dialect=SQLITE_PROFILE,
        db_id="erp_main",
    )
    assert sql == ""
    assert infra is None
