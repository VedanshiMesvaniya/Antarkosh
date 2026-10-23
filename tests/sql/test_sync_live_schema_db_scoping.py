"""Phase 1: sync_live_schema must use the TARGET database's engine and connection.

Before this fix it used the process-wide settings.db_engine for the dialect and
never passed db_id to its catalog queries, so syncing database B read database A.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import settings
from src.sql.dialects import get_dialect_profile
from src.sql.registry import resolve_db_engine

COLUMN_ROWS = [
    {"table_name": "orders", "column_name": "id", "data_type": "int", "column_comment": ""},
    {"table_name": "orders", "column_name": "total", "data_type": "decimal", "column_comment": ""},
]


@pytest.fixture(autouse=True)
def _no_auto_schema_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests check the sync's core behaviour. The per-database schema-intelligence flags default to ON for databases
    without their own entry (``db_defaults``), and each phase would add its own catalog queries, so pin them off here."""
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: False)


@pytest.fixture()
def engines(monkeypatch: pytest.MonkeyPatch):
    """Fake registry: {db_id: {"conn": engine|None, "meta": engine|None}}; default engine mysql."""
    table: dict[str, dict[str, str | None]] = {}

    def fake_get_connection(db_id: str, *a: Any, **k: Any) -> dict[str, Any]:
        eng = table.get(db_id, {}).get("conn")
        return {"engine": eng} if eng else {}

    def fake_get_database(db_id: str, *a: Any, **k: Any) -> dict[str, Any]:
        eng = table.get(db_id, {}).get("meta")
        if eng is None:
            raise FileNotFoundError(db_id)
        return {"engine": eng}

    for mod in ("src.sql.registry", "src.sql.pipeline"):
        monkeypatch.setattr(f"{mod}.get_connection", fake_get_connection)
        monkeypatch.setattr(f"{mod}.get_database", fake_get_database)
    monkeypatch.setattr(settings, "db_engine", "mysql")
    return table


def _mocks():
    store = AsyncMock()
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(return_value=([[0.1] * 8], [None]))
    return emb, store


# ---------------------------------------------------------------- resolve_db_engine


def test_resolve_prefers_connection_then_metadata_then_settings(engines):
    engines["a"] = {"conn": "postgresql", "meta": "sqlite"}
    engines["b"] = {"conn": None, "meta": "sqlite"}
    assert resolve_db_engine("a") == "postgresql"
    assert resolve_db_engine("b") == "sqlite"
    assert resolve_db_engine("unknown_db") == "mysql"  # settings fallback
    assert resolve_db_engine("") == resolve_db_engine("erp_main") == "mysql"


@pytest.mark.parametrize(
    "conn,meta,expected",
    [("postgresql", "sqlite", "postgresql"), (None, "sqlite", "sqlite"), (None, None, "mysql")],
)
def test_resolve_matches_sqlretriever_resolution(engines, conn, meta, expected):
    """The helper must agree with what queries already do (SQLRetriever.__init__)."""
    from src.sql.pipeline import SQLRetriever

    engines["xdb"] = {"conn": conn, "meta": meta}
    retriever = SQLRetriever(AsyncMock(), None, None, db_id="xdb")
    assert retriever._dialect.key == expected == resolve_db_engine("xdb")


# ---------------------------------------------------------------- sync_live_schema


@pytest.mark.asyncio
async def test_sync_uses_target_engine_and_passes_db_id_to_every_query(engines, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    engines["pg_db"] = {"conn": "postgresql", "meta": None}  # global default stays mysql
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_run(sql: str, *a: Any, **kw: Any):
        calls.append((sql, kw))
        return COLUMN_ROWS if sql == get_dialect_profile("postgresql").schema_query else []

    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", fake_run)
    emb, store = _mocks()

    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="pg_db")

    pg = get_dialect_profile("postgresql")
    assert result["status"] == "ok" and result["db_id"] == "pg_db"
    assert [c[0] for c in calls] == [pg.schema_query, pg.fk_query]  # postgres SQL, not mysql SQL
    assert all(kw.get("db_id") == "pg_db" for _, kw in calls)


@pytest.mark.asyncio
async def test_sync_sqlite_fk_lookup_is_scoped_to_target_db(engines, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    engines["lite_db"] = {"conn": "sqlite", "meta": None}
    run = AsyncMock(return_value=[{"name": "orders", "sql": "CREATE TABLE orders (id INT)"}])
    fk = AsyncMock(return_value=[])
    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", run)
    monkeypatch.setattr("src.sql.generation.fetch_sqlite_foreign_keys", fk)
    emb, store = _mocks()

    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="lite_db")

    assert result["status"] == "ok"
    assert run.call_args.kwargs["db_id"] == "lite_db"
    fk.assert_awaited_once_with(db_id="lite_db")


@pytest.mark.asyncio
async def test_default_db_still_syncs_with_default_engine_and_db_id(engines, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_run(sql: str, *a: Any, **kw: Any):
        seen.append((sql, kw))
        return COLUMN_ROWS if sql == get_dialect_profile("mysql").schema_query else []

    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", fake_run)
    emb, store = _mocks()

    result = await sync_live_schema(embedding_service=emb, vector_store=store)  # db_id defaults to erp_main

    mysql = get_dialect_profile("mysql")
    assert result["status"] == "ok" and result["db_id"] == "erp_main"
    assert [s for s, _ in seen] == [mysql.schema_query, mysql.fk_query]
    assert all(kw.get("db_id") == "erp_main" for _, kw in seen)


@pytest.mark.asyncio
async def test_two_databases_with_different_engines_do_not_interfere(engines, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    engines["pg_db"] = {"conn": "postgresql", "meta": None}
    used: dict[str, list[str]] = {}

    async def fake_run(sql: str, *a: Any, **kw: Any):
        used.setdefault(kw.get("db_id"), []).append(sql)
        return COLUMN_ROWS if "information_schema.columns" in sql else []

    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", fake_run)
    for db in ("erp_main", "pg_db", "erp_main"):
        emb, store = _mocks()
        assert (await sync_live_schema(embedding_service=emb, vector_store=store, db_id=db))["status"] == "ok"

    assert used["erp_main"][0] == get_dialect_profile("mysql").schema_query
    assert used["pg_db"][0] == get_dialect_profile("postgresql").schema_query


@pytest.mark.asyncio
async def test_unsupported_engine_still_fails_loudly(engines, monkeypatch):
    """An engine with no dialect profile must raise, not silently use another engine's SQL."""
    from src.sql.schema_retrieval import sync_live_schema

    engines["db2_db"] = {"conn": "db2", "meta": None}
    run = AsyncMock(return_value=[])
    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", run)
    emb, store = _mocks()

    with pytest.raises(ValueError, match="Unsupported db_engine"):
        await sync_live_schema(embedding_service=emb, vector_store=store, db_id="db2_db")
    run.assert_not_awaited()  # no query was sent to the wrong database
