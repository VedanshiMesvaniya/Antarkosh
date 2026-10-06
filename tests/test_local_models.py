"""Tests for the fully local stack (BGE-M3, BGE reranker, local vision/OCR, local Qdrant).

The real model weights are never downloaded here: FlagEmbedding is replaced by a
tiny fake module whose method signatures and return shapes mirror the real
FlagEmbedding 1.4.x API (verified from its source), so these tests prove the
wiring without needing a GPU, a download, or a network.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from src.core import local_models
from src.core.config import settings
from src.core.embeddings import EmbeddingAdapter, SparseVector
from src.models.schemas import FigureData, PageContent, ParsedDocument, FileCategory
from src.stages import s07_s08_visuals as visuals
from src.stages.s09_chunking import _figure_to_text
from src.stages.s10_embeddings import BGEM3EmbeddingAdapter, EmbeddingService


# ---------------------------------------------------------------------------
# Fake FlagEmbedding (same API shape as the real library)
# ---------------------------------------------------------------------------

class _FakeM3:
    instances = 0

    def __init__(self, model_name_or_path: str, use_fp16: bool = False) -> None:
        type(self).instances += 1
        self.name = model_name_or_path

    def encode(self, sentences, batch_size=None, max_length=None, return_dense=None,
               return_sparse=None, return_colbert_vecs=None, **kw):
        assert return_dense and return_sparse and not return_colbert_vecs
        n = len(sentences)
        dense = np.tile(np.linspace(0, 1, 1024, dtype=np.float32), (n, 1))
        # real lib: str(token_id) -> numpy float, may contain zero/negative-free weights
        lex = [{str(100 + i): np.float32(0.5), "7": np.float32(0.25)} for i in range(n)]
        return {"dense_vecs": dense, "lexical_weights": lex, "colbert_vecs": None}


class _FakeReranker:
    def __init__(self, model_name_or_path: str, use_fp16: bool = False) -> None:
        self.name = model_name_or_path

    def compute_score(self, sentence_pairs, batch_size=256, max_length=512, normalize=False, **kw):
        assert normalize is True
        scores = [1.0 / (1 + len(doc)) + (0.9 if "needle" in doc else 0.0) for _, doc in sentence_pairs]
        return scores if len(scores) != 1 else scores[0]  # real lib returns a bare float for 1 pair


@pytest.fixture
def fake_flag(monkeypatch):
    mod = types.ModuleType("FlagEmbedding")
    mod.BGEM3FlagModel = _FakeM3
    mod.FlagReranker = _FakeReranker
    monkeypatch.setitem(sys.modules, "FlagEmbedding", mod)
    local_models.reset_cache()
    _FakeM3.instances = 0
    yield mod
    local_models.reset_cache()


# ---------------------------------------------------------------------------
# BGE-M3 embeddings
# ---------------------------------------------------------------------------

def test_bge_m3_adapter_satisfies_protocol():
    adapter = BGEM3EmbeddingAdapter()
    assert isinstance(adapter, EmbeddingAdapter)
    assert adapter.vector_dim == 1024 and adapter.supports_sparse is True


async def test_bge_m3_returns_dense_and_real_sparse(fake_flag):
    dense, sparse = await BGEM3EmbeddingAdapter().embed(["alpha", "beta", "gamma"])
    assert len(dense) == len(sparse) == 3
    assert all(len(v) == 1024 and all(isinstance(x, float) for x in v) for v in dense)
    sv = sparse[1]
    assert isinstance(sv, SparseVector) and not sv.is_empty()
    assert sv.indices == sorted(sv.indices) and len(sv.indices) == len(sv.values)
    assert 101 in sv.indices and 7 in sv.indices
    assert all(isinstance(v, float) and v > 0 for v in sv.values)


async def test_bge_m3_model_is_loaded_once_and_shared(fake_flag):
    await BGEM3EmbeddingAdapter().embed(["a"])
    await BGEM3EmbeddingAdapter().embed(["b"])
    await asyncio.gather(*(BGEM3EmbeddingAdapter().embed([str(i)]) for i in range(5)))
    assert _FakeM3.instances == 1


async def test_bge_m3_empty_input(fake_flag):
    assert await BGEM3EmbeddingAdapter().embed([]) == ([], [])


async def test_bge_m3_missing_dependency_gives_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "FlagEmbedding", None)  # makes `import FlagEmbedding` fail
    local_models.reset_cache()
    with pytest.raises(local_models.LocalModelUnavailable, match="uv sync"):
        await BGEM3EmbeddingAdapter().embed(["x"])


def test_lexical_weights_edge_cases():
    assert local_models.lexical_weights_to_sparse(None).is_empty()
    assert local_models.lexical_weights_to_sparse({}).is_empty()
    sv = local_models.lexical_weights_to_sparse({"5": 0.0, "9": -1.0, "3": 0.4})
    assert sv.indices == [3] and sv.values == [0.4]
    hashed = local_models.lexical_weights_to_sparse({"hello": 0.3})  # non-numeric key is hashed
    assert len(hashed.indices) == 1 and hashed.indices[0] >= 0


async def test_query_embedding_goes_through_cache_with_bge(fake_flag):
    from src.stages.s10_embeddings import get_query_embedding_cache
    get_query_embedding_cache().clear()
    svc = EmbeddingService()
    d1, s1 = await svc.embed_query("what is x")
    d2, s2 = await svc.embed_query("what is x")
    assert d1 == d2 and get_query_embedding_cache().stats()["hits"] == 1


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------

def _retrieved(texts: list[str]):
    from src.models.schemas import Chunk, RetrievedChunk
    out = []
    for i, t in enumerate(texts):
        out.append(RetrievedChunk(
            chunk=Chunk(chunk_id=f"c{i}", document_id="d", content=t),
            score=0.1, retrieval_method="hybrid",
        ))
    return out


async def test_local_reranker_orders_and_truncates(fake_flag):
    from src.stages.s12_s13_s14_retrieval import Reranker
    chunks = _retrieved(["junk " * 30, "the needle is here", "other filler text", "more filler"])
    out = await Reranker().rerank("where is the needle", chunks, top_k=2)
    assert len(out) == 2 and out[0].chunk.content == "the needle is here"
    assert all(c.retrieval_method == "reranked" and 0.0 <= c.score <= 1.0 + 1e-9 for c in out)
    assert out[0].score >= out[1].score


async def test_local_reranker_single_chunk_bare_float(fake_flag):
    from src.stages.s12_s13_s14_retrieval import Reranker
    out = await Reranker().rerank("q", _retrieved(["needle"]), top_k=5)
    assert len(out) == 1


async def test_local_reranker_failure_falls_back_to_lexical(monkeypatch):
    from src.stages.s12_s13_s14_retrieval import Reranker
    monkeypatch.setitem(sys.modules, "FlagEmbedding", None)
    local_models.reset_cache()
    chunks = _retrieved(["totally unrelated words", "iphone price list"])
    out = await Reranker().rerank("iphone price", chunks, top_k=1)
    assert out[0].chunk.content == "iphone price list"  # lexical fallback still answers


# ---------------------------------------------------------------------------
# Ingestion is local-only: routing
# ---------------------------------------------------------------------------

def test_ingestion_router_reaches_only_the_local_model(monkeypatch):
    """Even with every cloud key configured, ingestion can only call the local server."""
    from src.core.provider_client import (
        INGESTION_TASKS, LOCAL_PROVIDER, build_ingestion_router,
    )
    for key in ("gemini_api_key", "nvidia_nim_api_key", "groq_api_key", "openrouter_api_key"):
        monkeypatch.setattr(settings, key, "cloud-key")
    router = build_ingestion_router()
    assert list(router._providers) == [LOCAL_PROVIDER]
    for task in INGESTION_TASKS:
        opts = router._get_route(task).options
        assert [(o.provider_name, o.model) for o in opts] == [(LOCAL_PROVIDER, settings.local_vision_model)]


def test_ingestion_tasks_cover_every_stage_that_calls_a_model():
    from src.core.provider_client import INGESTION_TASKS
    assert INGESTION_TASKS == {
        "ocr_vision", "layout_analysis", "table_extraction",
        "chart_analysis", "image_understanding", "semantic_classification",
    }


def test_pipeline_default_router_is_the_local_one(tmp_path, monkeypatch):
    from src.core.ingestion_registry import IngestionRegistry
    from src.pipeline.ingestion import IngestionPipeline
    monkeypatch.setattr(settings, "gemini_api_key", "cloud-key")
    pipe = IngestionPipeline(
        embedding_service=EmbeddingService(primary=BGEM3EmbeddingAdapter()),
        vector_store=object(),  # type: ignore[arg-type]
        registry=IngestionRegistry(registry_path=tmp_path / "r.json"),
    )
    assert list(pipe._router._providers) == ["local"]


def test_query_router_still_has_cloud_providers_for_answers(monkeypatch):
    """Only ingestion is local-only; answer generation keeps using the normal router."""
    from src.core.provider_client import ProviderRouter, _build_providers
    from src.core.rate_limiter import RateLimiter
    monkeypatch.setattr(settings, "gemini_api_key", "cloud-key")
    providers = _build_providers(RateLimiter())
    assert "gemini" in providers and "local" not in providers
    assert ProviderRouter(preferred_provider="auto")._get_route("general_qa").options


def test_local_vision_defaults():
    assert settings.local_vision_base_url == "http://localhost:11434/v1"
    assert settings.local_vision_model == "qwen3-vl:4b"
    assert settings.local_vision_concurrency == 1
    assert settings.local_vision_timeout_seconds >= 300


def test_removed_cloud_settings_are_gone_and_old_env_files_still_load(tmp_path):
    """A .env that still contains JINA_API_KEY / OCR_SPACE_API_KEY must not stop the app starting."""
    from src.core.config import Settings
    assert not hasattr(settings, "jina_api_key") and not hasattr(settings, "ocr_space_api_key")
    env = tmp_path / ".env"
    env.write_text("JINA_API_KEY=abc\nOCR_SPACE_API_KEY=def\nEMBEDDING_PROVIDER=jina\nGROQ_API_KEY=g\n")
    loaded = Settings(_env_file=str(env))
    assert loaded.groq_api_key == "g"


async def test_ocr_stage_uses_only_the_local_vision_model():
    from src.stages import s04_ocr

    class _Router:
        def __init__(self):
            self.tasks = []

        async def vision(self, task, image_data, prompt, **kw):
            self.tasks.append(task)
            return "Quarterly report\nRevenue grew by 11 percent in the last quarter."

    router = _Router()
    text, confidence, method = await s04_ocr._ocr_chain(b"png-bytes", router)
    assert router.tasks == ["ocr_vision"] and method == "vision_llm"
    assert "Revenue grew" in text and confidence > 0
    assert not hasattr(s04_ocr, "_ocr_space")


# ---------------------------------------------------------------------------
# Visuals stage: OCR text vs meaning, saved image, chunk text
# ---------------------------------------------------------------------------

def test_split_image_analysis_variants():
    f = visuals._split_image_analysis
    assert f("VISIBLE TEXT:\nStart\nEnd\n\nMEANING:\nA flowchart.") == ("Start\nEnd", "A flowchart.")
    assert f("**VISIBLE TEXT:** none\n**MEANING:** A cat photo.") == ("", "A cat photo.")
    assert f("VISIBLE TEXT: N/A\nMEANING: Logo.") == ("", "Logo.")
    assert f("No labels at all, just prose.") == ("", "No labels at all, just prose.")
    assert f("") == ("", "")
    assert f("MEANING: only meaning") == ("", "only meaning")


def test_figure_to_text_includes_ocr_only_when_present():
    base = FigureData(page_number=1, description="A flowchart of login.")
    assert _figure_to_text(base) == "A flowchart of login."
    with_ocr = FigureData(page_number=1, description="A flowchart.", ocr_text="Start\nEnd")
    assert _figure_to_text(with_ocr) == "A flowchart.\nText in image: Start\nEnd"


def test_save_figure_image_writes_file_and_is_path_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "processed_dir", tmp_path / "processed")
    rel = visuals._save_figure_image("/x/../we ird/Report (final).pdf", 3, 1, b"PNGDATA", "PNG; rm -rf")
    saved = Path(rel) if Path(rel).is_absolute() else Path(visuals.PROJECT_ROOT) / rel
    assert saved.read_bytes() == b"PNGDATA"
    assert (tmp_path / "processed" / "figures") in saved.parents
    assert saved.name == "p0003_f01.pngrmrf"[: len("p0003_f01.") + 5] or saved.suffix.startswith(".")
    assert ".." not in saved.relative_to(tmp_path / "processed").parts


def test_save_figure_image_disabled_or_failing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "processed_dir", tmp_path / "processed")
    monkeypatch.setattr(settings, "save_figure_images", False)
    assert visuals._save_figure_image("a.pdf", 1, 0, b"x", "png") == ""
    monkeypatch.setattr(settings, "save_figure_images", True)
    (tmp_path / "processed").write_text("i am a file, not a dir")  # mkdir will fail
    assert visuals._save_figure_image("a.pdf", 1, 0, b"x", "png") == ""


@pytest.mark.parametrize(
    "width,height,expected_task",
    [(300, 300, "chart_analysis"), (150, 400, "image_understanding")],
)
async def test_visual_stage_end_to_end_with_real_pdf(tmp_path, monkeypatch, width, height, expected_task):
    """A real PDF with an embedded image goes through analyze_visuals with a fake router.

    300x300 is classified as a chart/diagram by the repo's heuristic, 150x400 as a
    general image — both must yield separate OCR text and meaning plus a saved file.
    """
    import pymupdf

    monkeypatch.setattr(settings, "processed_dir", tmp_path / "processed")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pix.clear_with(200)
    pdf_path = tmp_path / "doc.pdf"
    d = pymupdf.open()
    page = d.new_page()
    page.insert_image(pymupdf.Rect(50, 50, 50 + width, 50 + height), pixmap=pix)
    d.save(str(pdf_path))
    d.close()

    class _Router:
        def __init__(self):
            self.tasks = []

        async def vision(self, task, image_data, prompt, **kw):
            self.tasks.append(task)
            return "VISIBLE TEXT:\nLogin\nDashboard\n\nMEANING:\nA two-step login flow diagram."

    document = ParsedDocument(
        file_path=str(pdf_path), file_category=FileCategory.PDF, total_pages=1,
        pages=[PageContent(page_number=1)],
    )
    router = _Router()
    out = await visuals.analyze_visuals(document, router)
    figs = out.pages[0].figures
    assert len(figs) == 1
    fig = figs[0]
    assert router.tasks == [expected_task]
    assert fig.ocr_text == "Login\nDashboard"
    assert fig.description == "A two-step login flow diagram."
    assert fig.image_path and fig.confidence == 0.85
    saved = Path(fig.image_path) if Path(fig.image_path).is_absolute() else Path(visuals.PROJECT_ROOT) / fig.image_path
    assert saved.exists() and saved.stat().st_size > 0
    assert "Text in image: Login" in _figure_to_text(fig)


# ---------------------------------------------------------------------------
# Qdrant gating (local server without an API key)
# ---------------------------------------------------------------------------

def test_qdrant_configured_needs_only_url(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "qdrant_api_key", "")
    assert not settings.qdrant_configured
    monkeypatch.setattr(settings, "qdrant_url", "http://localhost:6333")
    assert settings.qdrant_configured and settings.qdrant_api_key_or_none is None
    monkeypatch.setattr(settings, "qdrant_api_key", "  secret ")
    assert settings.qdrant_api_key_or_none == "secret"


def test_metadata_backend_selection_follows_url(monkeypatch, tmp_path):
    from src.core import metadata_store
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "qdrant_api_key", "")
    assert isinstance(metadata_store.create_metadata_backend(tmp_path / "r.json"),
                      metadata_store.JsonMetadataBackend)
    monkeypatch.setattr(settings, "qdrant_url", "http://localhost:6333")
    assert isinstance(metadata_store.create_metadata_backend(tmp_path / "r.json"),
                      metadata_store.QdrantMetadataBackend)
