"""Backward-compatibility shim for src.utils.empty_result_classifier -> src.sql.safety.empty_result_classifier."""

from __future__ import annotations

from src.sql.safety.empty_result_classifier import (
    SUSPICIOUS_EMPTY,
    VALID_EMPTY,
    classify_empty_result,
)
from src.sql.safety import empty_result_classifier as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
