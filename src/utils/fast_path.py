"""Backward-compatibility shim for src.utils.fast_path -> src.sql.fast_path."""

from __future__ import annotations

from src.sql.fast_path import (
    DISABLED_TEMPLATES,
    DISQUALIFIERS,
    FAIL_RATE_THRESHOLD,
    QueryType,
    build_aggregate_micro_prompt,
    classify_query,
    fast_path_format,
    format_aggregate_fast_path,
    format_list_fast_path,
    is_pure_factual,
    is_template_enabled,
)
from src.sql import fast_path as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
