"""Database engine definitions and dialect mapping."""

from __future__ import annotations

from enum import Enum
from typing import Any


class Engine(str, Enum):
    """Supported database engines with sqlglot and dialect keys."""

    SQLITE = "sqlite"
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    MSSQL = "mssql"
    ORACLE = "oracle"

    @property
    def key(self) -> str:
        """Normalized dialect/engine key name (matches value)."""
        return self.value

    @property
    def sqlglot(self) -> str:
        """sqlglot dialect string identifier."""
        return _SQLGLOT_DIALECT_MAP[self]

    @classmethod
    def from_value(cls, val: str | Engine) -> Engine:
        """Parse engine from string or Engine instance, supporting aliases."""
        if isinstance(val, cls):
            return val
        if isinstance(val, str):
            normalized = val.strip().lower()
            if normalized in _ENGINE_ALIASES:
                return _ENGINE_ALIASES[normalized]
        raise ValueError(f"Unknown database engine: {val!r}")


_SQLGLOT_DIALECT_MAP: dict[Engine, str] = {
    Engine.SQLITE: "sqlite",
    Engine.MYSQL: "mysql",
    Engine.POSTGRESQL: "postgres",
    Engine.MSSQL: "tsql",
    Engine.ORACLE: "oracle",
}

_ENGINE_ALIASES: dict[str, Engine] = {
    "sqlite": Engine.SQLITE,
    "mysql": Engine.MYSQL,
    "postgresql": Engine.POSTGRESQL,
    "postgres": Engine.POSTGRESQL,
    "mssql": Engine.MSSQL,
    "tsql": Engine.MSSQL,
    "oracle": Engine.ORACLE,
}

# Engine form specification and UI catalogue
# `ready` = the query layer (connectors / dialects) can run this engine today.
ENGINES: dict[str, dict[str, Any]] = {
    "mysql": {
        "label": "MySQL",
        "ready": True,
        "default_port": 3306,
        "fields": ["host", "port", "database", "username", "password"],
    },
    "postgresql": {
        "label": "PostgreSQL",
        "ready": True,
        "default_port": 5432,
        "fields": ["host", "port", "database", "username", "password"],
    },
    "sqlite": {
        "label": "SQLite",
        "ready": True,
        "default_port": None,
        "fields": [],
    },
    "mssql": {
        "label": "Microsoft SQL Server",
        "ready": True,
        "default_port": 1433,
        "fields": ["host", "port", "database", "username", "password", "odbc_driver"],
    },
    "oracle": {
        "label": "Oracle",
        "ready": True,
        "default_port": 1521,
        "fields": ["host", "port", "service_name", "username", "password"],
    },
}

REQUIRED_FIELDS: dict[str, list[str]] = {
    "mysql": ["host", "database", "username"],
    "postgresql": ["host", "database", "username"],
    "sqlite": [],
    "mssql": ["host", "database", "username"],
    "oracle": ["host", "service_name", "username"],
}

__all__ = [
    "ENGINES",
    "REQUIRED_FIELDS",
    "Engine",
]
