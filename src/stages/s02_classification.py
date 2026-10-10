"""Backward-compatibility shim for src.stages.s02_classification.

Re-exports all symbols from src.rag.stages.s02_classification.
"""

from __future__ import annotations

from src.rag.stages.s02_classification import (
    ClassificationResult,
    DocumentType,
    FileCategory,
    FileDetectionResult,
    PageStructure,
    _CLASSIFICATION_PROMPT,
    _MIN_CHARS_PER_PAGE,
    _MIN_QUALITY_RATIO,
    _classify_page_structure,
    _classify_pdf_structure,
    classify_semantic,
    classify_structure,
)

__all__ = [
    "ClassificationResult",
    "DocumentType",
    "FileCategory",
    "FileDetectionResult",
    "PageStructure",
    "_CLASSIFICATION_PROMPT",
    "_MIN_CHARS_PER_PAGE",
    "_MIN_QUALITY_RATIO",
    "_classify_page_structure",
    "_classify_pdf_structure",
    "classify_semantic",
    "classify_structure",
]
