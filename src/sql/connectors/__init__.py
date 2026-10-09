"""Database connectors package."""

from __future__ import annotations

from typing import Any

from src.sql.connectors.base import Connector
from src.sql.connectors.sqlite import SQLiteConnector

__all__ = [
    "Connector",
    "SQLiteConnector",
    "get_connector",
]


def get_connector(engine: str) -> Connector:
    """Return a connector instance for the requested database engine."""
    engine_normalized = (engine or "").strip().lower()
    if engine_normalized == "sqlite":
        return SQLiteConnector()
    if engine_normalized == "mysql":
        from src.sql.connectors.mysql import MySQLConnector
        return MySQLConnector()
    if engine_normalized == "postgresql":
        from src.sql.connectors.postgresql import PostgreSQLConnector
        return PostgreSQLConnector()
    if engine_normalized == "mssql":
        from src.sql.connectors.mssql import MSSQLConnector
        return MSSQLConnector()
    if engine_normalized == "oracle":
        from src.sql.connectors.oracle import OracleConnector
        return OracleConnector()
    raise ValueError(f"Unsupported db_engine {engine!r}")
