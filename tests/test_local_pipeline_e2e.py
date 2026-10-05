"""The REAL ingestion pipeline, fully local, end to end.

A real PDF (one text page with an embedded image) goes through every stage —
detection, classification, parsing, vision, chunking, embedding, vector store,
registry — and is then retrieved. Only the model servers are stand-ins:

  * a tiny HTTP server answers as Ollama would (OpenAI-compatible API),
  * FlagEmbedding is a fake with the real library's call/return shapes,
  * Qdrant is the in-memory engine of qdrant-client (same code as the server).

Nothing here can reach a cloud provider: the ingestion router only knows the
local server, and no cloud keys are set.
"""

from __future__ import annotations

import sys
import types
import zlib

import numpy as np
import pytest

pytest.importorskip("pdfplumber")

from src.core import local_models  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.ingestion_registry import IngestionRegistry  # noqa: E402
from src.models.schemas import ChunkType  # noqa: E402
from tests.test_local_vision_provider import _FakeOllama  # noqa: E402


class _BagOfWordsM3:
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
            dense.append(vec / (np.linalg.norm(vec) or 1.0))
            lex.append(weights)
        return {"dense_vecs": np.stack(dense), "lexical_weights": lex, "colbert_vecs": None}


@pytest.fixture
def local_world(monkeypatch, tmp_path):
    from src.stages import s11_vector_store as s11

    fake = types.ModuleType("FlagEmbedding")
    fake.BGEM3FlagModel = _BagOfWordsM3
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake)
    local_models.reset_cache()

    for key in ("gemini_api_key", "nvidia_nim_api_key", "groq_api_key", "openrouter_api_key"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "qdrant_url", "")  # in-memory Qdrant
    monkeypatch.setattr(settings, "processed_dir", tmp_path / "processed")
    s11._global_client = None
    s11._global_client_loop = None

    with _FakeOllama() as ollama:
        monkeypatch.setattr(settings, "local_vision_base_url", ollama.base_url)
        monkeypatch.setattr(settings, "local_vision_timeout_seconds", 10.0)
        yield ollama
    s11._global_client = None
    s11._global_client_loop = None
    local_models.reset_cache()


def _make_pdf(tmp_path):
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 300))
    pix.clear_with(200)
    pdf = tmp_path / "manual.pdf"
    d = pymupdf.open()
    page = d.new_page()
    # Enough native text that the page is classified as a text page, not a scan.
    paragraph = (
        "Warehouse manual. Pallets of finished goods are stored in aisle seven "
        "and counted every Friday by the night shift supervisor. Damaged pallets "
        "are moved to the quarantine bay and photographed before any claim is "
        "filed with the carrier. Forklift drivers must complete the safety "
        "induction before operating equipment in the warehouse. "
    ) * 3
    page.insert_textbox(pymupdf.Rect(72, 72, 540, 330), paragraph, fontsize=10)
    page.insert_image(pymupdf.Rect(72, 400, 372, 700), pixmap=pix)
    d.save(str(pdf))
    d.close()
    return pdf


async def test_pdf_ingests_locally_and_is_retrievable(local_world, tmp_path):
    from src.pipeline.ingestion import IngestionPipeline, _INGEST_LOCKS
    from src.stages.s10_embeddings import EmbeddingService
    from src.stages.s11_vector_store import QdrantStore
    from src.stages.s12_s13_s14_retrieval import Retriever

    pdf = _make_pdf(tmp_path)
    registry = IngestionRegistry(registry_path=tmp_path / "registry.json")
    embeddings = EmbeddingService()
    store = QdrantStore(embedding_service=embeddings)
    pipe = IngestionPipeline(embedding_service=embeddings, vector_store=store, registry=registry)
    assert list(pipe._router._providers) == ["local"]

    try:
        result = await pipe.ingest(pdf)
    finally:
        _INGEST_LOCKS.clear()

    assert not result.skipped and result.total_chunks >= 2

    # the image was analysed by the local model (vision calls hit the fake Ollama)
    assert any(
        isinstance(m["content"], list)
        for r in local_world.requests for m in r["body"]["messages"]
    )

    # registry knows which model built the document
    (entry,) = registry.get_active()
    assert entry["embedding_model"] == "bge-m3"

    # chunks: the page text and a figure chunk with OCR text + meaning + saved image
    client = await store._get_client()
    points, _ = await client.scroll(store._collection_name, limit=50, with_payload=True)
    payloads = [p.payload for p in points]
    assert all(p["embedding_model"] == "bge-m3" for p in payloads)
    figure = [p for p in payloads if p["chunk_type"] == ChunkType.FIGURE_CAPTION.value]
    assert len(figure) == 1
    assert "Text in image: Hello" in figure[0]["content"] and "greeting card" in figure[0]["content"]
    assert figure[0]["image_path"].endswith(".png") or figure[0]["image_path"].endswith(".jpeg") \
        or "figures" in figure[0]["image_path"]

    # hybrid retrieval over the local vectors
    hits = await Retriever(store, embeddings).retrieve("who counts the pallets in aisle seven", top_k=3)
    assert hits and "aisle seven" in hits[0].chunk.content

    # ingesting the same file again is skipped (registry sees the current model)
    try:
        again = await pipe.ingest(pdf)
    finally:
        _INGEST_LOCKS.clear()
    assert again.skipped
