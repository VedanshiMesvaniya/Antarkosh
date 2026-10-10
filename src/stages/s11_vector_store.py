"""Backward-compatibility shim for src.stages.s11_vector_store.

Re-exports all symbols from src.rag.stages.s11_vector_store.
"""

from __future__ import annotations

from src.rag.stages.s11_vector_store import (
    Chunk,
    DimensionMismatchError,
    Protocol,
    QdrantStore,
    RetrievedChunk,
    SparseVector,
    VectorStore,
    _global_client,
    _global_client_loop,
    _global_has_sparse,
)

__all__ = [
    "Chunk",
    "DimensionMismatchError",
    "Protocol",
    "QdrantStore",
    "RetrievedChunk",
    "SparseVector",
    "VectorStore",
    "_global_client",
    "_global_client_loop",
    "_global_has_sparse",
]
