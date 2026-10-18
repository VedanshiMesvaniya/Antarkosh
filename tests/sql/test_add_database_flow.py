"""Adding a database from the admin screen: saving its connection creates its folder; it stays inactive until synced."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.core import db_settings
from src.sql import registry

ROOT = Path(__file__).resolve().parents[2]
ADMIN = {"X-User-Id": "admin"}
BODY = {"engine": "postgresql", "host": "h", "port": 5432, "database": "d", "username": "u", "password": "secret"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    shutil.copytree(ROOT / "databases" / "_template", base / "_template")
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.json"))
    monkeypatch.delenv("DB_CONNECTIONS_FILE", raising=False)
    monkeypatch.setenv("ALLOW_HEADER_AUTH", "1")

    class _Connector:
        def __init__(self, cfg):
            self.cfg = cfg

        async def test(self):
            return None

    monkeypatch.setattr("src.sql.connectors.get_connector", lambda engine, cfg=None: _Connector(cfg or {}))
    monkeypatch.setattr(db_settings, "_apply_runtime", MagicMock())
    return base


def _client():
    from src.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _meta(base: Path, db_id: str) -> dict[str, Any]:
    return yaml.safe_load((base / db_id / "db.yaml").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_saving_a_new_connection_creates_the_database_folder_inactive(env: Path):
    async with _client() as c:
        res = await c.post("/api/databases/qts_ais/connection", json={**BODY, "engine": "mssql"}, headers=ADMIN)
    assert res.status_code == 200 and res.json() == {"ok": True, "status": "saved", "db_id": "qts_ais", "folder_created": True}
    meta = _meta(env, "qts_ais")
    assert (meta["id"], meta["engine"], meta["connection"], meta["enabled"]) == ("qts_ais", "mssql", "qts_ais", False)
    assert (env / "qts_ais" / "schema" / "schema.json").is_file()
    assert registry.is_database_active("qts_ais") is False  # answering stays closed until its first sync


@pytest.mark.asyncio
async def test_saving_again_leaves_the_existing_folder_untouched(env: Path):
    async with _client() as c:
        await c.post("/api/databases/qts_ais/connection", json=BODY, headers=ADMIN)
        (env / "qts_ais" / "notes.txt").write_text("mine", encoding="utf-8")
        before = (env / "qts_ais" / "db.yaml").read_bytes()
        res = await c.post("/api/databases/qts_ais/connection", json={**BODY, "host": "other-host"}, headers=ADMIN)
    assert res.json()["folder_created"] is False
    assert (env / "qts_ais" / "db.yaml").read_bytes() == before and (env / "qts_ais" / "notes.txt").read_text() == "mine"
    assert registry.get_connection("qts_ais")["host"] == "other-host"  # the connection itself was updated


@pytest.mark.asyncio
async def test_the_default_database_never_gets_a_folder_from_this_endpoint(env: Path):
    async with _client() as c:
        res = await c.post("/api/databases/erp_main/connection", json=BODY, headers=ADMIN)
    assert res.status_code == 200 and res.json()["folder_created"] is False
    assert not (env / "erp_main").exists()


@pytest.mark.asyncio
async def test_a_folder_problem_never_fails_the_save(env: Path, monkeypatch):
    monkeypatch.setattr("src.sql.introspection.scaffold.ensure_db_folder", MagicMock(side_effect=OSError("read-only disk")))
    async with _client() as c:
        res = await c.post("/api/databases/qts_ais/connection", json=BODY, headers=ADMIN)
    assert res.status_code == 200 and res.json()["folder_created"] is False
    assert registry.get_connection("qts_ais")["host"] == "h"


@pytest.mark.asyncio
async def test_a_failed_connection_test_creates_nothing(env: Path, monkeypatch):
    class _Bad:
        def __init__(self, cfg):
            pass

        async def test(self):
            raise RuntimeError("Login failed")

    monkeypatch.setattr("src.sql.connectors.get_connector", lambda engine, cfg=None: _Bad(cfg))
    async with _client() as c:
        res = await c.post("/api/databases/qts_ais/connection", json=BODY, headers=ADMIN)
    assert res.status_code == 422 and not (env / "qts_ais").exists()


@pytest.mark.asyncio
async def test_the_edit_form_can_read_a_saved_connection_without_its_password(env: Path):
    async with _client() as c:
        await c.post("/api/databases/qts_ais/connection", json={**BODY, "engine": "postgresql", "schema_name": "erp_food"}, headers=ADMIN)
        res = await c.get("/api/databases/qts_ais/connection", headers=ADMIN)
        missing = await c.get("/api/databases/nope_db/connection", headers=ADMIN)
        bad = await c.get("/api/databases/Bad-Name/connection", headers=ADMIN)
        anon = await c.get("/api/databases/qts_ais/connection")
    body = res.json()
    assert res.status_code == 200 and body["schema_name"] == "erp_food" and body["password_set"] is True
    assert "password" not in body and "secret" not in str(body)
    assert (missing.status_code, bad.status_code) == (404, 400) and anon.status_code in (401, 403)


@pytest.mark.asyncio
async def test_first_successful_sync_makes_a_ui_added_database_active_with_no_flags(env: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    async with _client() as c:
        await c.post("/api/databases/qts_ais/connection", json=BODY, headers=ADMIN)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: False)  # no flags at all
    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", AsyncMock(return_value=[
        {"table_name": "orders", "column_name": "id", "data_type": "int", "column_comment": ""}]))
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(return_value=([[0.1] * 8], [None]))
    monkeypatch.setattr("src.sql.pipeline.clear_knowledge_caches", MagicMock())

    assert registry.is_database_active("qts_ais") is False
    result = await sync_live_schema(embedding_service=emb, vector_store=AsyncMock(), db_id="qts_ais")
    assert result["status"] == "ok"
    assert _meta(env, "qts_ais")["enabled"] is True and registry.is_database_active("qts_ais") is True
