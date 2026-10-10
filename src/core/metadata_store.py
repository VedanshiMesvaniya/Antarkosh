"""Backward-compatibility shim for src.core.metadata_store -> src.rag.metadata_store."""

from __future__ import annotations

from src.rag.metadata_store import (
    JsonMetadataBackend,
    MetadataBackend,
    QdrantMetadataBackend,
    create_metadata_backend,
    migrate_registry,
)
from src.rag import metadata_store as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
