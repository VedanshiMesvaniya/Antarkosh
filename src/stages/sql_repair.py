"""Backward-compatibility shim for src.stages.sql_repair -> src.sql.repair.sql_repair."""

from __future__ import annotations

from src.sql.repair.sql_repair import (
    MAX_DELTA_REPAIR_ATTEMPTS,
    attempt_delta_repair,
    extract_schema_context_from_ddl,
    extract_sql_from_response,
)
from src.sql.repair import sql_repair as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
