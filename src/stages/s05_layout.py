"""Backward-compatibility shim for src.stages.s05_layout.

Re-exports all symbols from src.rag.stages.s05_layout.
"""

from __future__ import annotations

from src.rag.stages.s05_layout import (
    PageContent,
    PageStructure,
    ParsedDocument,
    ProviderRouter,
    _extract_headings_native,
    _text_to_markdown_with_headings,
    _vision_layout_analysis,
    analyze_layout,
)

__all__ = [
    "PageContent",
    "PageStructure",
    "ParsedDocument",
    "ProviderRouter",
    "_extract_headings_native",
    "_text_to_markdown_with_headings",
    "_vision_layout_analysis",
    analyze_layout,
]
