"""Shim module re-exporting sql dialect definitions from src.sql.dialects.

To be removed in Phase I.
"""

from __future__ import annotations

from src.sql.dialects import DIALECTS, get_dialect_profile
from src.sql.dialects.base import SQLDialectProfile

__all__ = [
    "DIALECTS",
    "SQLDialectProfile",
    "get_dialect_profile",
]
