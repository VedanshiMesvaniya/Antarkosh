"""MySQL database connector."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


class MySQLConnector:
    """Read-only MySQL database connector."""

    async def test(self, cfg: dict[str, Any] | None = None) -> None:
        """Test MySQL connection."""
        import aiomysql

        c = cfg or {}
        host = c.get("host") or settings.db_host
        port = int(c.get("port") or settings.db_port)
        user = c.get("username") or settings.db_readonly_user
        password = c.get("password") or settings.db_readonly_password
        database = c.get("database") or settings.db_name

        conn = await asyncio.wait_for(
            aiomysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                db=database,
                connect_timeout=5,
            ),
            timeout=8,
        )
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
        finally:
            conn.close()

    async def run_readonly(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Open a read-only connection as dedicated read-only user and run query."""
        import aiomysql

        conn = await aiomysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_readonly_user,
            password=settings.db_readonly_password,
            db=settings.db_name,
            cursorclass=aiomysql.cursors.DictCursor,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                rows = await cursor.fetchall()
                return list(rows)
        finally:
            conn.close()


_default_instance = MySQLConnector()


async def test(cfg: dict[str, Any] | None = None) -> None:
    """Test MySQL connection."""
    await _default_instance.test(cfg)


async def run_readonly(
    sql: str,
    params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only MySQL query."""
    return await _default_instance.run_readonly(sql, params)
