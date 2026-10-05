from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.provider_client import ProviderRouter
from src.models.schemas import Chunk, ChunkType, DocumentType, RetrievedChunk
from src.pipeline.query import _format_history
from src.stages.s12_s13_s14_retrieval import Generator


TABLE = "SQL Query Executed: `SELECT region, SUM(amount) FROM sales GROUP BY region`\n\n| region | total |\n|---|---|\n| North | 900000 |\n| South | 600000 |"


def test_format_history_drops_rows_keeps_sql():
    hist = [
        {"role": "user", "content": "total revenue by region?"},
        {"role": "assistant", "content": TABLE, "modelUsed": "sql/direct"},
        {"role": "user", "content": "now only North"},
    ]
    out = _format_history(hist)
    assert "900000" not in out and "| North |" not in out
    assert "SELECT region, SUM(amount) FROM sales GROUP BY region" in out
    assert "User: total revenue by region?" in out and "User: now only North" in out


def test_format_history_template_value_removed():
    hist = [{"role": "assistant", "content": "There are 42 customers.\n\n" + TABLE, "modelUsed": "fast_path/count"}]
    assert "42" not in _format_history(hist)


def test_format_history_doc_answers_untouched():
    hist = [{"role": "assistant", "content": "The refund policy allows 30 days [1].", "modelUsed": "groq/x"}]
    assert _format_history(hist) == "Assistant: The refund policy allows 30 days [1]."


def test_format_history_hybrid_keeps_llm_text():
    hist = [{"role": "assistant", "content": "Policy says 30 days.\n\n" + TABLE, "modelUsed": "groq/x"}]
    out = _format_history(hist)
    assert "Policy says 30 days." in out and "900000" not in out


def _chunk():
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id="c",
            document_id="live_db",
            chunk_type=ChunkType.SQL_RESULT,
            content=TABLE,
            document_type=DocumentType.GENERAL,
        ),
        score=1.0,
    )


@pytest.mark.asyncio
async def test_aggregate_no_llm_when_flag_off():
    router = MagicMock(spec=ProviderRouter)
    router.usage = MagicMock(model_copy=MagicMock(return_value={}))
    router.chat = AsyncMock(return_value="x")
    real = lambda name: name == "fast_path_enabled"
    with patch("src.stages.s12_s13_s14_retrieval.is_feature_enabled", side_effect=real):
        result = await Generator(router=router).generate("What is the total revenue by region?", [_chunk()])
    assert router.chat.await_count == 0
    assert result.model_used == "sql/direct" and "| North | 900000 |" in result.answer


@pytest.mark.asyncio
async def test_aggregate_stream_no_llm_when_flag_off():
    router = MagicMock(spec=ProviderRouter)
    router.usage = MagicMock(model_copy=MagicMock(return_value={}))
    router.chat = AsyncMock(return_value="x")
    real = lambda name: name == "fast_path_enabled"
    with patch("src.stages.s12_s13_s14_retrieval.is_feature_enabled", side_effect=real):
        items = [
            item
            async for item in Generator(router=router).generate_stream(
                "What is the total revenue by region?", [_chunk()]
            )
        ]
    assert router.chat.await_count == 0
    assert items[-1].model_used == "sql/direct"


async def _run_title(messages, llm_reply="Revenue By Region", fail=False):
    from src.api import ui

    seen = {}
    async def fake_chat(self, task, messages, **kwargs):
        seen["prompt"] = messages[0]["content"]
        if fail:
            raise RuntimeError("llm down")
        return llm_reply

    saved = {}
    with patch.object(ui.state_manager, "get_messages", return_value=messages), \
         patch.object(ui, "_check_chat_access"), \
         patch.object(ui.state_manager, "update_chat", side_effect=lambda cid, data: saved.update(data)), \
         patch.object(ui.ProviderRouter, "chat", fake_chat):
        result = await ui.generate_chat_title("c1", current_user="u")
    return result, seen, saved


@pytest.mark.asyncio
async def test_title_uses_only_user_question_never_the_answer():
    messages = [
        {"role": "user", "content": "total revenue by region"},
        {"role": "assistant", "content": TABLE, "modelUsed": "sql/direct"},
    ]
    result, seen, saved = await _run_title(messages)
    assert result["title"] == "Revenue By Region" and saved["title"] == "Revenue By Region"
    assert "total revenue by region" in seen["prompt"]
    for leaked in ("900000", "| North |", "SELECT region", "SQL Query Executed", "Assistant:"):
        assert leaked not in seen["prompt"]


@pytest.mark.asyncio
async def test_title_long_question_is_capped_and_title_stays_short():
    long_question = "please tell me " + "about the warehouse stock levels " * 40
    result, seen, _ = await _run_title(
        [{"role": "user", "content": long_question}],
        llm_reply="Title: Warehouse Stock Levels Overview Report Today Extra Words.",
    )
    assert len(result["title"].split()) <= 6
    assert len(seen["prompt"]) < 1600


@pytest.mark.asyncio
async def test_title_falls_back_when_llm_fails():
    result, _, _ = await _run_title(
        [{"role": "user", "content": "show me sales by state"}], fail=True
    )
    assert result["title"] and len(result["title"].split()) <= 6


@pytest.mark.asyncio
async def test_title_works_before_any_answer_exists():
    result, seen, _ = await _run_title(
        [{"role": "user", "content": "list all warehouses"}], llm_reply="Warehouse List"
    )
    assert result["title"] == "Warehouse List" and "list all warehouses" in seen["prompt"]
