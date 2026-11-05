"""The feedback route keeps working exactly as before; Phase 14 learning rides along without changing its response."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.auth import ALPHA_USERS
from src.api.ui import chats
from src.main import app
from src.sql.learning import term_pairs as tp


class FakeState:
    def __init__(self):
        self.messages = [
            {"id": "u1", "role": "user", "content": "which supplier is best"},
            {"id": "a1", "role": "assistant", "content": "x", "dbId": "shop", "sqlPayload": {"query": "SELECT 1", "row_count": 2}},
        ]
        self.saved: list = []

    def set_message_feedback(self, chat_id, message_id, feedback, comment=None):
        if not any(m["id"] == message_id for m in self.messages):
            return False
        self.saved.append((chat_id, message_id, feedback, comment))
        return True

    def get_messages(self, chat_id):
        return self.messages

    def get_chats(self):
        return [{"id": "c1", "userId": "mihir"}]


@pytest.fixture()
def wired(monkeypatch):
    state, calls = FakeState(), []
    monkeypatch.setattr(chats, "state_manager", state)
    monkeypatch.setattr(chats, "_log_pipeline_event", lambda *a, **k: None)
    import src.utils.feature_flags as ff

    monkeypatch.setattr(ff, "is_db_feature_enabled", lambda f, d: f == "auto_learning")
    monkeypatch.setattr(tp, "record_feedback", lambda db_id, **kw: calls.append((db_id, kw)) or {})
    return state, calls


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_anonymous_rating_still_saves_and_responds_the_same_but_is_not_learned(wired):
    state, calls = wired
    async with _client() as c:
        res = await c.post("/api/chats/c1/messages/a1/feedback", json={"feedback": "up"})
    assert res.status_code == 200 and res.json() == {"status": "ok", "feedback": "up"}
    assert state.saved == [("c1", "a1", "up", None)]
    assert calls == []


@pytest.mark.asyncio
async def test_logged_in_owner_rating_is_recorded_after_the_response(wired):
    state, calls = wired
    async with _client() as c:
        await c.post("/api/auth/login", json={"username": "mihir", "password": ALPHA_USERS["mihir"]})
        res = await c.post("/api/chats/c1/messages/a1/feedback", json={"feedback": "down"})
        removed = await c.post("/api/chats/c1/messages/a1/feedback", json={})
    assert res.status_code == 200 and res.json() == {"status": "ok", "feedback": "down"}
    assert removed.status_code == 200
    assert [(d, kw["signal"], kw["user_id"], kw["question"]) for d, kw in calls] == [
        ("shop", "down", "mihir", "which supplier is best"),
        ("shop", None, "mihir", "which supplier is best"),
    ]


@pytest.mark.asyncio
async def test_errors_are_unchanged_and_a_learning_failure_never_changes_the_response(wired, monkeypatch):
    del wired  # fixture only installs the fakes
    async with _client() as c:
        assert (await c.post("/api/chats/c1/messages/a1/feedback", json={"feedback": "maybe"})).status_code == 400
        assert (await c.post("/api/chats/c1/messages/nope/feedback", json={"feedback": "up"})).status_code == 404
        monkeypatch.setattr(tp, "record_feedback", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        await c.post("/api/auth/login", json={"username": "mihir", "password": ALPHA_USERS["mihir"]})
        res = await c.post("/api/chats/c1/messages/a1/feedback", json={"feedback": "up"})
    assert res.status_code == 200 and res.json()["feedback"] == "up"
