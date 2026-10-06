"""Local (CPU-friendly) model loaders — BGE-M3 embeddings and the BGE reranker.

Both models are heavy (~2 GB each), so they are:
  * imported lazily — the cloud deployment never needs FlagEmbedding/torch;
  * loaded once per process and shared (EmbeddingService / Reranker objects are
    created per request, the model must not be);
  * run in a worker thread behind a lock so they never block the event loop and
    never run two encodes at once on a CPU.

FlagEmbedding is a normal dependency of the project: ``pip install -e .`` or ``uv sync``.
"""

from __future__ import annotations

import logging
import threading
import zlib
from typing import Any

from src.core.embeddings import SparseVector

logger = logging.getLogger(__name__)

_load_lock = threading.Lock()
_run_lock = threading.Lock()
_models: dict[tuple[str, str, bool], Any] = {}


class LocalModelUnavailable(RuntimeError):
    """The optional local-model dependencies are missing or the model failed to load."""


def _import_flag_embedding() -> Any:
    try:
        import FlagEmbedding  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise LocalModelUnavailable(
            "FlagEmbedding is not installed. Run: pip install -e .  (or: uv sync)"
        ) from exc
    return FlagEmbedding


def _get_model(kind: str, model_name: str, use_fp16: bool) -> Any:
    """Return the cached model, loading it on first use."""
    key = (kind, model_name, use_fp16)
    model = _models.get(key)
    if model is not None:
        return model
    with _load_lock:
        model = _models.get(key)
        if model is not None:
            return model
        fe = _import_flag_embedding()
        logger.info("Loading local %s model '%s' (first use — this can take a minute)", kind, model_name)
        try:
            if kind == "embed":
                model = fe.BGEM3FlagModel(model_name, use_fp16=use_fp16)
            else:
                model = fe.FlagReranker(model_name, use_fp16=use_fp16)
        except Exception as exc:  # noqa: BLE001
            raise LocalModelUnavailable(f"Could not load '{model_name}': {exc}") from exc
        _models[key] = model
        return model


def reset_cache() -> None:
    """Drop cached models (used by tests)."""
    with _load_lock:
        _models.clear()


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def _token_index(key: Any) -> int:
    """BGE-M3 lexical weights are keyed by token id (a str of an int)."""
    try:
        return int(key)
    except (TypeError, ValueError):
        # Defensive: if a future version keys by token text, hash it stably.
        return zlib.crc32(str(key).encode("utf-8")) & 0x7FFFFFFF


def lexical_weights_to_sparse(weights: dict[Any, Any] | None) -> SparseVector:
    """Convert a BGE-M3 lexical-weights dict to Qdrant-style parallel arrays."""
    if not weights:
        return SparseVector()
    merged: dict[int, float] = {}
    for key, value in weights.items():
        idx = _token_index(key)
        val = float(value)
        if val <= 0.0:
            continue
        # Collisions (only possible with hashed keys) keep the larger weight.
        merged[idx] = max(val, merged.get(idx, 0.0))
    ordered = sorted(merged.items())
    return SparseVector(indices=[i for i, _ in ordered], values=[v for _, v in ordered])


def embed_sync(
    texts: list[str],
    *,
    model_name: str,
    use_fp16: bool,
    max_length: int,
    batch_size: int,
) -> tuple[list[list[float]], list[SparseVector]]:
    """Blocking BGE-M3 encode → (dense vectors, sparse vectors)."""
    model = _get_model("embed", model_name, use_fp16)
    with _run_lock:
        out = model.encode(
            texts,
            batch_size=batch_size,
            max_length=max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
    dense = [[float(x) for x in vec] for vec in out["dense_vecs"]]
    sparse = [lexical_weights_to_sparse(w) for w in out["lexical_weights"]]
    if len(dense) != len(texts) or len(sparse) != len(texts):
        raise LocalModelUnavailable(
            f"BGE-M3 returned {len(dense)} vectors for {len(texts)} texts"
        )
    return dense, sparse


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------

def rerank_scores_sync(
    query: str,
    documents: list[str],
    *,
    model_name: str,
    use_fp16: bool,
    max_length: int,
    batch_size: int,
) -> list[float]:
    """Blocking cross-encoder scores in [0, 1] (sigmoid-normalised), one per document."""
    if not documents:
        return []
    model = _get_model("rerank", model_name, use_fp16)
    pairs = [[query, doc] for doc in documents]
    with _run_lock:
        scores = model.compute_score(
            pairs, batch_size=batch_size, max_length=max_length, normalize=True
        )
    if isinstance(scores, (int, float)):  # FlagEmbedding returns a bare float for one pair
        scores = [scores]
    result = [float(s) for s in scores]
    if len(result) != len(documents):
        raise LocalModelUnavailable(
            f"Reranker returned {len(result)} scores for {len(documents)} documents"
        )
    return result
