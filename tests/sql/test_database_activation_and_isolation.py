"""Phase 2b: a database answers questions only once it is active, and databases never share a connection.

* ``enabled: false`` (the template default) blocks answering; a first successful sync turns it on.
* A non-default database without its own complete connection is refused instead of silently
  falling back to the default database's host, user, password and name.
* Concurrent requests for different databases keep separate state.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.core import db_client
from src.core.config import settings
from src.sql import registry
from src.sql.introspection import scaffold
from src.sql.introspection.scaffold import activate_after_sync, ensure_db_folder

REAL_TEMPLATE = Path(__file__).resolve().parents[2] / "databases" / "_template"


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    shutil.copytree(REAL_TEMPLATE, base / "_template")
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    monkeypatch.setattr(settings, "db_engine", "mysql")
    return base


def _write_db(base: Path, db_id: str, **fields: Any) -> Path:
    folder = base / db_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "db.yaml").write_text(
        yaml.safe_dump({"id": db_id, "engine": "mysql", "allowed_users": ["admin"], **fields}), encoding="utf-8"
    )
    return folder / "db.yaml"


def _meta(base: Path, db_id: str) -> dict[str, Any]:
    return yaml.safe_load((base / db_id / "db.yaml").read_text(encoding="utf-8"))


def _flag(monkeypatch, on: bool = True):
    """Turn the Phase 2 flag on/off; every other per-DB flag stays off so these tests stay about Phase 2."""
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: on and flag == "auto_scaffold")


# ------------------------------------------------------------------ is_database_active


def test_default_database_is_always_active(dbs: Path):
    _write_db(dbs, "erp_main", enabled=False)  # even a stray false cannot switch erp_main off
    assert registry.is_database_active("erp_main") is True
    assert registry.is_database_active("") is True


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"enabled": False}, False),
        ({"enabled": True}, True),
        ({}, True),  # no key: active, as before
        ({"enabled": "false"}, False),
        ({"enabled": "No"}, False),
        ({"enabled": "true"}, True),
    ],
)
def test_active_follows_enabled_flag(dbs: Path, fields, expected):
    _write_db(dbs, "shop_db", **fields)
    assert registry.is_database_active("shop_db") is expected


def test_database_without_a_folder_keeps_todays_behaviour(dbs: Path):
    assert registry.is_database_active("never_scaffolded") is True


def test_unreadable_db_yaml_fails_closed(dbs: Path):
    (dbs / "broken_db").mkdir()
    (dbs / "broken_db" / "db.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    assert registry.is_database_active("broken_db") is False


def test_scaffolded_database_starts_inactive(dbs: Path):
    ensure_db_folder("fresh_db", engine="mysql")
    assert registry.is_database_active("fresh_db") is False


# ------------------------------------------------------------------ activation after sync


def test_first_successful_sync_turns_the_database_on_and_stamps_the_time(dbs: Path, monkeypatch):
    _flag(monkeypatch)
    ensure_db_folder("fresh_db", engine="mysql")
    assert activate_after_sync("fresh_db") is True
    meta = _meta(dbs, "fresh_db")
    assert meta["enabled"] is True and meta["schema_sync"]["last_synced_at"]
    assert registry.is_database_active("fresh_db") is True


def test_resync_never_overrides_an_admin_who_disabled_it(dbs: Path, monkeypatch):
    _flag(monkeypatch)
    ensure_db_folder("fresh_db", engine="mysql")
    activate_after_sync("fresh_db")
    first_stamp = _meta(dbs, "fresh_db")["schema_sync"]["last_synced_at"]

    meta = _meta(dbs, "fresh_db")
    meta["enabled"] = False  # admin turns it off on purpose
    (dbs / "fresh_db" / "db.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")

    assert activate_after_sync("fresh_db") is False
    after = _meta(dbs, "fresh_db")
    assert after["enabled"] is False and after["schema_sync"]["last_synced_at"] >= first_stamp


def test_other_fields_survive_activation(dbs: Path, monkeypatch):
    _flag(monkeypatch)
    ensure_db_folder("fresh_db", engine="postgresql", display_name="Fresh")
    before = _meta(dbs, "fresh_db")
    activate_after_sync("fresh_db")
    after = _meta(dbs, "fresh_db")
    for key in ("id", "display_name", "engine", "connection", "allowed_users", "soft_delete", "default_filters"):
        assert after[key] == before[key], key


def test_activation_needs_no_flag_but_never_touches_a_pinned_database_or_a_missing_folder(dbs: Path, monkeypatch):
    ensure_db_folder("fresh_db", engine="mysql")
    _flag(monkeypatch, on=False)  # no per-DB flag is needed: the folder only exists because an admin added the database
    assert activate_after_sync("fresh_db") is True
    assert _meta(dbs, "fresh_db")["enabled"] is True

    _write_db(dbs, "erp_main", enabled=False)
    before = (dbs / "erp_main" / "db.yaml").read_bytes()
    assert activate_after_sync("erp_main") is False
    assert (dbs / "erp_main" / "db.yaml").read_bytes() == before
    assert activate_after_sync("no_folder_db") is False


def test_activation_never_raises(dbs: Path, monkeypatch):
    _flag(monkeypatch)
    ensure_db_folder("fresh_db", engine="mysql")
    monkeypatch.setattr(registry, "atomic_write_yaml", MagicMock(side_effect=OSError("disk full")))
    assert activate_after_sync("fresh_db") is False


def _sync_mocks(monkeypatch, rows):
    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", AsyncMock(return_value=rows))
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": "sqlite"})
    monkeypatch.setattr("src.sql.generation.fetch_sqlite_foreign_keys", AsyncMock(return_value=[]))
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(return_value=([[0.1] * 8] * max(len(rows), 1), [None] * max(len(rows), 1)))
    return emb, AsyncMock()


@pytest.mark.asyncio
async def test_sync_scaffolds_then_activates_only_on_success(dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    _flag(monkeypatch)
    monkeypatch.setattr("src.sql.pipeline.clear_knowledge_caches", MagicMock())

    # a sync that fails (no schema rows) leaves the new database inactive
    emb, store = _sync_mocks(monkeypatch, [])
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="shop_db")
    assert result["status"] == "error"
    assert _meta(dbs, "shop_db")["enabled"] is False
    assert registry.is_database_active("shop_db") is False

    # a sync that succeeds turns it on
    emb, store = _sync_mocks(monkeypatch, [{"name": "orders", "sql": "CREATE TABLE orders (id INT)"}])
    result = await sync_live_schema(embedding_service=emb, vector_store=store, db_id="shop_db")
    assert result["status"] == "ok" and set(result) == {"status", "db_id", "document_id", "tables_synced", "table_names"}
    assert _meta(dbs, "shop_db")["enabled"] is True and registry.is_database_active("shop_db") is True


@pytest.mark.asyncio
async def test_sync_with_flag_off_writes_nothing(dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    _flag(monkeypatch, on=False)
    emb, store = _sync_mocks(monkeypatch, [{"name": "orders", "sql": "CREATE TABLE orders (id INT)"}])
    assert (await sync_live_schema(embedding_service=emb, vector_store=store, db_id="shop_db"))["status"] == "ok"
    assert not (dbs / "shop_db").exists()


# ------------------------------------------------------------------ API gates


def _client():
    from src.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FakePipeline:
    inits: list[Any] = []

    def __init__(self, *a: Any, **kw: Any):
        _FakePipeline.inits.append(kw.get("db_id"))

    async def query(self, question, filters=None, db_id=None, **_):
        from src.models.schemas import QueryResult

        return QueryResult(query=question, answer="ok", model_used="test")


@pytest.mark.asyncio
async def test_query_api_refuses_an_inactive_database_even_for_admin(dbs: Path, monkeypatch):
    _write_db(dbs, "crm_sales", enabled=False)
    _FakePipeline.inits = []
    monkeypatch.setattr("src.api.query.QueryPipeline", _FakePipeline)

    async with _client() as c:
        res = await c.post("/api/query", json={"question": "Find leads", "db_id": "crm_sales"}, headers={"X-User-Id": "admin"})
    assert res.status_code == 409 and "not active" in res.json()["detail"]
    assert _FakePipeline.inits == []  # nothing was built, so nothing could be fetched


@pytest.mark.asyncio
async def test_query_api_serves_active_and_default_databases(dbs: Path, monkeypatch):
    _write_db(dbs, "crm_sales", enabled=True)
    _FakePipeline.inits = []
    monkeypatch.setattr("src.api.query.QueryPipeline", _FakePipeline)

    async with _client() as c:
        active = await c.post("/api/query", json={"question": "Find leads", "db_id": "crm_sales"}, headers={"X-User-Id": "admin"})
        default = await c.post("/api/query", json={"question": "Revenue?"}, headers={"X-User-Id": "admin"})
    assert active.status_code == 200 and default.status_code == 200
    assert _FakePipeline.inits == ["crm_sales", "erp_main"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["messages", "messages/stream"])
async def test_chat_endpoints_refuse_an_inactive_database(dbs: Path, monkeypatch, suffix):
    _write_db(dbs, "crm_sales", enabled=False)
    fake_state = MagicMock()
    fake_state.get_messages.return_value = []
    monkeypatch.setattr("src.api.ui.chats.state_manager", fake_state)
    monkeypatch.setattr("src.api.ui.chats._check_chat_access", lambda *a, **k: None)
    _FakePipeline.inits = []
    monkeypatch.setattr("src.api.ui.chats.QueryPipeline", _FakePipeline)

    async with _client() as c:
        res = await c.post(f"/api/chats/c1/{suffix}", json={"message": "hi", "db_id": "crm_sales"}, headers={"X-User-Id": "admin"})
    assert res.status_code == 409 and "not active" in res.json()["detail"]
    assert _FakePipeline.inits == []


@pytest.mark.asyncio
async def test_database_list_hides_inactive_databases_from_users_but_not_admins(dbs: Path):
    from src.api.ui.databases import list_databases_endpoint

    _write_db(dbs, "live_db", enabled=True, allowed_users=["admin", "rahul"])
    _write_db(dbs, "pending_db", enabled=False, allowed_users=["admin", "rahul"])

    as_user = {d["id"] for d in await list_databases_endpoint(current_user="rahul")}
    as_admin = {d["id"]: d["enabled"] for d in await list_databases_endpoint(current_user="admin")}
    assert as_user == {"live_db"}
    assert as_admin == {"live_db": True, "pending_db": False}


# ------------------------------------------------------------------ connection isolation


class _SpyConnector:
    """Stands in for a real connector; records the config each query was given."""

    seen: list[dict[str, Any]] = []

    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = dict(cfg or {})

    async def run_readonly(self, sql: str, params: Any = None):
        await asyncio.sleep(0)  # yield, so concurrent queries really interleave
        _SpyConnector.seen.append(self.cfg)
        return [{"db": self.cfg.get("database")}]


@pytest.fixture()
def spy(monkeypatch):
    _SpyConnector.seen = []
    monkeypatch.setattr("src.sql.connectors.get_connector", lambda engine, cfg=None: _SpyConnector(cfg))
    return _SpyConnector


def _connections(monkeypatch, table: dict[str, dict[str, Any]]):
    monkeypatch.setattr("src.sql.registry.get_connection", lambda db_id, *a, **k: dict(table.get(db_id, {})))
    monkeypatch.setattr("src.sql.registry.get_database", lambda db_id, *a, **k: (_ for _ in ()).throw(FileNotFoundError(db_id)))


@pytest.mark.asyncio
async def test_unregistered_database_is_refused_instead_of_borrowing_the_default_connection(spy, monkeypatch):
    monkeypatch.setattr(settings, "db_engine", "mysql")
    monkeypatch.setattr(settings, "db_host", "erp-prod-server")
    _connections(monkeypatch, {})

    with pytest.raises(db_client.DatabaseNotConfiguredError, match="no complete connection") as exc:
        await db_client.run_readonly_query("SELECT 1", db_id="ghost_db")
    assert "erp-prod-server" not in str(exc.value)  # names missing fields only, never values
    assert spy.seen == []  # no connector was even created


@pytest.mark.asyncio
async def test_partial_connection_is_refused_and_names_the_missing_fields(spy, monkeypatch):
    monkeypatch.setattr(settings, "db_engine", "mysql")
    _connections(monkeypatch, {"half_db": {"engine": "mysql", "host": "h", "port": 3306}})
    with pytest.raises(db_client.DatabaseNotConfiguredError, match=r"missing: database, username"):
        await db_client.run_readonly_query("SELECT 1", db_id="half_db")
    assert spy.seen == []


@pytest.mark.asyncio
async def test_complete_connection_uses_its_own_config(spy, monkeypatch):
    monkeypatch.setattr(settings, "db_engine", "mysql")
    own = {"engine": "postgresql", "host": "pg", "port": 5432, "database": "sales", "username": "ro", "password": ""}
    _connections(monkeypatch, {"sales_db": own})
    rows = await db_client.run_readonly_query("SELECT 1", db_id="sales_db")
    assert rows == [{"db": "sales"}] and spy.seen[0]["host"] == "pg"


@pytest.mark.asyncio
async def test_oracle_service_name_requirement_accepts_the_database_alias(spy, monkeypatch):
    own = {"engine": "oracle", "host": "ora", "database": "XEPDB1", "username": "hr"}
    _connections(monkeypatch, {"hr_db": own})
    assert await db_client.run_readonly_query("SELECT 1 FROM DUAL", db_id="hr_db") == [{"db": "XEPDB1"}]


@pytest.mark.asyncio
async def test_default_database_still_uses_its_global_settings_fallback(spy, monkeypatch):
    monkeypatch.setattr(settings, "db_engine", "mysql")
    _connections(monkeypatch, {})  # erp_main with an empty saved config: legacy fallback preserved
    rows = await db_client.run_readonly_query("SELECT 1")
    assert rows and len(spy.seen) == 1


@pytest.mark.asyncio
async def test_sqlite_databases_are_unaffected_by_the_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_engine", "sqlite")
    _connections(monkeypatch, {})
    monkeypatch.setattr(db_client, "DATA_DIR", tmp_path)
    assert await db_client.run_readonly_query("SELECT 1", db_id="lite_db") == []  # no file yet: empty, as before


@pytest.mark.asyncio
async def test_sync_of_an_unregistered_database_fails_before_touching_anything(dbs: Path, monkeypatch, spy):
    from src.sql.schema_retrieval import sync_live_schema

    _flag(monkeypatch)
    monkeypatch.setattr(settings, "db_engine", "mysql")
    _connections(monkeypatch, {})
    emb, store = AsyncMock(), AsyncMock()
    with pytest.raises(db_client.DatabaseNotConfiguredError):
        await sync_live_schema(embedding_service=emb, vector_store=store, db_id="ghost_db")
    store.upsert.assert_not_awaited()
    assert _meta(dbs, "ghost_db")["enabled"] is False  # scaffolded inactive, and the failed sync never turned it on


@pytest.mark.asyncio
async def test_two_clients_on_different_databases_never_share_connection_or_context(spy, monkeypatch):
    from src.sql.learning.pipeline_metrics import CURRENT_DB_ID

    monkeypatch.setattr(settings, "db_engine", "mysql")
    _connections(
        monkeypatch,
        {
            "sales_db": {"engine": "mysql", "host": "h1", "port": 1, "database": "sales", "username": "u1"},
            "hr_db": {"engine": "postgresql", "host": "h2", "port": 2, "database": "hr", "username": "u2"},
        },
    )

    async def client(db_id: str):
        CURRENT_DB_ID.set(db_id)
        out = []
        for _ in range(5):
            out.append((await db_client.run_readonly_query("SELECT 1", db_id=db_id))[0]["db"])
            await asyncio.sleep(0)
            assert CURRENT_DB_ID.get() == db_id  # the other client's request never changed ours
        return out

    a, b = await asyncio.gather(asyncio.create_task(client("sales_db")), asyncio.create_task(client("hr_db")))
    assert set(a) == {"sales"} and set(b) == {"hr"}
    assert {c["host"] for c in spy.seen} == {"h1", "h2"}
