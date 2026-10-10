"""Backward-compatibility shim for src.utils.semantic_cache -> src.sql.semantic_cache."""

from __future__ import annotations

from src.sql.semantic_cache import (
    MAX_ENTRIES_PER_SCOPE,
    SIMILARITY_THRESHOLD,
    CachedEntry,
    SemanticCache,
    get_semantic_cache,
)
from src.sql import semantic_cache as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
