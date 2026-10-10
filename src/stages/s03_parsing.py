"""Backward-compatibility shim for src.stages.s03_parsing.

Re-exports all symbols from src.rag.stages.s03_parsing.
"""

from __future__ import annotations

from src.rag.stages.s03_parsing import (
    ClassificationResult,
    FileCategory,
    FileDetectionResult,
    PageContent,
    PageStructure,
    ParsedDocument,
    _extract_pdf_metadata,
    _parse_csv,
    _parse_doc,
    _parse_docx,
    _parse_html,
    _parse_image,
    _parse_json,
    _parse_pdf,
    _parse_ppt,
    _parse_pptx,
    _parse_text,
    _parse_tsv,
    _parse_xlsx,
    parse_document,
)

__all__ = [
    "ClassificationResult",
    "FileCategory",
    "FileDetectionResult",
    "PageContent",
    "PageStructure",
    "ParsedDocument",
    "_extract_pdf_metadata",
    "_parse_csv",
    "_parse_doc",
    "_parse_docx",
    "_parse_html",
    "_parse_image",
    "_parse_json",
    "_parse_pdf",
    "_parse_ppt",
    "_parse_pptx",
    "_parse_text",
    "_parse_tsv",
    "_parse_xlsx",
    "parse_document",
]
