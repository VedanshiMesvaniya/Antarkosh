"""Tests for src.sql.pipeline and re-exports in src.stages.s12b_sql_retrieval."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.stages.s12b_sql_retrieval as s12b
from src.core.provider_client import ProviderRouter
from src.models.schemas import Chunk, ChunkType, RetrievedChunk
from src.sql import pipeline


def test_reexport_object_identity():
    """SQLRetriever and associated pipeline symbols in s12b must be identical to pipeline.py."""
    assert s12b.SQLRetriever is pipeline.SQLRetriever
    assert s12b._SQLRetriever is pipeline.SQLRetriever
    assert s12b.clear_knowledge_caches is pipeline.clear_knowledge_caches
    assert s12b._clear_knowledge_caches is pipeline.clear_knowledge_caches
    assert s12b._MAX_RESULT_CACHE_ENTRIES == pipeline._MAX_RESULT_CACHE_ENTRIES
    assert s12b.MAX_RESULT_CACHE_ENTRIES == pipeline.MAX_RESULT_CACHE_ENTRIES


def test_retriever_initialization_and_properties():
    """Test SQLRetriever initialization with default and custom db_id."""
    mock_router = MagicMock(spec=ProviderRouter)
    retriever = pipeline.SQLRetriever(router=mock_router, db_id="erp_main")
    assert retriever.db_id == "erp_main"
    assert retriever._dialect.key in ("sqlite", "mysql", "postgresql")
    assert retriever.last_infra_error is None
    assert retriever.last_query_status is None


def test_result_cache_isolation_and_clearing():
    """Test SQLRetriever._result_cache per-db_id isolation and invalidation."""
    pipeline.SQLRetriever.clear_result_cache()
    assert len(pipeline.SQLRetriever._result_cache) == 0

    dummy_chunk = RetrievedChunk(
        chunk=Chunk(
            chunk_id="chunk_1",
            content="Result content",
            chunk_type=ChunkType.SQL_RESULT,
            document_id="doc1",
        ),
        similarity=1.0,
    )

    # Seed result cache with entries for two distinct db_ids
    pipeline.SQLRetriever._result_cache[("erp_main", "select * from party")] = (100.0, [dummy_chunk])
    pipeline.SQLRetriever._result_cache[("db_other", "select * from party")] = (100.0, [dummy_chunk])
    assert len(pipeline.SQLRetriever._result_cache) == 2

    # Clear only erp_main
    pipeline.SQLRetriever.clear_result_cache("erp_main")
    assert ("erp_main", "select * from party") not in pipeline.SQLRetriever._result_cache
    assert ("db_other", "select * from party") in pipeline.SQLRetriever._result_cache

    # Global clear
    pipeline.SQLRetriever.clear_result_cache()
    assert len(pipeline.SQLRetriever._result_cache) == 0


def test_schema_cache_isolation_and_clearing():
    """Test SQLRetriever._full_schema_cache per-db_id isolation and invalidation."""
    pipeline.SQLRetriever.clear_schema_cache()
    pipeline.SQLRetriever._full_schema_cache["erp_main"] = "CREATE TABLE erp (id INT);"
    pipeline.SQLRetriever._full_schema_cache["db_other"] = "CREATE TABLE other (id INT);"
    assert len(pipeline.SQLRetriever._full_schema_cache) == 2

    # Targeted clear
    pipeline.SQLRetriever.clear_schema_cache("erp_main")
    assert "erp_main" not in pipeline.SQLRetriever._full_schema_cache
    assert "db_other" in pipeline.SQLRetriever._full_schema_cache

    # Global clear
    pipeline.SQLRetriever.clear_schema_cache()
    assert len(pipeline.SQLRetriever._full_schema_cache) == 0


@pytest.mark.asyncio
async def test_access_control_disallowed_user():
    """Users without access to a database get empty results immediately."""
    mock_router = MagicMock(spec=ProviderRouter)
    retriever = pipeline.SQLRetriever(router=mock_router, db_id="erp_main")

    with patch("src.sql.registry.can_access_database", return_value=False):
        results = await retriever.retrieve("SELECT * FROM party", user_id="unauthorized_user")
        assert results == []


def test_feature_flag_check_honors_patches():
    """_check_feature_enabled checks sys.modules['src.stages.s12b_sql_retrieval'] patches."""
    with patch("src.stages.s12b_sql_retrieval.is_feature_enabled", return_value=True):
        assert pipeline._check_feature_enabled("delta_repair_enabled") is True

    with patch("src.stages.s12b_sql_retrieval.is_feature_enabled", return_value=False):
        assert pipeline._check_feature_enabled("delta_repair_enabled") is False
