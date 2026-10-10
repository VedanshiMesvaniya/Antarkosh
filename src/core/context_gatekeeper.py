"""Compatibility shim for src.core.context_gatekeeper -> src.pipeline.context_gatekeeper."""

from __future__ import annotations

from src.pipeline.context_gatekeeper import (
    ContextAction,
    ContextGatekeeper,
    ConversationState,
)

__all__ = [
    "ContextAction",
    "ContextGatekeeper",
    "ConversationState",
]
