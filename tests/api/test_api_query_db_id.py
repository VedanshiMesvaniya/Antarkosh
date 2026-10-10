"""Tests for Step D7: db_id propagation across API, pipeline, and metrics."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from src.core.pipeline_metrics import CURRENT_DB_ID, log_event
from src.main import app
from src.models.schemas import QueryResult
from src.sql.context import DEFAULT_DB_ID


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_api_query_without_db_id_defaults_to_erp_main(monkeypatch):
    captured = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):
            captured["init_db_id"] = kwargs.get("db_id")

        async def query(self, question, filters=None, db_id=None, **_):
            captured["query_db_id"] = db_id
            captured["filters"] = filters
            return QueryResult(query=question, answer="all good", model_used="test-model")

    monkeypatch.setattr("src.api.query.QueryPipeline", FakePipeline)

    async with _client() as c:
        res = await c.post(
            "/api/query",
            json={"question": "What are our revenue figures?"},
            headers={"X-User-Id": "admin"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["answer"] == "all good"

    assert captured["init_db_id"] == DEFAULT_DB_ID
    assert captured["query_db_id"] == DEFAULT_DB_ID


@pytest.mark.asyncio
async def test_api_query_with_valid_db_id(monkeypatch):
    captured = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):
            captured["init_db_id"] = kwargs.get("db_id")

        async def query(self, question, filters=None, db_id=None, **_):
            captured["query_db_id"] = db_id
            return QueryResult(query=question, answer="crm response", model_used="test-model")

    monkeypatch.setattr("src.api.query.QueryPipeline", FakePipeline)

    async with _client() as c:
        res = await c.post(
            "/api/query",
            json={"question": "Find leads", "db_id": "crm_sales"},
            headers={"X-User-Id": "admin"},
        )
        assert res.status_code == 200
        assert res.json()["answer"] == "crm response"

    assert captured["init_db_id"] == "crm_sales"
    assert captured["query_db_id"] == "crm_sales"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_db_id",
    [
        "123start_with_number",
        "has spaces",
        "has-hyphens",
        "UPPERCASE",
        "../traversal",
        "semi;colon",
        "a" * 65,  # too long
    ],
)
async def test_api_query_with_invalid_db_id_returns_400(invalid_db_id):
    async with _client() as c:
        res = await c.post(
            "/api/query",
            json={"question": "Find leads", "db_id": invalid_db_id},
            headers={"X-User-Id": "admin"},
        )
        assert res.status_code == 400
        assert "Invalid db_id" in res.json().get("detail", "")


@pytest.mark.asyncio
async def test_ui_chat_routes_db_id_validation_and_propagation(monkeypatch):
    captured = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):
            captured["init_db_id"] = kwargs.get("db_id")

        async def query(self, question, filters=None, history=None, mode="auto", db_id=None):
            captured["query_db_id"] = db_id
            return QueryResult(
                query=question,
                answer="chat answer",
                model_used="test-model",
            )

    monkeypatch.setattr("src.api.ui.QueryPipeline", FakePipeline)
    monkeypatch.setattr("src.api.ui._check_chat_access", lambda chat_id, user: None)

    async with _client() as c:
        # 1. Invalid db_id -> 400
        res = await c.post(
            "/api/chats/test-chat/messages",
            json={"message": "hello", "db_id": "invalid-id"},
            headers={"X-User-Id": "admin"},
        )
        assert res.status_code == 400
        assert "Invalid db_id" in res.json().get("detail", "")

        # 2. Absent db_id -> default erp_main
        res = await c.post(
            "/api/chats/test-chat/messages",
            json={"message": "hello default"},
            headers={"X-User-Id": "admin"},
        )
        assert res.status_code == 200
        assert captured["init_db_id"] == DEFAULT_DB_ID
        assert captured["query_db_id"] == DEFAULT_DB_ID

        # 3. Explicit valid db_id -> target db
        res = await c.post(
            "/api/chats/test-chat/messages",
            json={"message": "hello analytics", "db_id": "analytics_dw"},
            headers={"X-User-Id": "admin"},
        )
        assert res.status_code == 200
        assert captured["init_db_id"] == "analytics_dw"
        assert captured["query_db_id"] == "analytics_dw"


def test_pipeline_metrics_logs_db_id(tmp_path, monkeypatch):
    metrics_path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr("src.core.pipeline_metrics.METRICS_FILE", metrics_path)

    # 1. Default when no db_id is provided
    log_event("query_success", query="select 1")
    # 2. ContextVar override
    token = CURRENT_DB_ID.set("context_db")
    try:
        log_event("query_success", query="select 2")
    finally:
        CURRENT_DB_ID.reset(token)
    # 3. Explicit db_id kwarg
    log_event("query_success", query="select 3", db_id="explicit_db")
    # 4. Details dictionary db_id
    log_event("routing_decision", query="select 4", details={"db_id": "details_db"})

    assert metrics_path.exists()
    lines = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 4
    assert lines[0]["db_id"] == DEFAULT_DB_ID
    assert lines[1]["db_id"] == "context_db"
    assert lines[2]["db_id"] == "explicit_db"
    assert lines[3]["db_id"] == "details_db"


@pytest.mark.asyncio
async def test_query_pipeline_routes_to_scoped_retriever(monkeypatch):
    from src.pipeline.query import QueryPipeline

    pipeline = QueryPipeline()
    retriever_erp = pipeline._get_sql_retriever(DEFAULT_DB_ID)
    assert retriever_erp.db_id == DEFAULT_DB_ID

    retriever_other = pipeline._get_sql_retriever("other_db")
    assert retriever_other.db_id == "other_db"
    assert retriever_other is not retriever_erp
