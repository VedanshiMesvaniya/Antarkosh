"""MSSQL dialect profile stub."""

from __future__ import annotations

from src.sql.dialects.base import SQLDialectProfile


def get_profile() -> SQLDialectProfile:
    """Build MSSQL dialect profile.

    Raises:
        NotImplementedError: Always, as MSSQL profile needs validation against a real instance.
    """
    raise NotImplementedError("needs validation against a real instance")
