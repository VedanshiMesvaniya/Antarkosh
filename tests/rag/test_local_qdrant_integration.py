"""Integration test against a REAL local Qdrant server (no API key).

Skipped unless QDRANT_TEST_URL is set, e.g.

    docker compose -f docker-compose.local.yml up -d
    QDRANT_TEST_URL=http://localhost:6333 pytest tests/rag/test_local_qdrant_integration.py

It uses a throw-away collection and deletes it afterwards. Embeddings come from a
fake BGE-M3 (deterministic bag-of-words vectors) so no model download is needed,
but everything else — collection schema, dense+sparse upsert, RRF hybrid search,
payloads, the model-mismatch warning — runs against the real server.
"""

from __future__ import annotations

import logging
import os
import sys
import types
import uuid
import zlib

import numpy as np
import pytest

from src.core import local_models
from src.core.config import settings
from src.models.schemas import Chunk, ChunkType

QDRANT_TEST_URL = os.environ.get("QDRANT_TEST_URL", "")
pytestmark = pytest.mark.skipif(not QDRANT_TEST_URL, reason="set QDRANT_TEST_URL to run")


class _BagOfWordsM3:
    """Deterministic stand-in with the real FlagEmbedding return shapes."""

    def __init__(self, model_name_or_path: str, use_fp16: bool = False) -> None:
        pass

    def encode(self, sentences, batch_size=None, max_length=None, return_dense=None,
               return_sparse=None, return_colbert_vecs=None, **kw):
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
def local_stack(monkeypatch):
    from src.stages import s11_vector_store as s11

    fake = types.ModuleType("FlagEmbedding")
    fake.BGEM3FlagModel = _BagOfWordsM3
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake)
    local_models.reset_cache()
    monkeypatch.setattr(settings, "qdrant_url", QDRANT_TEST_URL)
    monkeypatch.setattr(settings, "qdrant_api_key", "")          # local server: no key
    s11._global_client = None
    s11._global_client_loop = None
    name = f"antarkosh_it_{uuid.uuid4().hex[:8]}"
    yield name
    import asyncio
    from qdrant_client import QdrantClient

    QdrantClient(url=QDRANT_TEST_URL).delete_collection(name)
    s11._global_client = None
    s11._global_client_loop = None
    local_models.reset_cache()
    del asyncio


def _chunks() -> list[Chunk]:
    return [
        Chunk(chunk_id="c1", document_id="d1", content="the warehouse stores finished goods and pallets", page_number=1),
        Chunk(chunk_id="c2", document_id="d1", content="quarterly revenue grew eleven percent in europe", page_number=2),
        Chunk(chunk_id="c3", document_id="d1", chunk_type=ChunkType.FIGURE_CAPTION, page_number=3,
              content="login flow diagram\nText in image: Start Login Dashboard",
              metadata={"image_path": "data/processed/figures/doc_x/p0003_f00.png"}),
    ]


async def test_local_qdrant_roundtrip_hybrid_search(local_stack):
    from src.stages.s10_embeddings import EmbeddingService
    from src.stages.s11_vector_store import QdrantStore
    from src.stages.s12_s13_s14_retrieval import Retriever

    svc = EmbeddingService()
    store = QdrantStore(collection_name=local_stack, embedding_service=svc)
    chunks = _chunks()
    dense, sparse = await svc.embed_chunks(chunks)
    assert all(not s.is_empty() for s in sparse)  # real sparse vectors, not empty placeholders
    await store.upsert(chunks, dense, sparse)

    # Data really lives on the server (not a throw-away in-memory store).
    from qdrant_client import QdrantClient
    sync = QdrantClient(url=QDRANT_TEST_URL)
    info = sync.get_collection(local_stack)
    assert info.points_count == 3
    assert "text_sparse" in (info.config.params.sparse_vectors or {})
    assert info.config.params.vectors.size == 1024

    points, _ = sync.scroll(local_stack, limit=10, with_payload=True, with_vectors=True)
    by_id = {p.payload["chunk_id"]: p for p in points}
    assert all(p.payload["embedding_model"] == "bge-m3" for p in points)
    assert by_id["c3"].payload["image_path"].endswith("p0003_f00.png")
    assert "image_path" not in by_id["c1"].payload
    assert len(by_id["c1"].vector["text_sparse"].indices) > 0

    # Hybrid (dense + sparse, RRF) retrieval finds the right chunk first.
    retriever = Retriever(store, svc)
    hits = await retriever.retrieve("which chunk talks about the warehouse pallets", top_k=3)
    assert hits and hits[0].chunk.chunk_id == "c1"
    diag = await retriever.retrieve("login dashboard diagram", top_k=3)
    assert diag[0].chunk.chunk_id == "c3"


