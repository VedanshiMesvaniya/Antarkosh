"""SQLite database connector."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

from src.core.config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "live_data.db"


class SQLiteConnector:
    """Read-only SQLite database connector."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path else None

    def _resolve_path(self, cfg: dict[str, Any] | None = None) -> Path:
        if cfg:
            if "path" in cfg and cfg["path"]:
                return Path(cfg["path"])
            if "database" in cfg and cfg["database"]:
                return Path(cfg["database"])
        if self._db_path:
            return self._db_path
        try:
            from src.core import db_client
            return getattr(db_client, "DB_PATH", DB_PATH)
        except ImportError:
            return DB_PATH

    async def test(self, cfg: dict[str, Any] | None = None) -> None:
        """Verify the SQLite database file exists."""
        path = self._resolve_path(cfg)
        if not path.exists():
            raise FileNotFoundError(f"SQLite file not found at {path}.")

    async def run_readonly(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Open a read-only connection via URI mode=ro and execute the query."""
        path = self._resolve_path()
        if not path.exists():
            logger.warning("Database file not found at %s", path)
            return []

        # Layer 1 defense: SQLite engine-level Read-Only mode via the URI trick.
        db_uri = f"file:{path.resolve()}?mode=ro"
        async with aiosqlite.connect(db_uri, uri=True) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]


_default_instance = SQLiteConnector()


async def test(cfg: dict[str, Any] | None = None) -> None:
    """Test SQLite connection."""
    await _default_instance.test(cfg)


async def run_readonly(
    sql: str,
    params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only SQLite query."""
    return await _default_instance.run_readonly(sql, params)
