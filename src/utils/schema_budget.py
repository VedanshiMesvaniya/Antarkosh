"""Backward-compatibility shim for src.utils.schema_budget -> src.sql.schema_budget."""

from __future__ import annotations

from src.sql.schema_budget import (
    DEFAULT_SCHEMA_TOKEN_BUDGET,
    select_schema_within_budget,
)
from src.sql import schema_budget as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
