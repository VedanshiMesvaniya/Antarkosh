"""Backward-compatibility shim for src.core.pipeline_metrics -> src.sql.learning.pipeline_metrics."""

from __future__ import annotations

import sys
from src.sql.learning.pipeline_metrics import (
    CURRENT_DB_ID,
    METRICS_FILE,
    PipelineEvent,
    get_recent_events,
    get_score_summary,
    log_event,
)
from src.sql.learning import pipeline_metrics as _orig_mod

for _k, _v in _orig_mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v

__all__ = [k for k in globals() if not k.startswith("__")]
