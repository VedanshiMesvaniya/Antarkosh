"""Backward-compatibility shim for src.stages.s10_embeddings.

Re-exports all symbols from src.rag.stages.s10_embeddings.
"""

from __future__ import annotations

from src.rag.stages.s10_embeddings import (
    BGEM3EmbeddingAdapter,
    Chunk,
    DimensionMismatchError,
    EmbeddingAdapter,
    EmbeddingService,
    QueryEmbeddingCache,
    SparseVector,
    _query_cache,
    get_query_embedding_cache,
)

__all__ = [
    "BGEM3EmbeddingAdapter",
    "Chunk",
    "DimensionMismatchError",
    "EmbeddingAdapter",
    "EmbeddingService",
    "QueryEmbeddingCache",
    "SparseVector",
    "_query_cache",
    "get_query_embedding_cache",
]
