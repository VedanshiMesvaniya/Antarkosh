"""Oracle database connector."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


class OracleConnector:
    """Read-only Oracle database connector."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self._cfg = cfg or {}

    async def test(self, cfg: dict[str, Any] | None = None) -> None:
        """Test Oracle connection."""
        try:
            import oracledb
        except ImportError:
            raise RuntimeError(
                "The oracledb driver is not installed on the server. Run: uv add oracledb"
            ) from None

        c = cfg if cfg is not None else self._cfg
        user = c.get("username") or settings.db_readonly_user
        password = c.get("password") or settings.db_readonly_password
        host = c.get("host") or settings.db_host
        port = int(c.get("port") or settings.db_port)
        svc = c.get("service_name") or c.get("database") or settings.db_name

        def _connect() -> None:
            with oracledb.connect(
                user=user,
                password=password,
                host=host,
                port=port,
                service_name=svc,
                tcp_connect_timeout=5,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM DUAL")
                    cur.fetchone()

        await asyncio.wait_for(asyncio.to_thread(_connect), timeout=8)

    async def run_readonly(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute read-only query against Oracle, lower-casing column names."""
        import oracledb

        c = self._cfg or {}
        user = c.get("username") or settings.db_readonly_user
        password = c.get("password") or settings.db_readonly_password
        host = c.get("host") or settings.db_host
        port = int(c.get("port") or settings.db_port)
        svc = c.get("service_name") or c.get("database") or settings.db_name
        timeout = int(settings.db_query_timeout_seconds)

        def _run_oracle() -> list[dict[str, Any]]:
            with oracledb.connect(
                user=user,
                password=password,
                host=host,
                port=port,
                service_name=svc,
                tcp_connect_timeout=timeout,
            ) as conn:
                with conn.cursor() as cursor:
                    if params:
                        cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                    if not cursor.description:
                        return []
                    # Oracle returns column names uppercase; lower-case them for consistency
                    columns = [col[0].lower() for col in cursor.description]
                    rows = cursor.fetchall()
                    return [dict(zip(columns, row)) for row in rows]

        return await asyncio.to_thread(_run_oracle)


_default_instance = OracleConnector()


async def test(cfg: dict[str, Any] | None = None) -> None:
    """Test Oracle connection."""
    await _default_instance.test(cfg)


async def run_readonly(
    sql: str,
    params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only Oracle query."""
    return await _default_instance.run_readonly(sql, params)
