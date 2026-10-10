"""Compatibility shim for src.core.result_validator -> src.sql.safety.result_validator."""

from __future__ import annotations

from src.sql.safety.result_validator import (
    AggregationValidator,
    CardinalityValidator,
    DataTypeValidator,
    JoinPathValidator,
    ResultSanityValidator,
    ResultValidator,
    TemporalValidator,
    ValidationResult,
    ValidationSeverity,
)

__all__ = [
    "AggregationValidator",
    "CardinalityValidator",
    "DataTypeValidator",
    "JoinPathValidator",
    "ResultSanityValidator",
    "ResultValidator",
    "TemporalValidator",
    "ValidationResult",
    "ValidationSeverity",
]
