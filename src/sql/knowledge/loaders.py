"""Loaders for database knowledge pack files with fallback to legacy locations."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"
DATABASES_DIR = PROJECT_ROOT / "databases"

logger = logging.getLogger(__name__)

DEFAULT_DB_ID = "erp_main"

DB_ID_REGEX = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class KnowledgeFileNotFound(FileNotFoundError):
    """Raised when a knowledge file for a database does not exist."""


def validate_db_id(db_id: str) -> None:
    """Validate database ID format: lowercase alphanumeric and underscores, length 2-40, starts with letter."""
    if not isinstance(db_id, str) or not DB_ID_REGEX.match(db_id):
        raise ValueError(
            f"Invalid db_id {db_id!r}: must match regex ^[a-z][a-z0-9_]{{1,39}}$ "
            "(lowercase alphanumeric and underscores, starting with letter, length 2-40)"
        )


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
    and falling back to the old config/ location with a warning only for erp_main.
    """
    if relative_path is None:
        # Support calling with just relative_path as first arg: get_database_knowledge_path("schema/relationships.json")
        if "/" in db_id or "\\" in db_id or db_id.endswith(".json"):
            relative_path = db_id
            db_id = DEFAULT_DB_ID
        else:
            raise ValueError("relative_path must be specified")

    validate_db_id(db_id)

    rel_p = Path(relative_path)
    if rel_p.is_absolute() or ".." in rel_p.parts:
        raise ValueError(f"Invalid relative_path (traversal/absolute): {relative_path!r}")

    target_path = DATABASES_DIR / db_id / rel_p
    try:
        target_path.resolve().relative_to(DATABASES_DIR.resolve())
    except ValueError:
        raise ValueError(f"Path traversal outside DATABASES_DIR: {relative_path!r}")

    if target_path.exists():
        return target_path

    if db_id != DEFAULT_DB_ID:
        raise KnowledgeFileNotFound(
            f"Knowledge file {relative_path!r} missing for database {db_id!r} at {target_path}"
        )

    # Determine legacy fallback for erp_main
    legacy_path = None
    norm_rel = str(rel_p).replace("\\", "/")
    for k_rel, k_leg in _KNOWLEDGE_PATHS.values():
        if norm_rel == k_rel:
            legacy_path = k_leg
            break
    if legacy_path is None:
        legacy_path = CONFIG_DIR / rel_p.name

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
        if db_id != DEFAULT_DB_ID:
            raise KnowledgeFileNotFound(f"Knowledge file for kind {kind!r} missing at {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load knowledge JSON from %s: %s", path, e)
        return {}


def get_learned_path(
    filename: str,
    db_id: str = DEFAULT_DB_ID,
    create_dir: bool = True,
    databases_dir: Path | None = None,
) -> Path:
    """Return Path to a learned file in databases/<db_id>/learned/<filename>.

    If create_dir is True (default for write operations), the learned/
    directory is created if it does not already exist.
    """
    validate_db_id(db_id)
    rel_p = Path(filename)
    if rel_p.is_absolute() or ".." in rel_p.parts:
        raise ValueError(f"Invalid filename (traversal/absolute): {filename!r}")

    learned_env = os.environ.get("LEARNED_DATA_DIR", "").strip()
    if learned_env and databases_dir is None:
        base_dir = Path(learned_env).expanduser()
        target_dir = base_dir / db_id
    else:
        base_dir = databases_dir or DATABASES_DIR
        target_dir = base_dir / db_id / "learned"

    try:
        target_dir.resolve().relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(f"Path traversal outside base directory: {filename!r}")

    if create_dir:
        target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / rel_p


def resolve_learned_read_path(
    filename: str,
    fallback_path: Path | str,
    db_id: str = DEFAULT_DB_ID,
    databases_dir: Path | None = None,
) -> Path:
    """Resolve learned file path for reading: prefer databases/<db_id>/learned/<filename>,
    falling back to fallback_path if the database-specific file does not exist.
    """
    target = get_learned_path(filename, db_id=db_id, create_dir=False, databases_dir=databases_dir)
    if target.exists():
        return target

    aliases = {
        "learned_patterns.jsonl": "patterns.jsonl",
        "patterns.jsonl": "learned_patterns.jsonl",
        "learning_metrics.json": "metrics.json",
        "metrics.json": "learning_metrics.json",
        "schema_drift_log.jsonl": "drift_log.jsonl",
        "drift_log.jsonl": "schema_drift_log.jsonl",
    }
    if filename in aliases:
        alias_target = get_learned_path(aliases[filename], db_id=db_id, create_dir=False, databases_dir=databases_dir)
        if alias_target.exists():
            return alias_target

    return Path(fallback_path)

