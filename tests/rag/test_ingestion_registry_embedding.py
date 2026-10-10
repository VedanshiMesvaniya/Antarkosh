"""Registry records the embedding model; stale documents are re-indexed, not skipped.

Vectors from different embedding models are not comparable. An already-registered
document whose vectors came from another (or an unknown) model — for example an
entry imported from the legacy ``ingested_files.json`` or ingested earlier with
Jina — must be re-indexed instead of being skipped as "already ingested".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pdfplumber")

from src.core.ingestion_registry import IngestionRegistry, RegistryStatus  # noqa: E402
from src.core.embeddings import SparseVector  # noqa: E402
from src.pipeline.ingestion import IngestionPipeline, IngestionResult, _INGEST_LOCKS  # noqa: E402
from src.stages.s10_embeddings import EmbeddingService  # noqa: E402

MODEL = "bge-m3"


class _Adapter:
    model_id = MODEL
    vector_dim = 1024
    supports_sparse = True

    async def embed(self, texts, task="retrieval.passage"):
        return [[0.0] * 1024 for _ in texts], [SparseVector() for _ in texts]


def _file(tmp_path, name="doc.txt", data=b"some document bytes"):
    f = tmp_path / name
    f.write_bytes(data)
    return f


def _registry(tmp_path):
    return IngestionRegistry(registry_path=tmp_path / "registry.json")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_create_version_records_the_embedding_model(tmp_path):
    reg, f = _registry(tmp_path), _file(tmp_path)
    entry = reg.create_version(f, "hash1", 3, document_id="d1", embedding_model=MODEL)
    assert entry["embedding_model"] == MODEL
    assert reg.get_by_document_id("d1")["embedding_model"] == MODEL


def test_same_model_is_a_duplicate_to_skip(tmp_path):
    reg, f = _registry(tmp_path), _file(tmp_path)
    check = reg.check(f)
    reg.create_version(f, check.sha256, 3, document_id="d1", embedding_model=MODEL)
    again = reg.check(f, embedding_model=MODEL)
    assert again.status == RegistryStatus.ALREADY_INGESTED and again.old_document_id == "d1"


def test_other_model_is_stale(tmp_path):
    reg, f = _registry(tmp_path), _file(tmp_path)
    sha = reg.check(f).sha256
    reg.create_version(f, sha, 3, document_id="d1", embedding_model="jina-embeddings-v3")
    stale = reg.check(f, embedding_model=MODEL)
    assert stale.status == RegistryStatus.STALE_EMBEDDING
    assert stale.old_document_id == "d1" and stale.sha256 == sha


def test_entry_without_a_recorded_model_is_stale(tmp_path):
    """Legacy / pre-existing entries (no embedding_model field) must be re-indexed."""
    reg, f = _registry(tmp_path), _file(tmp_path)
    sha = reg.check(f).sha256
    reg.create_version(f, sha, 3, document_id="d1")  # no model recorded
    assert reg.check(f, embedding_model=MODEL).status == RegistryStatus.STALE_EMBEDDING


def test_check_without_a_model_behaves_exactly_as_before(tmp_path):
    reg, f = _registry(tmp_path), _file(tmp_path)
    reg.create_version(f, reg.check(f).sha256, 3, document_id="d1", embedding_model="old-model")
    assert reg.check(f).status == RegistryStatus.ALREADY_INGESTED


def test_different_content_is_new_regardless_of_model(tmp_path):
    reg = _registry(tmp_path)
    a, b = _file(tmp_path, "a.txt", b"aaa"), _file(tmp_path, "b.txt", b"bbb")
    reg.create_version(a, reg.check(a).sha256, 1, document_id="d1", embedding_model=MODEL)
    assert reg.check(b, embedding_model=MODEL).status == RegistryStatus.NEW_FILE


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _pipeline(tmp_path, registry=None):
    store = AsyncMock()
    pipe = IngestionPipeline(
        router=AsyncMock(),
        embedding_service=EmbeddingService(primary=_Adapter()),
        vector_store=store,
        registry=registry or _registry(tmp_path),
    )
    runs: list[str] = []

    async def fake_run_pipeline(path, user_id="system"):
        runs.append(path.name)
        return IngestionResult(
            file_path=str(path), file_category="text", document_type="general",
            total_pages=1, total_chunks=4, document_id=f"new-{len(runs)}",
        )

    pipe._run_pipeline = fake_run_pipeline  # type: ignore[assignment]
    return pipe, store, runs


@pytest.fixture(autouse=True)
def _clear_locks():
    yield
    _INGEST_LOCKS.clear()


async def test_first_ingest_records_the_model(tmp_path):
    pipe, store, runs = _pipeline(tmp_path)
    result = await pipe.ingest(_file(tmp_path))
    assert not result.skipped and runs == ["doc.txt"]
    entry = pipe._registry.get_by_document_id("new-1")
    assert entry["embedding_model"] == MODEL and entry["active"] is True
    store.delete_document.assert_not_called()


async def test_second_ingest_with_same_model_is_skipped(tmp_path):
    pipe, store, runs = _pipeline(tmp_path)
    f = _file(tmp_path)
    await pipe.ingest(f)
    again = await pipe.ingest(f)
    assert again.skipped and runs == ["doc.txt"]


async def test_stale_document_is_reindexed_and_superseded(tmp_path):
    reg = _registry(tmp_path)
    f = _file(tmp_path)
    # a document registered earlier with another embedding model
    reg.create_version(f, reg.check(f).sha256, 9, document_id="old-doc", embedding_model="jina-embeddings-v3")
    pipe, store, runs = _pipeline(tmp_path, registry=reg)

    result = await pipe.ingest(f)

    assert not result.skipped and runs == ["doc.txt"]          # pipeline ran again
    store.delete_document.assert_awaited_once_with("old-doc")   # wrong-space vectors removed
    old = reg.get_by_document_id("old-doc")
    new = reg.get_by_document_id("new-1")
    assert old["active"] is False and old["superseded_by"] == "new-1"
    assert new["active"] is True and new["embedding_model"] == MODEL and new["supersedes"] == "old-doc"
    assert [e["document_id"] for e in reg.get_active()] == ["new-1"]
    # and now it is current: the next ingest skips
    assert (await pipe.ingest(f)).skipped and runs == ["doc.txt"]


async def test_legacy_entry_without_model_is_reindexed(tmp_path):
    reg = _registry(tmp_path)
    f = _file(tmp_path)
    reg.create_version(f, reg.check(f).sha256, 9, document_id="legacy")  # like ingested_files.json
    pipe, store, runs = _pipeline(tmp_path, registry=reg)
    assert not (await pipe.ingest(f)).skipped and runs == ["doc.txt"]


async def test_reindex_to_the_same_document_id_overwrites_in_place(tmp_path):
    """Same upload path -> same document_id: no self-supersede, entry just gets the new model."""
    reg = _registry(tmp_path)
    f = _file(tmp_path)
    reg.create_version(f, reg.check(f).sha256, 9, document_id="new-1", embedding_model="other")
    pipe, store, runs = _pipeline(tmp_path, registry=reg)
    await pipe.ingest(f)  # fake pipeline also returns "new-1"
    entry = reg.get_by_document_id("new-1")
    assert entry["embedding_model"] == MODEL and entry["active"] is True
    assert entry["superseded_by"] is None and entry["supersedes"] is None
    assert len(reg.get_active()) == 1


async def test_concurrent_reindex_runs_once(tmp_path):
    reg = _registry(tmp_path)
    f = _file(tmp_path)
    reg.create_version(f, reg.check(f).sha256, 9, document_id="old-doc", embedding_model="other")
    pipe, store, runs = _pipeline(tmp_path, registry=reg)

    async def slow(path, user_id="system"):
        runs.append(path.name)
        await asyncio.sleep(0.05)
        return IngestionResult(file_path=str(path), file_category="text", document_type="general",
                               total_pages=1, total_chunks=2, document_id="new-x")

    pipe._run_pipeline = slow  # type: ignore[assignment]
    results = await asyncio.gather(pipe.ingest(f), pipe.ingest(f))
    assert len(runs) == 1 and sorted(r.skipped for r in results) == [False, True]


async def test_progress_path_reindexes_stale_documents(tmp_path):
    reg = _registry(tmp_path)
    f = _file(tmp_path)
    reg.create_version(f, reg.check(f).sha256, 9, document_id="old-doc", embedding_model="other")
    pipe, store, runs = _pipeline(tmp_path, registry=reg)
    seen: dict = {}

    async def fake_staged(path, *, content_hash, supersedes, user_id="system"):
        seen["supersedes"] = supersedes
        yield {"type": "complete", "skipped": False, "result": {"document_id": "new-1"}}

    pipe._staged_ingest_with_progress = fake_staged  # type: ignore[assignment]
    events = [e async for e in pipe.ingest_with_progress(f)]

    assert events[-1]["type"] == "complete" and events[-1]["skipped"] is False
    assert seen["supersedes"] == "old-doc"
    store.delete_document.assert_awaited_once_with("old-doc")


async def test_progress_path_still_skips_current_documents(tmp_path):
    reg = _registry(tmp_path)
    f = _file(tmp_path)
    reg.create_version(f, reg.check(f).sha256, 9, document_id="doc", embedding_model=MODEL)
    pipe, store, runs = _pipeline(tmp_path, registry=reg)
    events = [e async for e in pipe.ingest_with_progress(f)]
    assert events[0]["type"] == "skipped" and events[-1]["skipped"] is True
    store.delete_document.assert_not_called()
