"""Session-store auth and access-control hardening."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.auth import ALPHA_USERS, SessionStore
from src.main import app
from src.pipeline.query import _cache_acl_signature
from src.stages.s11_vector_store import QdrantStore


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_forged_username_cookie_is_rejected(monkeypatch):
    monkeypatch.delenv("ALLOW_HEADER_AUTH", raising=False)
    async with _client() as c:
        res = await c.get("/api/users", cookies={"alpha_session": "admin"})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_header_auth_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("ALLOW_HEADER_AUTH", raising=False)
    async with _client() as c:
        res = await c.get("/api/users", headers={"X-User-Id": "admin"})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_login_session_logout_cycle(monkeypatch):
    monkeypatch.delenv("ALLOW_HEADER_AUTH", raising=False)
    async with _client() as c:
        res = await c.post("/api/auth/login", json={"username": "mihir", "password": ALPHA_USERS["mihir"]})
        assert res.status_code == 200
        sid = res.cookies.get("alpha_session")
        assert sid and sid != "mihir"
        assert (await c.get("/api/auth/me")).json()["user_id"] == "mihir"
        assert (await c.get("/api/chats")).status_code == 200
        # a normal user is not admin
        assert (await c.get("/api/users")).status_code == 403
        await c.post("/api/auth/logout")
        assert (await c.get("/api/chats")).status_code == 401


@pytest.mark.asyncio
async def test_wrong_password_rejected():
    async with _client() as c:
        res = await c.post("/api/auth/login", json={"username": "mihir", "password": "nope"})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_anonymous_cannot_list_or_query():
    async with _client() as c:
        for method, url in (("get", "/api/chats"), ("get", "/api/documents")):
            assert (await getattr(c, method)(url)).status_code == 401
        res = await c.post("/api/query", json={"question": "hello"})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_client_cannot_choose_its_own_user_in_query(monkeypatch):
    seen = {}

    class FakePipeline:
        async def query(self, question, filters=None, **_):
            seen.update(filters or {})
            from src.models.schemas import QueryResult
            return QueryResult(query=question, answer="ok", model_used="fake")

    monkeypatch.setattr("src.api.query.QueryPipeline", lambda *a, **k: FakePipeline())
    async with _client() as c:
        res = await c.post(
            "/api/query",
            json={"question": "x", "filters": {"user_id": "admin", "allowed_document_ids": None}},
            headers={"X-User-Id": "rahul"},
        )
        assert res.status_code == 200
    assert seen["user_id"] == "rahul" and "allowed_document_ids" not in seen


def test_session_store_expiry_and_persistence(tmp_path):
    path = tmp_path / "s.json"
    store = SessionStore(path)
    sid = store.create("mihir")
    assert SessionStore(path).get(sid) == "mihir"          # survives a restart
    assert "mihir" in path.read_text() and sid not in path.read_text()  # only a hash on disk
    store._sessions[store._key(sid)]["expires"] = 0
    assert store.get(sid) is None
    assert store.get("garbage") is None and store.get(None) is None


def test_chunk_owner_clause_skipped_when_doc_level_acl_applies():
    from qdrant_client.models import Filter

    with_acl = QdrantStore._build_filter({"user_id": "rahul", "allowed_document_ids": ["d1"]})
    assert [c.key for c in with_acl.must] == ["document_id"]
    legacy = QdrantStore._build_filter({"user_id": "rahul"})
    assert len(legacy.must) == 1 and isinstance(legacy.must[0], Filter)  # owner clause kept without a doc-level list


def test_cache_signature_changes_when_access_changes(tmp_path, monkeypatch):
    from unittest.mock import patch
    from src.core.ingestion_registry import IngestionRegistry

    f = tmp_path / "doc.pdf"
    f.write_text("x")
    reg = IngestionRegistry(registry_path=tmp_path / "r.json")
    entry = reg.create_version(f, "h", 1, user_id="admin", allowed_users=["rahul"])
    with patch("src.core.ingestion_registry.IngestionRegistry.get_active", side_effect=reg.get_active):
        before = _cache_acl_signature("rahul")
        reg.update_document_access(entry["document_id"], [])
        after = _cache_acl_signature("rahul")
        assert before != after
        assert _cache_acl_signature("admin") == "admin"
