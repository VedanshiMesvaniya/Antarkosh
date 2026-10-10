"""PostgreSQL support in the Text-to-SQL layer.

The first half is pure (no server) and runs everywhere, including CI. The second
half is opt-in, like the MySQL tests: set POSTGRES_TEST_HOST (plus PORT/USER/
PASSWORD/DB) to a THROWAWAY database whose user may CREATE tables in `public`.
The live tests create and drop their own tables and run every query through the
real db_client, so they verify the read-only transaction, the schema/FK queries
and the timeout path against a real server.
"""

from __future__ import annotations

import os

import pytest

from src.core.sql_column_registry import ColumnRegistry
from src.core.sql_dialects import DIALECTS, get_dialect_profile
from src.pipeline.schema_ingestion import _split_schema_by_table
from src.stages.s12b_sql_retrieval import SQLRetriever, format_schema_rows
from src.utils.error_classification import classify_error
from src.utils.sql_safety import check_cartesian_explosion, is_destructive_sql

PG = get_dialect_profile("postgresql")

ROWS = [
    {"table_name": "gpu_sales", "column_name": "id", "data_type": "integer", "column_comment": None},
    {"table_name": "gpu_sales", "column_name": "revenue", "data_type": "numeric",
     "column_comment": "Gross revenue in USD"},
    {"table_name": "gpu_sales", "column_name": "tags", "data_type": "text[]", "column_comment": None},
    {"table_name": "regions", "column_name": "id", "data_type": "integer", "column_comment": None},
]


def _retriever() -> SQLRetriever:
    r = SQLRetriever(router=object())
    r._dialect = PG
    return r


class TestProfile:
    def test_fields(self) -> None:
        assert (PG.key, PG.name, PG.sqlglot_dialect) == ("postgresql", "PostgreSQL", "postgres")
        assert "information_schema.columns" in PG.schema_query and "current_schema()" in PG.schema_query
        assert "pg_constraint" in PG.fk_query and PG.date_functions.strip()

    def test_registry_key_matches(self) -> None:
        assert all(p.key == k for k, p in DIALECTS.items())
        assert set(DIALECTS) == {"sqlite", "mysql", "postgresql"}

    def test_unsupported_engines_still_raise(self) -> None:
        for engine in ("oracle", "mssql"):
            with pytest.raises(ValueError, match="Unsupported db_engine"):
                get_dialect_profile(engine)

    def test_introspection_queries_are_not_clamped_by_the_cartesian_guard(self) -> None:
        # Regression: an explicit CROSS JOIN here made db_client clamp the FK result to 100 rows.
        for sql in (PG.schema_query, PG.fk_query):
            assert check_cartesian_explosion(sql, dialect="postgres")[0] is False


class TestSchemaText:
    def test_format_groups_columns_and_keeps_comments(self) -> None:
        text = format_schema_rows(PG, ROWS)
        assert "TABLE gpu_sales (" in text and "TABLE regions (" in text
        assert "revenue numeric  -- Gross revenue in USD" in text and "tags text[]" in text

    def test_schema_sync_splits_per_table(self) -> None:
        parts = _split_schema_by_table(PG, ROWS)
        assert set(parts) == {"gpu_sales", "regions"} and "tags text[]" in parts["gpu_sales"]

    def test_column_registry_knows_postgres_schema(self) -> None:
        reg = ColumnRegistry(format_schema_rows(PG, ROWS), PG.sqlglot_dialect)
        assert set(reg._tables) == {"gpu_sales", "regions"}
        ok = "SELECT revenue FROM gpu_sales WHERE 'x' = ANY(tags) AND id > 1"
        assert reg.validate_columns(ok).is_valid
        assert not reg.validate_columns("SELECT nope FROM gpu_sales").is_valid


DANGEROUS_PG = [
    "SELECT pg_sleep(10)", "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)", "SELECT set_config('a', 'b', false)",
    "SELECT pg_advisory_lock(1)", "SELECT pg_terminate_backend(1)", "SELECT lo_import('/etc/passwd')",
]


