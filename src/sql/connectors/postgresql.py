"""PostgreSQL database connector."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


class PostgreSQLConnector:
    """Read-only PostgreSQL database connector."""

    async def test(self, cfg: dict[str, Any] | None = None) -> None:
        """Test PostgreSQL connection."""
        try:
            import asyncpg
        except ImportError:
            raise RuntimeError(
                "The PostgreSQL driver is not installed on the server. Run: uv add asyncpg"
            ) from None

        c = cfg or {}
        host = c.get("host") or settings.db_host
        port = int(c.get("port") or settings.db_port)
        user = c.get("username") or settings.db_readonly_user
        password = c.get("password") or settings.db_readonly_password
        database = c.get("database") or settings.db_name

        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                timeout=5,
            ),
            timeout=8,
        )
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

    async def run_readonly(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Open a read-only transaction and execute query."""
        import asyncpg

        # Unlike MySQL, PostgreSQL can enforce read-only at the transaction level,
        # so this holds even if the connected role has write grants. A read-only
        # role is still the recommended deployment, as with MySQL.
        # asyncpg (not psycopg) on purpose: it runs on Windows' default asyncio loop.
        if isinstance(params, dict):
            raise ValueError("Named query parameters are not supported for PostgreSQL")
        args = tuple(params) if params else ()

        timeout_seconds = settings.db_query_timeout_seconds
        conn = await asyncpg.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_readonly_user,
            password=settings.db_readonly_password,
            database=settings.db_name,
            timeout=10,
            server_settings={
                "application_name": "antarkosh",
                # Server-side backstop for the asyncio.timeout in the caller.
                "statement_timeout": str(int(timeout_seconds * 1000)),
            },
        )
        try:
            async with conn.transaction(readonly=True):
                records = await conn.fetch(sql, *args)
            return [dict(r) for r in records]
        finally:
            try:
                await asyncio.wait_for(conn.close(), timeout=5)
            except Exception:  # a cancelled/stuck connection: drop it rather than hang
                conn.terminate()


_default_instance = PostgreSQLConnector()


async def test(cfg: dict[str, Any] | None = None) -> None:
    """Test PostgreSQL connection."""
    await _default_instance.test(cfg)


async def run_readonly(
    sql: str,
    params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only PostgreSQL query."""
    return await _default_instance.run_readonly(sql, params)
