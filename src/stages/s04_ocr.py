"""Backward-compatibility shim for src.stages.s04_ocr.

Re-exports all symbols from src.rag.stages.s04_ocr.
"""

from __future__ import annotations

from src.rag.stages.s04_ocr import (
    PageContent,
    PageStructure,
    ParsedDocument,
    ProviderRouter,
    _get_page_image,
    _ocr_chain,
    _vision_llm_ocr,
    check_ocr_confidence,
    run_ocr,
)

__all__ = [
    "PageContent",
    "PageStructure",
    "ParsedDocument",
    "ProviderRouter",
    "_get_page_image",
    "_ocr_chain",
    "_vision_llm_ocr",
    "check_ocr_confidence",
    "run_ocr",
]
