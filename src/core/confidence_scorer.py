"""Compatibility shim for src.core.confidence_scorer -> src.sql.safety.confidence_scorer."""

from __future__ import annotations

from src.sql.safety.confidence_scorer import (
    ConfidenceBreakdown,
    ConfidenceScorer,
)

__all__ = [
    "ConfidenceBreakdown",
    "ConfidenceScorer",
]
