"""Backward-compatibility shim for src.stages.s09_chunking.

Re-exports all symbols from src.rag.stages.s09_chunking.
"""

from __future__ import annotations

from src.rag.stages.s09_chunking import (
    Chunk,
    ChunkType,
    DocumentType,
    FigureData,
    PageContent,
    ParsedDocument,
    TableData,
    _chunk_prose,
    _estimate_tokens,
    _figure_to_text,
    _make_doc_id,
    _rows_to_text,
    _set_parent_relationships,
    _split_by_headings,
    chunk_document,
)

__all__ = [
    "Chunk",
    "ChunkType",
    "DocumentType",
    "FigureData",
    "PageContent",
    "ParsedDocument",
    "TableData",
    "_chunk_prose",
    "_estimate_tokens",
    "_figure_to_text",
    "_make_doc_id",
    "_rows_to_text",
    "_set_parent_relationships",
    "_split_by_headings",
    "chunk_document",
]
