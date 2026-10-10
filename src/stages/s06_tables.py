"""Backward-compatibility shim for src.stages.s06_tables.

Re-exports all symbols from src.rag.stages.s06_tables.
"""

from __future__ import annotations

from src.rag.stages.s06_tables import (
    PageContent,
    PageStructure,
    ParsedDocument,
    ProviderRouter,
    TableData,
    _extract_tables_pdfplumber,
    _rows_to_markdown,
    _vision_table_extraction,
    check_table_extraction,
    extract_tables,
)

__all__ = [
    "PageContent",
    "PageStructure",
    "ParsedDocument",
    "ProviderRouter",
    "TableData",
    "_extract_tables_pdfplumber",
    "_rows_to_markdown",
    "_vision_table_extraction",
    "check_table_extraction",
    "extract_tables",
]
