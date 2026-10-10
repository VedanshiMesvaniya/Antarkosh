"""Backward-compatibility shim for src.pipeline.ingestion -> src.rag.ingestion."""

from __future__ import annotations

import sys
import types

import src.rag.ingestion as _target_module
from src.rag.ingestion import (
    _INGEST_LOCKS,
    IngestionPipeline,
    IngestionResult,
    _discard_redundant_upload,
    _lock_for_hash,
    reconcile_active_flags,
)

__all__ = [
    "_INGEST_LOCKS",
    "IngestionPipeline",
    "IngestionResult",
    "_discard_redundant_upload",
    "_lock_for_hash",
    "reconcile_active_flags",
]


class _ShimModule(types.ModuleType):
    def __getattr__(self, name: str):
        return getattr(_target_module, name)

    def __setattr__(self, name: str, value):
        super().__setattr__(name, value)
        if name != "__class__":
            setattr(_target_module, name, value)

    def __delattr__(self, name: str):
        super().__delattr__(name)
        if hasattr(_target_module, name):
            delattr(_target_module, name)


sys.modules[__name__].__class__ = _ShimModule
