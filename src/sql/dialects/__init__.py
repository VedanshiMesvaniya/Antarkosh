"""SQL dialect facts and profile registry for Text-to-SQL retrieval."""

from __future__ import annotations

from src.sql.dialects.base import SQLDialectProfile
from src.sql.dialects.mysql import MYSQL_PROFILE
from src.sql.dialects.postgresql import POSTGRESQL_PROFILE
from src.sql.dialects.sqlite import SQLITE_PROFILE

__all__ = [
    "DIALECTS",
    "SQLDialectProfile",
    "get_dialect_profile",
]

DIALECTS: dict[str, SQLDialectProfile] = {
    "sqlite": SQLITE_PROFILE,
    "mysql": MYSQL_PROFILE,
    "postgresql": POSTGRESQL_PROFILE,
}


def get_dialect_profile(engine: str) -> SQLDialectProfile:
    """Look up the dialect profile for the configured engine.

    Raises:
        ValueError: If the engine isn't in DIALECTS (fails fast at startup-adjacent
            code paths rather than producing confusing downstream sqlglot errors).
    """
    try:
        return DIALECTS[engine]
    except KeyError:
        raise ValueError(
            f"Unsupported db_engine {engine!r}. Supported engines: {list(DIALECTS)}"
        ) from None
