"""Stage 10 — Embeddings (local BGE-M3).

Every embedding — document chunks at ingestion time and queries at search time —
is produced by one local model, BGE-M3: 1024-d dense plus real sparse (lexical)
vectors in a single pass. There is no cloud embedding provider and no fallback:
mixing two embedding models in one collection silently ruins retrieval, so a
failed embed raises instead of switching models.

ARCH-3: BGEM3EmbeddingAdapter is an EmbeddingAdapter implementation. Tests (or
        a future model) can inject another adapter into EmbeddingService.

ARCH-4: QdrantStore creates the collection from the service's vector_dim and
        rejects upserts whose vectors have a different dimension.
"""

from __future__ import annotations

import logging

from src.core.config import settings
from src.core.embeddings import (
    DimensionMismatchError,
    EmbeddingAdapter,
    QueryEmbeddingCache,
    SparseVector,
)
from src.models.schemas import Chunk

logger = logging.getLogger(__name__)

# Re-export so existing callers that do
#   from src.stages.s10_embeddings import SparseVector
# keep working without changes.
__all__ = [
    "BGEM3EmbeddingAdapter",
    "DimensionMismatchError",
    "EmbeddingAdapter",
    "EmbeddingService",
    "QueryEmbeddingCache",
    "SparseVector",
    "get_query_embedding_cache",
]

# ---------------------------------------------------------------------------
# Process-scoped query embedding cache (ARCH-10)
# ---------------------------------------------------------------------------

_query_cache: QueryEmbeddingCache | None = None


def get_query_embedding_cache() -> QueryEmbeddingCache:
    """Return the process-scoped singleton query embedding cache.

    Lazily initialised on first call so settings are fully loaded first.
    """
    global _query_cache
    if _query_cache is None:
        _query_cache = QueryEmbeddingCache(max_size=settings.embedding_cache_size)
    return _query_cache


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class BGEM3EmbeddingAdapter:
    """BGE-M3 running locally — 1024-d dense + real sparse (lexical) vectors.

    Needs no API key and no network. The model is loaded lazily on first use
    and shared by every adapter instance in the process (see
    src/core/local_models.py). The ``task`` argument is ignored: BGE-M3 uses
    the same encoding for queries and passages.
    """

    model_id = "bge-m3"
    vector_dim = 1024
    supports_sparse = True

    def __init__(
        self,
        model_name: str | None = None,
        use_fp16: bool | None = None,
        max_length: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._model_name = model_name or settings.bge_m3_model
        self._use_fp16 = settings.bge_m3_use_fp16 if use_fp16 is None else use_fp16
        self._max_length = max_length or settings.bge_m3_max_length
        self._batch_size = batch_size or settings.bge_m3_batch_size

    async def embed(
        self,
        texts: list[str],
        task: str = "retrieval.passage",
    ) -> tuple[list[list[float]], list[SparseVector]]:
        import asyncio

        from src.core import local_models

        if not texts:
            return [], []
        return await asyncio.to_thread(
            local_models.embed_sync,
            texts,
            model_name=self._model_name,
            use_fp16=self._use_fp16,
            max_length=self._max_length,
            batch_size=self._batch_size,
        )


# ---------------------------------------------------------------------------
# Service — adapter orchestration + ARCH-4 guard
# ---------------------------------------------------------------------------

class EmbeddingService:
    """Embeds chunks and queries with a single local model (BGE-M3 by default).

    The model is the only embedding path: there is no fallback adapter, so
    vectors in one Qdrant collection always come from the same model. A failed
    embed raises.

    Public API is unchanged from the previous implementation so all callers
    (IngestionPipeline, QueryPipeline, Retriever) work without change.
    """

    def __init__(self, primary: EmbeddingAdapter | None = None) -> None:
        self._primary: EmbeddingAdapter = primary or BGEM3EmbeddingAdapter()

    # ------------------------------------------------------------------
    # Properties (ARCH-4 — collection creation reads these)
    # ------------------------------------------------------------------

    @property
    def vector_dim(self) -> int:
        """Dimensionality of vectors produced by the primary adapter."""
        return self._primary.vector_dim

    @property
    def model_id(self) -> str:
        """Model identifier of the primary adapter."""
        return self._primary.model_id

    # ------------------------------------------------------------------
    # Public embed interface
    # ------------------------------------------------------------------

    async def embed_chunks(
        self, chunks: list[Chunk]
    ) -> tuple[list[list[float]], list[SparseVector]]:
        return await self.embed_texts([c.content for c in chunks])

    async def embed_texts(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[SparseVector]]:
        """Embed texts. Returns (dense_vectors, sparse_vectors)."""
        if not texts:
            return [], []
        try:
            return await self._primary.embed(texts)
        except Exception:
            logger.exception("Embedding with %s failed", self._primary.model_id)
            raise

    async def embed_query(self, query: str) -> tuple[list[float], SparseVector]:
        """Embed a single query string. Returns (dense_vector, sparse_vector).

        ARCH-10: checks the process-scoped QueryEmbeddingCache before calling
        the model. A cache hit skips the encode entirely —
        especially valuable for SQL queries, where the same analytical question
        is asked repeatedly and the SQL result is already cached by
        SQLRetriever._result_cache.
        """
        cache = get_query_embedding_cache()
        cached = cache.get(query)
        if cached is not None:
            logger.debug("Embedding cache hit (len=%d chars)", len(query))
            return cached

        dense_list, sparse_list = await self.embed_texts([query])
        result = dense_list[0], sparse_list[0]
        cache.put(query, result[0], result[1])
        return result

    async def embed_queries(self, queries: list[str]) -> list[list[float]]:
        """Embed multiple queries (dense only). Used for batch retrieval tests."""
        dense_list, _ = await self.embed_texts(queries)
        return dense_list
