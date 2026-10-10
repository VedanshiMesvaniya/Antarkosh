"""Compatibility shim for src.core.pattern_learner -> src.sql.learning.pattern_learner."""

from __future__ import annotations

from src.sql.learning.pattern_learner import (
    LEARNED_PATTERNS_PATH,
    LEARNING_METRICS_PATH,
    LearnedPattern,
    PatternLearner,
)

__all__ = [
    "LEARNED_PATTERNS_PATH",
    "LEARNING_METRICS_PATH",
    "LearnedPattern",
    "PatternLearner",
]
