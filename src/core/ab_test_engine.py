"""Compatibility shim for src.core.ab_test_engine -> src.sql.learning.ab_test_engine."""

from __future__ import annotations

from src.sql.learning.ab_test_engine import (
    ABTestEngine,
    EXPERIMENT_LOG_PATH,
    EXPERIMENTS_CONFIG_PATH,
    ExperimentEvent,
)

__all__ = [
    "ABTestEngine",
    "EXPERIMENT_LOG_PATH",
    "EXPERIMENTS_CONFIG_PATH",
    "ExperimentEvent",
]