async def test_model_mismatch_warning_on_reused_collection(local_stack, caplog):
    from src.stages import s11_vector_store as s11
    from src.stages.s10_embeddings import EmbeddingService
    from src.stages.s11_vector_store import QdrantStore

    svc = EmbeddingService()
    store = QdrantStore(collection_name=local_stack, embedding_service=svc)
    chunks = _chunks()[:1]
    dense, sparse = await svc.embed_chunks(chunks)
    await store.upsert(chunks, dense, sparse)

    # Same collection reopened by a process that thinks it is using Jina → must warn.
    s11._global_client = None
    s11._global_client_loop = None

    class _JinaLike:
        vector_dim = 1024
        model_id = "jina-embeddings-v3"

    other = QdrantStore(collection_name=local_stack, embedding_service=_JinaLike())  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        await other._get_client()
    assert any("re-ingest" in r.message and "bge-m3" in r.message for r in caplog.records)

    # Same model reopening → silent.
    s11._global_client = None
    s11._global_client_loop = None
    caplog.clear()
    same = QdrantStore(collection_name=local_stack, embedding_service=svc)
    with caplog.at_level(logging.WARNING):
        await same._get_client()
    assert not any("re-ingest" in r.message for r in caplog.records)


async def test_metadata_backend_works_against_local_server(local_stack, monkeypatch, tmp_path):
    """The ingestion registry's Qdrant backend must also accept a key-less local server."""
    from src.core import metadata_store

    backend = metadata_store.create_metadata_backend(tmp_path / "registry.json")
    assert isinstance(backend, metadata_store.QdrantMetadataBackend)
    client = backend._get_client()  # creates its collection on the real server
    assert client.get_collections() is not None


async def test_figure_flows_from_parsed_document_to_qdrant_payload(local_stack):
    """FigureData -> s09 chunking -> BGE-M3 -> real Qdrant: OCR text and image path survive."""
    from qdrant_client import QdrantClient

    from src.models.schemas import FigureData, FileCategory, PageContent, ParsedDocument
    from src.stages.s09_chunking import chunk_document
    from src.stages.s10_embeddings import EmbeddingService
    from src.stages.s11_vector_store import QdrantStore
    from src.stages.s12_s13_s14_retrieval import Retriever

    document = ParsedDocument(
        file_path="/tmp/manual.pdf", file_category=FileCategory.PDF, total_pages=1,
        pages=[PageContent(
            page_number=1,
            text="Chapter one explains how the invoice approval workflow operates in detail.",
            figures=[FigureData(
                page_number=1, description="Flowchart of the invoice approval steps.",
                ocr_text="Submit\nManager Review\nPaid", image_path="data/processed/figures/manual_ab/p0001_f00.png",
                confidence=0.85,
            )],
        )],
    )
    chunks = chunk_document(document)
    figure_chunks = [c for c in chunks if c.chunk_type == ChunkType.FIGURE_CAPTION]
    assert len(figure_chunks) == 1
    assert "Text in image: Submit" in figure_chunks[0].content
    assert figure_chunks[0].metadata["image_path"].endswith("p0001_f00.png")

    svc = EmbeddingService()
    store = QdrantStore(collection_name=local_stack, embedding_service=svc)
    dense, sparse = await svc.embed_chunks(chunks)
    await store.upsert(chunks, dense, sparse)

    points, _ = QdrantClient(url=QDRANT_TEST_URL).scroll(local_stack, limit=10, with_payload=True)
    figure_points = [p for p in points if p.payload["chunk_type"] == "figure_caption"]
    assert figure_points[0].payload["image_path"].endswith("p0001_f00.png")

    hits = await Retriever(store, svc).retrieve("manager review paid flowchart", top_k=3)
    assert hits[0].chunk.chunk_type == ChunkType.FIGURE_CAPTION
