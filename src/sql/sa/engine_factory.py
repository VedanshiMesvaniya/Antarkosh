"""SQLAlchemy engine factory: one async engine per database, built from the saved connection.

``mysql+aiomysql``, ``postgresql+asyncpg``, ``mssql+aioodbc``, ``oracle+oracledb_async`` and
``sqlite+aiosqlite`` are used. Engines never pool (``NullPool``): connections are short-lived,
as they are in the existing connectors, and each database stays fully isolated from the others.

Connection rules match the rest of the app:
* a non-default database must have its OWN complete connection; it never borrows the default
  database's host, user or password (the same guard ``run_readonly_query`` applies);
* the default database (``erp_main``) falls back to the process-wide settings, as it always has.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)

SA_DRIVERS: dict[str, str] = {
    "mysql": "mysql+aiomysql",
    "postgresql": "postgresql+asyncpg",
    "mssql": "mssql+aioodbc",
    "oracle": "oracle+oracledb_async",
    "sqlite": "sqlite+aiosqlite",
}
DEFAULT_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"


def resolve_connection(db_id: str) -> tuple[str, dict[str, Any]]:
    """Return (engine key, the database's saved connection fields), refusing an incomplete non-default one."""
    from src.core.db_client import _require_own_connection
    from src.sql import registry

    validate_db_id(db_id)
    engine = registry.resolve_db_engine(db_id)
    cfg = dict(registry.get_connection(db_id))
    if engine != "sqlite" and db_id != DEFAULT_DB_ID:
        _require_own_connection(db_id, engine, cfg)
    return engine, cfg


def _sqlite_path(cfg: dict[str, Any]) -> Path:
    from src.sql.connectors.sqlite import SQLiteConnector

    return SQLiteConnector(cfg=cfg)._resolve_path(cfg)


def build_sa_url(engine: str, cfg: dict[str, Any], db_id: str = DEFAULT_DB_ID) -> URL:
    """Build the SQLAlchemy URL for ``engine``. Special characters in passwords are handled by URL.create."""
    if engine not in SA_DRIVERS:
        raise ValueError(f"Unsupported db_engine {engine!r}. Supported engines: {sorted(SA_DRIVERS)}")
    drivername = SA_DRIVERS[engine]

    if engine == "sqlite":
        path = _sqlite_path(cfg)
        # Read-only URI, like the existing SQLite connector.
        return URL.create(drivername, database=f"file:{path.resolve().as_posix()}", query={"mode": "ro", "uri": "true"})

    # Only the default database may fall back to the process-wide settings.
    fallback = db_id == DEFAULT_DB_ID

    def pick(key: str, default: Any) -> Any:
        value = cfg.get(key)
        return value if value not in (None, "") else (default if fallback else None)

    host = pick("host", settings.db_host)
    port = pick("port", settings.db_port)
    user = pick("username", settings.db_readonly_user)
    password = pick("password", settings.db_readonly_password) or ""
    database = pick("database", settings.db_name)
    port_int = int(port) if port not in (None, "") else None

    if engine == "oracle":
        service = cfg.get("service_name") or cfg.get("database") or (settings.db_name if fallback else None)
        return URL.create(drivername, username=user, password=password, host=host, port=port_int, query={"service_name": str(service)})
    if engine == "mssql":
        odbc = cfg.get("odbc_driver") or (settings.db_odbc_driver if fallback else None) or DEFAULT_ODBC_DRIVER
        return URL.create(
            drivername, username=user, password=password, host=host, port=port_int, database=database,
            query={"driver": str(odbc), "TrustServerCertificate": "yes"},
        )
    return URL.create(drivername, username=user, password=password, host=host, port=port_int, database=database)


def create_sa_engine(db_id: str, *, statement_timeout_s: int = 30) -> AsyncEngine:
    """An async, non-pooled engine for ``db_id``. The caller must ``await engine.dispose()``."""
    engine, cfg = resolve_connection(db_id)
    url = build_sa_url(engine, cfg, db_id)
    connect_args: dict[str, Any] = {}
    if engine == "postgresql":
        connect_args["server_settings"] = {
            "application_name": "antarkosh",
            "statement_timeout": str(int(statement_timeout_s * 1000)),
        }
    logger.debug("Creating SQLAlchemy engine for %s (%s)", db_id, SA_DRIVERS[engine])
    return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)


__all__ = ["SA_DRIVERS", "build_sa_url", "create_sa_engine", "resolve_connection"]
