"""Database context definition and per-database caching."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from src.core.config import DATABASES_DIR
from src.sql.engine import Engine
from src.sql.knowledge.loaders import (
    DEFAULT_DB_ID,
    get_knowledge_path,
    validate_db_id,
)
from src.sql.registry import get_database

logger = logging.getLogger(__name__)


class UnknownDatabaseError(FileNotFoundError, KeyError, ValueError):
    """Raised when a requested database ID does not exist or has no configuration."""


@dataclass(frozen=True)
class DatabaseContext:
    """Immutable runtime context for a specific configured database."""

    db_id: str
    engine: Engine
    display_name: str
    description: str
    schema_path: Path
    relationships_path: Path
    glossary_path: Path
    column_glossary_path: Path
    atlas_path: Path
    soft_delete_column: str | None = None
    routing_hints_path: Path | None = None

    @property
    def behavioral_atlas_path(self) -> Path:
        """Alias for atlas_path for descriptive compatibility."""
        return self.atlas_path

    @property
    def paths(self) -> dict[str, Path]:
        """Dictionary of all knowledge file paths for this database."""
        result: dict[str, Path] = {
            "schema": self.schema_path,
            "relationships": self.relationships_path,
            "glossary": self.glossary_path,
            "column_glossary": self.column_glossary_path,
            "atlas": self.atlas_path,
            "behavioral_atlas": self.atlas_path,
        }
        if self.routing_hints_path is not None:
            result["routing_hints"] = self.routing_hints_path
        return result


_CONTEXT_CACHE: dict[str, DatabaseContext] = {}


def get_context(db_id: str = DEFAULT_DB_ID) -> DatabaseContext:
    """Retrieve the DatabaseContext for db_id, loading and caching it if not yet cached."""
    validate_db_id(db_id)

    if db_id in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[db_id]

    try:
        data = get_database(db_id)
    except FileNotFoundError as e:
        yaml_path = DATABASES_DIR / db_id / "db.yaml"
        raise UnknownDatabaseError(
            f"Database configuration not found for {db_id!r} at {yaml_path}"
        ) from e

    engine = Engine.from_value(data["engine"])

    display_name = str(data.get("display_name") or db_id)
    description = str(data.get("description") or "")

    sd = data.get("soft_delete")
    if isinstance(sd, dict):
        soft_delete_column = sd.get("column")
    elif isinstance(sd, str):
        soft_delete_column = sd
    else:
        soft_delete_column = None

    schema_path = get_knowledge_path("schema", db_id=db_id)
    relationships_path = get_knowledge_path("relationships", db_id=db_id)
    glossary_path = get_knowledge_path("glossary", db_id=db_id)
    column_glossary_path = get_knowledge_path("column_glossary", db_id=db_id)
    atlas_path = get_knowledge_path("behavioral_atlas", db_id=db_id)

    try:
        routing_hints_path: Path | None = get_knowledge_path("routing_hints", db_id=db_id)
    except (KeyError, ValueError, FileNotFoundError, OSError):
        routing_hints_path = None

    context = DatabaseContext(
        db_id=db_id,
        engine=engine,
        display_name=display_name,
        description=description,
        schema_path=schema_path,
        relationships_path=relationships_path,
        glossary_path=glossary_path,
        column_glossary_path=column_glossary_path,
        atlas_path=atlas_path,
        soft_delete_column=soft_delete_column,
        routing_hints_path=routing_hints_path,
    )

    _CONTEXT_CACHE[db_id] = context
    return context


def clear_context_cache(db_id: str | None = None) -> None:
    """Clear cached DatabaseContext instances for all databases or a specific db_id."""
    if db_id is None:
        _CONTEXT_CACHE.clear()
    else:
        _CONTEXT_CACHE.pop(db_id, None)


__all__ = [
    "DEFAULT_DB_ID",
    "DatabaseContext",
    "UnknownDatabaseError",
    "clear_context_cache",
    "get_context",
]
