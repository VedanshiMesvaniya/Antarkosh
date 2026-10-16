"""PostgreSQL database connector.

Origin: merged from src/core/db_settings.py (_test_postgresql) and src/core/db_client.py (postgresql branch)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


def _schema_from(cfg: dict[str, Any]) -> str:
    """The connection's pinned schema ('' = none). Re-validated here: it goes into a server setting."""
    from src.sql.engine import validate_schema_name

    return validate_schema_name(cfg.get("schema_name"))


def _search_path(schema: str) -> str:
    """Quoted so a mixed-case schema keeps its case; the app never needs to prefix table names."""
    return '"' + schema.replace('"', '""') + '"'


class PostgreSQLConnector:
    """Read-only PostgreSQL database connector."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self._cfg = cfg or {}

    async def test(self, cfg: dict[str, Any] | None = None) -> None:
        """Test PostgreSQL connection."""
        try:
            import asyncpg
        except ImportError:
            raise RuntimeError(
                "The PostgreSQL driver is not installed on the server. Run: uv add asyncpg"
            ) from None

        c = cfg if cfg is not None else self._cfg
        host = c.get("host") or settings.db_host
        port = int(c.get("port") or settings.db_port)
        user = c.get("username") or settings.db_readonly_user
        password = c.get("password") or settings.db_readonly_password
        database = c.get("database") or settings.db_name

        schema = _schema_from(c)
        connect_kwargs: dict[str, Any] = {}
        if schema:
            connect_kwargs["server_settings"] = {"search_path": _search_path(schema)}

        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                timeout=5,
                **connect_kwargs,
            ),
            timeout=8,
        )
        try:
            await conn.fetchval("SELECT 1")
            if schema:
                # information_schema.schemata lists only schemas this user owns or may USE, so a
                # misspelt schema and a missing privilege both show up here, not as an empty sync later.
                visible = await conn.fetchval(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", schema
                )
                if not visible:
                    raise RuntimeError(
                        f"Schema {schema!r} was not found, or this user has no access to it (USAGE privilege)."
                    )
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
        c = self._cfg or {}
        host = c.get("host") or settings.db_host
        port = int(c.get("port") or settings.db_port)
        user = c.get("username") or settings.db_readonly_user
        password = c.get("password") or settings.db_readonly_password
        database = c.get("database") or settings.db_name

        server_settings = {
            "application_name": "antarkosh",
            # Server-side backstop for the asyncio.timeout in the caller.
            "statement_timeout": str(int(timeout_seconds * 1000)),
        }
        schema = _schema_from(c)
        if schema:
            # Applies to every statement on this connection: the catalog queries (current_schema())
            # and the generated SQL's bare table names both resolve inside the chosen schema.
            server_settings["search_path"] = _search_path(schema)

        conn = await asyncpg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            timeout=10,
            server_settings=server_settings,
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
