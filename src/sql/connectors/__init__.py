"""Database connectors package."""

from __future__ import annotations

from typing import Any

from src.sql.connectors.base import Connector
from src.sql.connectors.mssql import MSSQLConnector
from src.sql.connectors.mysql import MySQLConnector
from src.sql.connectors.oracle import OracleConnector
from src.sql.connectors.postgresql import PostgreSQLConnector
from src.sql.connectors.sqlite import SQLiteConnector

__all__ = [
    "Connector",
    "MSSQLConnector",
    "MySQLConnector",
    "OracleConnector",
    "PostgreSQLConnector",
    "SQLiteConnector",
    "get_connector",
]


def get_connector(engine: str, cfg: dict[str, Any] | None = None) -> Connector:
    """Return a connector instance for the requested database engine."""
    engine_normalized = (engine or "").strip().lower()
    if engine_normalized == "sqlite":
        return SQLiteConnector(cfg=cfg)
    if engine_normalized == "mysql":
        from src.sql.connectors.mysql import MySQLConnector
        return MySQLConnector(cfg=cfg)
    if engine_normalized == "postgresql":
        from src.sql.connectors.postgresql import PostgreSQLConnector
        return PostgreSQLConnector(cfg=cfg)
    if engine_normalized == "mssql":
        from src.sql.connectors.mssql import MSSQLConnector
        return MSSQLConnector(cfg=cfg)
    if engine_normalized == "oracle":
        from src.sql.connectors.oracle import OracleConnector
        return OracleConnector(cfg=cfg)
    raise ValueError(f"Unsupported db_engine {engine!r}")
