"""Backward-compatibility shim for src.core.sql_column_registry -> src.sql.safety.column_registry."""

from __future__ import annotations

from src.sql.safety.column_registry import (
    COLUMN_GLOSSARY_PATH,
    GLOSSARY_PATH,
    ColumnRegistry,
)
from src.sql.safety import column_registry as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
