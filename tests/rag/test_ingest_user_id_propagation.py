"""Regression: the SSE ingest path must carry the uploader's user_id through chunking and commit."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline.ingestion import IngestionPipeline


def _pipeline():
    emb = MagicMock()
    emb.embed_chunks = AsyncMock(
        side_effect=lambda chunks: (
            [[0.1] * 4 for _ in chunks],
            [MagicMock(is_empty=lambda: False) for _ in chunks],
        )
    )
    store = MagicMock()
    store.upsert = AsyncMock()
    router = MagicMock()
    router.chat = AsyncMock(return_value="{}")
    pipe = IngestionPipeline(router=router, embedding_service=emb, vector_store=store)
    pipe._registry = MagicMock()
    return pipe, store


@pytest.mark.asyncio
async def test_staged_ingest_completes_and_tags_chunks_with_user(tmp_path):
    f = tmp_path / "probe.html"
    f.write_text(
        "<html><body><h1>T</h1>"
        + "<p>Warehouse text for chunking. </p>" * 60
        + "</body></html>"
    )
    pipe, store = _pipeline()
    events = [
        e
        async for e in pipe._staged_ingest_with_progress(
            f, content_hash="h", supersedes=None, user_id="alice"
        )
    ]
    errors = [e for e in events if e.get("type") == "progress" and e.get("status") == "error"]
    assert not errors, errors
    assert events[-1]["type"] == "complete"
    stored_chunks = store.upsert.await_args.args[0]
    assert stored_chunks and all(c.metadata["user_id"] == "alice" for c in stored_chunks)


@pytest.mark.asyncio
async def test_staged_ingest_defaults_to_system_user(tmp_path):
    f = tmp_path / "probe.html"
    f.write_text("<html><body>" + "<p>More warehouse text. </p>" * 60 + "</body></html>")
    pipe, store = _pipeline()
    events = [
        e
        async for e in pipe._staged_ingest_with_progress(
            f, content_hash="h", supersedes=None
        )
    ]
    assert events[-1]["type"] == "complete"
    assert all(c.metadata["user_id"] == "system" for c in store.upsert.await_args.args[0])
