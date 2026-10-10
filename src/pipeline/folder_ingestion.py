"""Backward-compatibility shim for src.pipeline.folder_ingestion -> src.rag.folder_ingestion."""

from __future__ import annotations

import sys
import types

import src.rag.folder_ingestion as _target_module
from src.rag.folder_ingestion import (
    FolderIngestionResult,
    _discover_files,
    _Ingester,
    _IngestResult,
    run_periodic_scan,
    scan_and_ingest,
)

__all__ = [
    "FolderIngestionResult",
    "_IngestResult",
    "_Ingester",
    "_discover_files",
    "run_periodic_scan",
    "scan_and_ingest",
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
