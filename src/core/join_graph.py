"""Compatibility shim for src.core.join_graph -> src.sql.safety.join_graph."""

from __future__ import annotations

from src.sql.safety.join_graph import (
    ForeignKey,
    JoinGraphBuilder,
    JoinPath,
)

__all__ = [
    "ForeignKey",
    "JoinGraphBuilder",
    "JoinPath",
]
