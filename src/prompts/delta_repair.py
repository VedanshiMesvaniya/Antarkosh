"""Backward-compatibility shim for src.prompts.delta_repair -> src.sql.repair.delta_repair."""

from __future__ import annotations

from src.sql.repair.delta_repair import (
    DELTA_REPAIR_SYSTEM_PROMPT,
    MAX_ERROR_CHARS,
    MAX_SYSTEM_TOKENS,
    MAX_TOTAL_REPAIR_TOKENS,
    build_delta_repair_payload,
    count_tokens,
    format_compact_schema,
)
from src.sql.repair import delta_repair as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
