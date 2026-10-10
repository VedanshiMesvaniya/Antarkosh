"""Backward-compatibility shim for src.pipeline.schema_ingestion -> src.sql.schema_retrieval."""

from __future__ import annotations

import sys
import types

import src.sql.schema_retrieval as _target_module
from src.sql.schema_retrieval import (
    SCHEMA_DOCUMENT_ID,
    _enrich_table_schema,
    _load_table_metadata,
    _split_mysql_tables,
    _split_schema_by_table,
    _split_sqlite_tables,
    get_schema_document_id,
    run_readonly_query,
    sync_live_schema,
)

__all__ = [
    "SCHEMA_DOCUMENT_ID",
    "_enrich_table_schema",
    "_load_table_metadata",
    "_split_mysql_tables",
    "_split_schema_by_table",
    "_split_sqlite_tables",
    "get_schema_document_id",
    "run_readonly_query",
    "sync_live_schema",
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
