"""Unified SQL caches module (semantic cache and result cache helpers)."""

from __future__ import annotations

from src.sql.semantic_cache import (
    CachedEntry,
    SemanticCache,
    get_semantic_cache,
)

build_scope_key = SemanticCache.build_scope_key

__all__ = [
    "CachedEntry",
    "SemanticCache",
    "build_scope_key",
    "get_semantic_cache",
]
