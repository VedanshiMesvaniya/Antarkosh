"""MSSQL database connector."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


class MSSQLConnector:
    """Read-only MSSQL database connector."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self._cfg = cfg or {}

    def _build_conn_str(self, cfg: dict[str, Any] | None = None) -> str:
        c = cfg if cfg is not None else self._cfg
        driver = c.get("odbc_driver") or settings.db_odbc_driver or "ODBC Driver 18 for SQL Server"
        host = c.get("host") or settings.db_host
        port = c.get("port") or settings.db_port
        database = c.get("database") or settings.db_name
        user = c.get("username") or settings.db_readonly_user
        password = c.get("password") or settings.db_readonly_password

        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={host},{port};"
            f"DATABASE={database};"
            f"UID={user};"
            f"PWD={password};"
            "TrustServerCertificate=yes;"
        )

    async def test(self, cfg: dict[str, Any] | None = None) -> None:
        """Test MSSQL connection."""
        try:
            import pyodbc
        except ImportError:
            raise RuntimeError(
                "The pyodbc driver is not installed on the server. Run: uv add pyodbc"
            ) from None

        conn_str = self._build_conn_str(cfg)

        def _connect() -> None:
            with pyodbc.connect(conn_str, timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()

        await asyncio.wait_for(asyncio.to_thread(_connect), timeout=8)

    async def run_readonly(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute read-only query against MSSQL."""
        import pyodbc

        conn_str = self._build_conn_str()
        timeout = int(settings.db_query_timeout_seconds)

        def _run_mssql() -> list[dict[str, Any]]:
            with pyodbc.connect(conn_str, timeout=timeout) as conn:
                with conn.cursor() as cursor:
                    if params:
                        cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                    if not cursor.description:
                        return []
                    columns = [col[0] for col in cursor.description]
                    rows = cursor.fetchall()
                    return [dict(zip(columns, row)) for row in rows]

        return await asyncio.to_thread(_run_mssql)


_default_instance = MSSQLConnector()


async def test(cfg: dict[str, Any] | None = None) -> None:
    """Test MSSQL connection."""
    await _default_instance.test(cfg)


async def run_readonly(
    sql: str,
    params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only MSSQL query."""
    return await _default_instance.run_readonly(sql, params)