class TestSafety:
    @pytest.mark.parametrize("sql", DANGEROUS_PG)
    def test_dangerous_functions_blocked_in_both_guards(self, sql: str) -> None:
        assert is_destructive_sql(sql, dialect="postgres")
        assert not _retriever()._is_safe_read_query(sql)

    @pytest.mark.parametrize("sql", [
        "SELECT date_trunc('month', sold_on), sum(revenue) FROM gpu_sales GROUP BY 1",
        "SELECT model FROM gpu_sales WHERE sold_on >= CURRENT_DATE - INTERVAL '30 days'",
        "SELECT g.model, r.name FROM gpu_sales g JOIN regions r ON r.id = g.region_id",
    ])
    def test_normal_postgres_reads_allowed(self, sql: str) -> None:
        assert not is_destructive_sql(sql, dialect="postgres")
        assert _retriever()._is_safe_read_query(sql)

    @pytest.mark.parametrize("sql", [
        "DELETE FROM regions", "SELECT 1; DROP TABLE regions", "SELECT * INTO newt FROM regions",
    ])
    def test_writes_blocked(self, sql: str) -> None:
        assert not _retriever()._is_safe_read_query(sql)

    def test_existing_mysql_names_still_blocked(self) -> None:
        r = SQLRetriever(router=object())
        r._dialect = get_dialect_profile("mysql")
        assert not r._is_safe_read_query("SELECT SLEEP(10)")
        assert not r._is_safe_read_query("SELECT GET_LOCK('a', 1)")


def test_asyncpg_errors_classify_as_db_errors() -> None:
    assert classify_error("asyncpg.exceptions.InternalServerError: boom") == "db_execution_error"


async def test_named_params_rejected_before_connecting(monkeypatch) -> None:
    pytest.importorskip("asyncpg")
    from src.core import db_client
    with pytest.raises(ValueError, match="Named query parameters"):
        await db_client._execute("postgresql", "SELECT 1", {"a": 1})


# ----------------------------------------------------------------------------
# Opt-in live tests (real PostgreSQL)
# ----------------------------------------------------------------------------
PG_HOST = os.environ.get("POSTGRES_TEST_HOST")
requires_postgres = pytest.mark.skipif(
    not PG_HOST, reason="Set POSTGRES_TEST_HOST (+ _PORT/_USER/_PASSWORD/_DB) to run against a real PostgreSQL."
)

_DDL = """
DROP TABLE IF EXISTS atk_shipments, atk_order_lines, atk_sales, atk_regions CASCADE;
DROP TYPE IF EXISTS atk_mood;
CREATE TYPE atk_mood AS ENUM ('ok', 'bad');
CREATE TABLE atk_regions (id serial PRIMARY KEY, name varchar(50) NOT NULL, mood atk_mood);
CREATE TABLE atk_sales (id serial PRIMARY KEY, model text, revenue numeric(12,2), sold_on date,
    region_id int REFERENCES atk_regions(id), meta jsonb, ref uuid, tags text[],
    created_at timestamptz DEFAULT now());
COMMENT ON COLUMN atk_sales.revenue IS 'Gross revenue in USD';
CREATE TABLE atk_order_lines (order_id int, line_no int, PRIMARY KEY (order_id, line_no));
CREATE TABLE atk_shipments (id serial PRIMARY KEY, order_id int, line_no int,
    FOREIGN KEY (order_id, line_no) REFERENCES atk_order_lines (order_id, line_no));
INSERT INTO atk_regions (name) VALUES ('APAC'), ('EMEA');
INSERT INTO atk_sales (model, revenue, sold_on, region_id, meta, ref, tags) VALUES
  ('RTX 5090', 1999000.50, '2026-09-01', 1, '{"a": 1}', '8b1c2d3e-0000-4000-8000-000000000001', '{x,y}'),
  ('RTX 5080', 2497500.00, '2026-09-15', 2, '{"b": 2}', NULL, '{z}');
"""


