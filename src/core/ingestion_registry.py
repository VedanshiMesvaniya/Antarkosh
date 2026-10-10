"""Backward-compatibility shim for src.core.ingestion_registry.

Re-exports all symbols from src.rag.ingestion_registry.
"""

from __future__ import annotations

from src.rag.ingestion_registry import (
    _BUFFER_SIZE,
    IngestionRegistry,
    RegistryCheckResult,
    RegistryStatus,
)

__all__ = [
    "_BUFFER_SIZE",
    "IngestionRegistry",
    "RegistryCheckResult",
    "RegistryStatus",
]
