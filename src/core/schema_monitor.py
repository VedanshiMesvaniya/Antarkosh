"""Compatibility shim for src.core.schema_monitor -> src.sql.learning.schema_monitor."""

from __future__ import annotations

from src.sql.learning.schema_monitor import (
    DRIFT_LOG_PATH,
    SCHEMA_ATLAS_PATH,
    SchemaDrift,
    SchemaMonitor,
)

__all__ = [
    "DRIFT_LOG_PATH",
    "SCHEMA_ATLAS_PATH",
    "SchemaDrift",
    "SchemaMonitor",
]
