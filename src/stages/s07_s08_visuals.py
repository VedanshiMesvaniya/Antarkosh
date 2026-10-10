"""Backward-compatibility shim for src.stages.s07_s08_visuals.

Re-exports all symbols from src.rag.stages.s07_s08_visuals.
"""

from __future__ import annotations

from src.rag.stages.s07_s08_visuals import (
    FigureData,
    PageContent,
    ParsedDocument,
    PROJECT_ROOT,
    ProviderRouter,
    _LABEL_MEANING,
    _LABEL_VISIBLE,
    _NO_TEXT_MARKERS,
    _analyze_chart,
    _analyze_image,
    _analyze_visuals_impl,
    _likely_chart,
    _save_figure_image,
    _split_image_analysis,
    analyze_visuals,
)

__all__ = [
    "FigureData",
    "PageContent",
    "ParsedDocument",
    "PROJECT_ROOT",
    "ProviderRouter",
    "_LABEL_MEANING",
    "_LABEL_VISIBLE",
    "_NO_TEXT_MARKERS",
    "_analyze_chart",
    "_analyze_image",
    "_analyze_visuals_impl",
    "_likely_chart",
    "_save_figure_image",
    "_split_image_analysis",
    "analyze_visuals",
]
