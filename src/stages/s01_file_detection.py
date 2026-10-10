"""Backward-compatibility shim for src.stages.s01_file_detection.

Re-exports all symbols from src.rag.stages.s01_file_detection.
"""

from __future__ import annotations

from src.rag.stages.s01_file_detection import (
    FileCategory,
    FileDetectionResult,
    _EXT_MAP,
    _IMAGE_MIME_PREFIX,
    _MIME_MAP,
    _detect_via_filetype,
    _detect_via_magic,
    _detect_via_mimetypes,
    _mime_to_category,
    detect_file,
)

__all__ = [
    "FileCategory",
    "FileDetectionResult",
    "_EXT_MAP",
    "_IMAGE_MIME_PREFIX",
    "_MIME_MAP",
    "_detect_via_filetype",
    "_detect_via_magic",
    "_detect_via_mimetypes",
    "_mime_to_category",
    "detect_file",
]