@pytest.fixture
async def live_pg(monkeypatch):
    import asyncpg
    from src.core import config

    conn_args = dict(
        host=PG_HOST, port=int(os.environ.get("POSTGRES_TEST_PORT", "5432")),
        user=os.environ.get("POSTGRES_TEST_USER", "postgres"),
        password=os.environ.get("POSTGRES_TEST_PASSWORD", ""),
        database=os.environ.get("POSTGRES_TEST_DB", "postgres"),
    )
    admin = await asyncpg.connect(**conn_args)
    await admin.execute(_DDL)
    for name, val in [("db_engine", "postgresql"), ("db_host", conn_args["host"]),
                      ("db_port", conn_args["port"]), ("db_name", conn_args["database"]),
                      ("db_readonly_user", conn_args["user"]),
                      ("db_readonly_password", conn_args["password"])]:
        monkeypatch.setattr(config.settings, name, val)
    SQLRetriever.clear_schema_cache()
    yield conn_args
    SQLRetriever.clear_schema_cache()
    await admin.execute(
        "DROP TABLE IF EXISTS atk_shipments, atk_order_lines, atk_sales, atk_regions CASCADE;"
        "DROP TYPE IF EXISTS atk_mood;")
    await admin.close()


@requires_postgres
class TestLivePostgres:
    async def test_schema_query_formats_comments_arrays_and_enums(self, live_pg) -> None:
        from src.core.db_client import run_readonly_query
        rows = await run_readonly_query(PG.schema_query, max_rows=20000)
        text = format_schema_rows(PG, [r for r in rows if r["table_name"].startswith("atk_")])
        assert "revenue numeric  -- Gross revenue in USD" in text
        assert "tags text[]" in text and "mood atk_mood" in text

    async def test_foreign_keys_pair_composite_columns(self, live_pg) -> None:
        from src.core.db_client import run_readonly_query
        fks = {(r["table_name"], r["column_name"], r["referenced_table_name"], r["referenced_column_name"])
               for r in await run_readonly_query(PG.fk_query, max_rows=20000)
               if r["table_name"].startswith("atk_")}
        assert fks == {
            ("atk_sales", "region_id", "atk_regions", "id"),
            ("atk_shipments", "order_id", "atk_order_lines", "order_id"),
            ("atk_shipments", "line_no", "atk_order_lines", "line_no"),   # NOT a cross product
        }

    async def test_select_limit_and_json_safe_types(self, live_pg) -> None:
        import json
        from src.core.db_client import run_readonly_query
        from src.stages.s12b_sql_retrieval import _sanitize_rows
        rows = await run_readonly_query("SELECT * FROM atk_sales ORDER BY id", max_rows=1)
        assert len(rows) == 1                                     # LIMIT injected for PostgreSQL
        json.dumps(_sanitize_rows(rows))                          # uuid/numeric/timestamptz/array/jsonb

    async def test_writes_are_rejected_by_the_read_only_transaction(self, live_pg) -> None:
        from src.core.db_client import _execute
        with pytest.raises(Exception, match="read-only"):
            await _execute("postgresql", "DELETE FROM atk_regions", None)
        assert len(await _execute("postgresql", "SELECT 1 FROM atk_regions", None)) == 2

    async def test_timeout_is_clean_and_connection_pool_still_usable(self, live_pg, monkeypatch) -> None:
        import asyncio
        from src.core import db_client
        monkeypatch.setattr(db_client, "QUERY_TIMEOUT_SECONDS", 1)
        with pytest.raises(asyncio.TimeoutError):
            await db_client.run_readonly_query("SELECT pg_sleep(5)")
        assert await db_client.run_readonly_query("SELECT 1 AS x") == [{"x": 1}]

    async def test_retriever_builds_schema_and_column_registry(self, live_pg) -> None:
        r = _retriever()
        schema = await r._fetch_full_schema()
        assert "TABLE atk_sales" in schema and "atk_shipments.order_id -> atk_order_lines.order_id" in schema
        assert "atk_sales" in SQLRetriever._column_registry._tables

    async def test_admin_connection_test_succeeds_and_wrong_password_is_scrubbed(self, live_pg) -> None:
        import src.core.db_settings as dbs
        cfg = {"engine": "postgresql", "host": live_pg["host"], "port": live_pg["port"],
               "database": live_pg["database"], "username": live_pg["user"], "password": live_pg["password"]}
        await dbs._test_connection(cfg)
        with pytest.raises(dbs.DBSettingsError) as exc:
            await dbs._test_connection({**cfg, "password": "definitely-wrong-pw"})
        assert "definitely-wrong-pw" not in str(exc.value)
