"""Loaders for database knowledge pack files with fallback to legacy locations."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.core.config import CONFIG_DIR, DATABASES_DIR, PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_DB_ID = "erp_main"

# Mapping of kind -> (relative_path_in_db_folder, legacy_fallback_path)
_KNOWLEDGE_PATHS: dict[str, tuple[str, Path]] = {
    "schema": ("schema/schema.json", PROJECT_ROOT / "evals" / "Antarkosh" / "Antarkosh_schema.json"),
    "relationships": ("schema/relationships.json", CONFIG_DIR / "sql_relationships.json"),
    "glossary": ("semantics/glossary.json", CONFIG_DIR / "sql_glossary.json"),
    "column_glossary": ("semantics/column_glossary.json", CONFIG_DIR / "sql_column_glossary.json"),
    "behavioral_atlas": ("semantics/behavioral_atlas.json", CONFIG_DIR / "behavioral_schema_atlas.json"),
    "routing_hints": ("semantics/routing_hints.json", CONFIG_DIR / "routing_hints.json"),
}


def get_database_knowledge_path(db_id: str = DEFAULT_DB_ID, relative_path: str | None = None) -> Path:
    """Return the Path for a knowledge file, preferring databases/<db_id>/<relative_path>
    and falling back to the old config/ location with a warning if missing.
    """
    if relative_path is None:
        # Support calling with just relative_path as first arg: get_database_knowledge_path("schema/relationships.json")
        if "/" in db_id or "\\" in db_id or db_id.endswith(".json"):
            relative_path = db_id
            db_id = DEFAULT_DB_ID
        else:
            raise ValueError("relative_path must be specified")

    target_path = DATABASES_DIR / db_id / relative_path
    if target_path.exists():
        return target_path

    # Determine legacy fallback
    legacy_path = None
    norm_rel = relative_path.replace("\\", "/")
    for _kind, (k_rel, k_leg) in _KNOWLEDGE_PATHS.items():
        if norm_rel == k_rel:
            legacy_path = k_leg
            break
    if legacy_path is None:
        legacy_path = CONFIG_DIR / Path(relative_path).name

    logger.warning(
        "Database knowledge file %s missing at %s; falling back to legacy location %s",
        relative_path, target_path, legacy_path
    )
    return legacy_path


def get_knowledge_path(kind: str, db_id: str = DEFAULT_DB_ID) -> Path:
    """Return the Path for a knowledge file by kind, preferring databases/<db_id>/ and falling back to legacy path."""
    if kind not in _KNOWLEDGE_PATHS:
        raise ValueError(f"Unknown knowledge file kind: {kind!r}")

    rel_path, _ = _KNOWLEDGE_PATHS[kind]
    return get_database_knowledge_path(db_id=db_id, relative_path=rel_path)


def load_knowledge_json(kind: str, db_id: str = DEFAULT_DB_ID) -> Any:
    """Load JSON data for a knowledge file kind, preferring databases/<db_id>/ with fallback."""
    path = get_knowledge_path(kind, db_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load knowledge JSON from %s: %s", path, e)
        return {}
