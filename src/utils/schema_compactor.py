"""Backward-compatibility shim for src.utils.schema_compactor -> src.sql.schema_compactor."""

from __future__ import annotations

from src.sql.schema_compactor import (
    AUDIT_COLUMNS,
    compact_ddl,
    extract_join_hints,
)
from src.sql import schema_compactor as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
