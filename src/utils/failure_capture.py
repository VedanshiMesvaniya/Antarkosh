"""Backward-compatibility shim for src.utils.failure_capture -> src.sql.learning.failure_capture."""

from __future__ import annotations

from src.sql.learning.failure_capture import (
    DEFAULT_FAILURE_LOG_FILE,
    capture_sql_failure,
)
from src.sql.learning import failure_capture as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
