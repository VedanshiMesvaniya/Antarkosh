"""Tests for SQLRetriever and SemanticCache multi-database cache isolation."""

import time
from unittest.mock import MagicMock

import pytest

from src.core.db_settings import _apply_runtime
from src.core.provider_client import ProviderRouter
from src.core.sql_column_registry import ColumnRegistry
from src.models.schemas import Chunk, ChunkType, QueryResult, RetrievedChunk
from src.stages.s12b_sql_retrieval import SQLRetriever
from src.utils.query_classifier import QueryType
from src.utils.semantic_cache import SemanticCache, get_semantic_cache


@pytest.fixture(autouse=True)
def _clean_caches():
    """Ensure caches are reset before and after each test."""
    SQLRetriever.clear_result_cache()
    SQLRetriever.clear_schema_cache()
    SemanticCache.reset()
    yield
    SQLRetriever.clear_result_cache()
    SQLRetriever.clear_schema_cache()
    SemanticCache.reset()


def test_retrievers_with_different_db_ids_do_not_share_result_cache():
    """Two retrievers with different db_ids and identical question text do not share entries."""
    router = MagicMock(spec=ProviderRouter)
    retriever_erp = SQLRetriever(router, db_id="erp_main")
    retriever_other = SQLRetriever(router, db_id="other_db")

    question = "how many active orders?"
    chunk_erp = RetrievedChunk(
        chunk=Chunk(
            chunk_id="c_erp",
            document_id="doc_erp",
            chunk_index=0,
            content="ERP Order Result",
            chunk_type=ChunkType.TABLE,
        ),
        score=1.0,
    )

    # Populate cache for erp_main
    now = time.monotonic()
    SQLRetriever._result_cache[(retriever_erp.db_id, question.lower())] = (
        now,
        [chunk_erp],
    )

    # other_db retriever querying the identical question does NOT hit erp_main cache
    assert (retriever_other.db_id, question.lower()) not in SQLRetriever._result_cache
    cached_other = SQLRetriever._result_cache.get((retriever_other.db_id, question.lower()))
    assert cached_other is None

    # erp_main retriever querying the identical question DOES hit
    cached_erp = SQLRetriever._result_cache.get((retriever_erp.db_id, question.lower()))
    assert cached_erp is not None
    assert cached_erp[1][0].chunk.content == "ERP Order Result"


def test_schema_cache_and_column_registry_isolated_by_db_id():
    """Verify _full_schema_cache and _column_registry are independently keyed by db_id."""
    router = MagicMock(spec=ProviderRouter)
    retriever_erp = SQLRetriever(router, db_id="erp_main")
    retriever_other = SQLRetriever(router, db_id="other_db")

    SQLRetriever._full_schema_cache["erp_main"] = "TABLE orders (\n  id int\n)"
    SQLRetriever._full_schema_cache["other_db"] = "CREATE TABLE invoices (\n  id int\n);"

    assert SQLRetriever._full_schema_cache[retriever_erp.db_id] != SQLRetriever._full_schema_cache[retriever_other.db_id]

    SQLRetriever._column_registry["erp_main"] = ColumnRegistry(
        "TABLE orders (\n  id int,\n  amount decimal\n)", "mysql"
    )
    SQLRetriever._column_registry["other_db"] = ColumnRegistry(
        "CREATE TABLE invoices (\n  id int,\n  total decimal\n);", "sqlite"
    )

    assert retriever_erp.column_registry is not None
    assert retriever_other.column_registry is not None
    assert retriever_erp.column_registry is not retriever_other.column_registry

    assert "orders" in retriever_erp.column_registry._tables
    assert "invoices" not in retriever_erp.column_registry._tables
    assert "invoices" in retriever_other.column_registry._tables
    assert "orders" not in retriever_other.column_registry._tables


def test_selective_and_global_cache_clearing():
    """Verify selective clearing by db_id leaves other db_ids intact, while wipe clears all."""
    SQLRetriever._result_cache[("erp_main", "q1")] = (time.monotonic(), [])
    SQLRetriever._result_cache[("other_db", "q1")] = (time.monotonic(), [])
    SQLRetriever._full_schema_cache["erp_main"] = "schema1"
    SQLRetriever._full_schema_cache["other_db"] = "schema2"

    # Selective result cache clear
    SQLRetriever.clear_result_cache("erp_main")
    assert ("erp_main", "q1") not in SQLRetriever._result_cache
    assert ("other_db", "q1") in SQLRetriever._result_cache

    # Selective schema cache clear
    SQLRetriever.clear_schema_cache("erp_main")
    assert "erp_main" not in SQLRetriever._full_schema_cache
    assert "other_db" in SQLRetriever._full_schema_cache

    # Re-populate
    SQLRetriever._result_cache[("erp_main", "q1")] = (time.monotonic(), [])
    SQLRetriever._full_schema_cache["erp_main"] = "schema1"

    # Global wipe (e.g. from db_settings._apply_runtime)
    _apply_runtime({"engine": "sqlite"})
    assert len(SQLRetriever._result_cache) == 0
    assert len(SQLRetriever._full_schema_cache) == 0
    assert len(SQLRetriever._column_registry) == 0


def test_semantic_cache_isolation_by_db_id():
    """Verify SemanticCache scopes entries by db_id."""
    cache = get_semantic_cache()
    emb = [0.1] * 384
    result = QueryResult(
        query="what is revenue?",
        answer="Total revenue is $1000",
        model_used="mock",
    )

    # Store with db_id="erp_main"
    cache.store(
        "what is revenue?",
        emb,
        result,
        scope_key="tenant_corp",
        query_type=QueryType.SUM,
        db_id="erp_main",
    )

    # Lookup under other_db with same base scope must MISS
    miss = cache.lookup(
        "what is revenue?",
        emb,
        scope_key="tenant_corp",
        db_id="other_db",
    )
    assert miss is None

    # Lookup under erp_main must HIT
    hit = cache.lookup(
        "what is revenue?",
        emb,
        scope_key="tenant_corp",
        db_id="erp_main",
    )
    assert hit is not None
    assert hit.answer == "Total revenue is $1000"
