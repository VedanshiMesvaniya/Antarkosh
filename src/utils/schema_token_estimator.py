"""Backward-compatibility shim for src.utils.schema_token_estimator -> src.sql.schema_token_estimator."""

from __future__ import annotations

from src.sql.schema_token_estimator import estimate_schema_tokens
from src.sql import schema_token_estimator as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
