"""Multi-database Schema RAG isolation and transition rule integration tests.

Tests against local in-memory Qdrant with deterministic embeddings in the style
of tests/test_local_qdrant_integration.py.
"""

from __future__ import annotations

import sys
import types
import zlib
from unittest.mock import AsyncMock

import numpy as np
import pytest

from src.core import local_models
from src.core.config import settings
from src.models.schemas import Chunk, ChunkType
from src.stages.s10_embeddings import EmbeddingService
from src.stages.s11_vector_store import QdrantStore
from src.stages.s12b_sql_retrieval import SQLRetriever


class _BagOfWordsM3:
    """Deterministic embedding stand-in with FlagEmbedding call/return shapes."""

    def __init__(self, model_name_or_path: str, use_fp16: bool = False) -> None:
        pass

    def encode(
        self,
        sentences,
        batch_size=None,
        max_length=None,
        return_dense=None,
        return_sparse=None,
        return_colbert_vecs=None,
        **kw,
    ):
        dense, lex = [], []
        for text in sentences:
            vec = np.zeros(1024, dtype=np.float32)
            weights: dict[str, np.float32] = {}
            for word in text.lower().split():
                tok = zlib.crc32(word.encode()) % 250_000
                vec[tok % 1024] += 1.0
                weights[str(tok)] = np.float32(weights.get(str(tok), 0.0) + 0.5)
            norm = np.linalg.norm(vec) or 1.0
            dense.append(vec / norm)
            lex.append(weights)
        return {"dense_vecs": np.stack(dense), "lexical_weights": lex, "colbert_vecs": None}


@pytest.fixture
def local_qdrant_env(monkeypatch: pytest.MonkeyPatch):
    """Set up an in-memory Qdrant instance and deterministic embeddings."""
    from src.stages import s11_vector_store as s11

    SQLRetriever.clear_schema_cache()
    SQLRetriever.clear_result_cache()

    fake = types.ModuleType("FlagEmbedding")
    fake.BGEM3FlagModel = _BagOfWordsM3
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake)
    local_models.reset_cache()

    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "qdrant_api_key", "")
    s11._global_client = None
    s11._global_client_loop = None

    yield

    s11._global_client = None
    s11._global_client_loop = None
    SQLRetriever.clear_schema_cache()
    SQLRetriever.clear_result_cache()
    local_models.reset_cache()


def _dummy_router():
    router = AsyncMock()
    router.route = AsyncMock(return_value="SELECT 1")
    return router


@pytest.mark.asyncio
async def test_two_dbs_same_table_name_schema_rag_isolation(local_qdrant_env):
    """Two databases with identical table names never cross-pollinate during Schema RAG."""
    svc = EmbeddingService()
    store = QdrantStore(embedding_service=svc)

    erp_chunk = Chunk(
        chunk_id="erp_main_schema_customers",
        document_id="schema:erp_main",
        chunk_type=ChunkType.SQL_SCHEMA,
        content="TABLE customers (\n  id INT,\n  erp_account_code VARCHAR(50)\n)",
        db_id="erp_main",
        metadata={"db_id": "erp_main"},
    )
    crm_chunk = Chunk(
        chunk_id="crm_db_schema_customers",
        document_id="schema:crm_db",
        chunk_type=ChunkType.SQL_SCHEMA,
        content="TABLE customers (\n  customer_uuid VARCHAR(36),\n  crm_lead_score DOUBLE\n)",
        db_id="crm_db",
        metadata={"db_id": "crm_db"},
    )

    chunks = [erp_chunk, crm_chunk]
    dense, sparse = await svc.embed_chunks(chunks)
    await store.upsert(chunks, dense, sparse)

    retriever_erp = SQLRetriever(
        router=_dummy_router(),
        vector_store=store,
        embedding_service=svc,
        db_id="erp_main",
    )
    schema_erp = await retriever_erp._get_schema("find customers")
    assert "erp_account_code" in schema_erp
    assert "crm_lead_score" not in schema_erp

    retriever_crm = SQLRetriever(
        router=_dummy_router(),
        vector_store=store,
        embedding_service=svc,
        db_id="crm_db",
    )
    schema_crm = await retriever_crm._get_schema("find customers")
    assert "crm_lead_score" in schema_crm
    assert "erp_account_code" not in schema_crm


@pytest.mark.asyncio
async def test_legacy_schema_chunk_transition_rule(local_qdrant_env):
    """Legacy chunks (document_id 'live_db_schema', no db_id) accepted ONLY for erp_main."""
    svc = EmbeddingService()
    store = QdrantStore(embedding_service=svc)

    legacy_chunk = Chunk(
        chunk_id="legacy_schema_inventory",
        document_id="live_db_schema",
        chunk_type=ChunkType.SQL_SCHEMA,
        content="TABLE legacy_inventory (\n  item_id INT,\n  stock_count INT\n)",
        metadata={},  # No db_id
    )

    chunks = [legacy_chunk]
    dense, sparse = await svc.embed_chunks(chunks)
    await store.upsert(chunks, dense, sparse)

    # erp_main accepts legacy chunk
    retriever_erp = SQLRetriever(
        router=_dummy_router(),
        vector_store=store,
        embedding_service=svc,
        db_id="erp_main",
    )
    schema_erp = await retriever_erp._get_schema("check inventory")
    assert "legacy_inventory" in schema_erp

    # crm_db REJECTS legacy chunk
    retriever_crm = SQLRetriever(
        router=_dummy_router(),
        vector_store=store,
        embedding_service=svc,
        db_id="crm_db",
    )
    schema_crm = await retriever_crm._get_schema("check inventory")
    assert "legacy_inventory" not in schema_crm


@pytest.mark.asyncio
async def test_sync_live_schema_tags_document_id_and_db_id(monkeypatch: pytest.MonkeyPatch, local_qdrant_env):
    """sync_live_schema tags chunks with payload db_id and document_id schema:<db_id>."""
    from src.pipeline.schema_ingestion import sync_live_schema

    mock_run = AsyncMock(return_value=[
        {"name": "orders", "sql": "CREATE TABLE orders (id INT, total FLOAT)"}
    ])
    monkeypatch.setattr("src.pipeline.schema_ingestion.run_readonly_query", mock_run)

    captured_chunks: list[Chunk] = []
    mock_store = AsyncMock()
    mock_store.upsert = AsyncMock(side_effect=lambda chunks, *args: captured_chunks.extend(chunks))

    mock_embeddings = AsyncMock()
    mock_embeddings.embed_chunks = AsyncMock(return_value=([[0.1] * 1024], [None]))

    result = await sync_live_schema(
        embedding_service=mock_embeddings,
        vector_store=mock_store,
        db_id="analytics_db",
    )

    assert result["status"] == "ok"
    assert result["db_id"] == "analytics_db"
    assert result["document_id"] == "schema:analytics_db"

    assert len(captured_chunks) == 1
    chunk = captured_chunks[0]
    assert chunk.document_id == "schema:analytics_db"
    assert chunk.db_id == "analytics_db"
    assert chunk.chunk_id == "analytics_db_schema_orders"
    assert chunk.metadata.get("db_id") == "analytics_db"
