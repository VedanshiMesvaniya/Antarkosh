"""Folder scaffolding for newly registered databases (auto-schema Phase 2).

``ensure_db_folder`` gives a database that has no ``databases/<db_id>/`` folder yet a
complete one, copied from ``databases/_template/``, so every knowledge loader finds
the files it expects. It never touches a folder that already exists.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.sql import registry
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)

TEMPLATE_DIR_NAME = "_template"
# Created if missing, so a template copied from an image that drops empty or runtime
# folders (the Docker build excludes learned/ and evals/) still ends up complete.
STANDARD_SUBDIRS = ("schema", "semantics", "learned", "evals")
# Files that only exist as git placeholders, or runtime-only folders: not "missing" if absent.
_OPTIONAL_TOP_DIRS = frozenset({"learned", "evals"})
_PLACEHOLDER_NAMES = frozenset({".gitkeep"})
# Databases that are pinned to their curated, hand-maintained files and are never scaffolded.
_PINNED_DB_IDS = frozenset({DEFAULT_DB_ID})


@dataclass(frozen=True)
class ScaffoldResult:
    """Outcome of ``ensure_db_folder``."""

    db_id: str
    path: Path
    created: bool
    # Template files absent from a pre-existing folder. Informational only: nothing is written.
    missing_files: tuple[str, ...] = ()


def display_name_from_id(db_id: str) -> str:
    """``my_crm`` -> ``My Crm``. Only a starting label; the admin can rename it in db.yaml."""
    return " ".join(part.capitalize() for part in db_id.split("_") if part) or db_id


def _template_files(template: Path) -> list[str]:
    out: list[str] = []
    for p in sorted(template.rglob("*")):
        if not p.is_file() or p.name in _PLACEHOLDER_NAMES or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(template)
        if rel.parts[0] in _OPTIONAL_TOP_DIRS:
            continue
        out.append(rel.as_posix())
    return out


def _missing_files(target: Path, template: Path) -> tuple[str, ...]:
    if not target.is_dir() or not template.is_dir():
        return ()
    return tuple(rel for rel in _template_files(template) if not (target / rel).exists())


def ensure_db_folder(
    db_id: str,
    *,
    engine: str | None = None,
    display_name: str | None = None,
    owner: str | None = None,
    databases_dir: Path | None = None,
) -> ScaffoldResult:
    """Create ``databases/<db_id>/`` from the template if (and only if) it does not exist.

    * An existing folder is returned untouched (``created=False``).
    * The new folder is built in a temporary sibling and renamed into place, so a crash or a
      concurrent call can never leave a half-built folder under the real name.
    * ``db.yaml`` gets ``id``, ``display_name``, ``engine`` (explicit, else the database's own
      connection/default engine), ``connection`` (its key in config/connections.json) and an
      empty ``description``. Everything else (``enabled``, ``soft_delete``, ``schema_sync``...) stays as
      the template defines it.
    * With ``owner`` (the admin who connected the database) the folder also records ``owner``,
      ``created_by`` and ``created_at``, and ``allowed_users`` starts as just that owner, so nobody else
      can read this database's data unless the owner grants it.

    Raises ValueError for an invalid or pinned ``db_id`` or an unsupported engine, and
    FileNotFoundError if the template itself is missing. Nothing is left behind on failure.
    """
    validate_db_id(db_id)
    base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
    target = base / db_id
    template = base / TEMPLATE_DIR_NAME

    if target.exists():
        return ScaffoldResult(db_id, target, created=False, missing_files=_missing_files(target, template))

    if db_id in _PINNED_DB_IDS:
        raise ValueError(f"{db_id!r} is pinned to its curated files and is never scaffolded")
    if not (template / "db.yaml").is_file():
        raise FileNotFoundError(f"Database template not found at {template} (needs db.yaml)")

    resolved_engine = Engine.from_value(engine if engine else registry.resolve_db_engine(db_id)).value

    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"_scaffold_{db_id}_", dir=base))  # '_' prefix: list_databases skips it
    try:
        shutil.copytree(
            template, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        try:
            os.chmod(tmp, 0o755)  # mkdtemp creates 0700; match a normally created folder
        except OSError:
            pass
        for sub in STANDARD_SUBDIRS:
            (tmp / sub).mkdir(exist_ok=True)

        with open(tmp / "db.yaml", "r", encoding="utf-8") as fh:
            meta = yaml.safe_load(fh) or {}
        meta["id"] = db_id
        meta["display_name"] = display_name or display_name_from_id(db_id)
        meta["engine"] = resolved_engine
        meta["connection"] = db_id
        meta["description"] = ""
        meta["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        if owner:
            meta["created_by"] = owner
            meta["allowed_users"] = ["*"]
        registry.atomic_write_yaml(tmp / "db.yaml", meta)

        try:
            os.rename(tmp, target)
        except OSError:
            if target.exists():  # lost a race: someone else created it first. Theirs wins, untouched.
                return ScaffoldResult(db_id, target, created=False, missing_files=_missing_files(target, template))
            raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # no-op after a successful rename

    logger.info("Scaffolded database folder for %s (engine %s)", db_id, resolved_engine)
    return ScaffoldResult(db_id, target, created=True)


def scaffold_if_enabled(db_id: str) -> ScaffoldResult | None:
    """Sync-time entry point: scaffold when the per-DB ``auto_scaffold`` flag is on.

    Never raises: if scaffolding fails the sync carries on exactly as it did before.
    Returns None when the flag is off or scaffolding failed.
    """
    from src.utils.feature_flags import is_db_feature_enabled

    if not is_db_feature_enabled("auto_scaffold", db_id):
        return None
    try:
        result = ensure_db_folder(db_id)
    except Exception as exc:  # noqa: BLE001 - scaffolding must never block a schema sync
        logger.warning("Folder scaffolding skipped for %s: %s", db_id, exc)
        return None

    if result and result.created:
        try:  # drop anything cached while the folder did not exist
            from src.sql.pipeline import clear_knowledge_caches

            clear_knowledge_caches(db_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not clear knowledge caches for %s: %s", db_id, exc)
    return result


def activate_after_sync(db_id: str, synced_by: str | None = None) -> bool:
    """After a SUCCESSFUL schema sync, record it in ``db.yaml`` and make the database active.

    Never runs for a pinned database, and only when ``databases/<db_id>/db.yaml`` exists. A folder
    only exists because an admin added the connection (or turned on ``auto_scaffold``), so no
    flag is needed here. It always stamps
    ``schema_sync.last_synced_at``. It flips ``enabled`` from false to true ONLY on the first
    successful sync (``last_synced_at`` still empty), so an admin who later disables a database
    on purpose is not overridden by a re-sync. Never raises; returns True if it turned the
    database on.
    """
    if db_id in _PINNED_DB_IDS:
        return False
    try:
        yaml_path = registry.DATABASES_DIR / db_id / "db.yaml"
        if not yaml_path.is_file():
            return False
        with open(yaml_path, "r", encoding="utf-8") as fh:
            meta = yaml.safe_load(fh) or {}
        if not isinstance(meta, dict):
            return False

        sync_meta = meta.get("schema_sync")
        if not isinstance(sync_meta, dict):
            sync_meta = {}
        first_sync = not sync_meta.get("last_synced_at")
        turned_on = first_sync and meta.get("enabled") is False
        sync_meta["last_synced_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        if synced_by:
            sync_meta["synced_by"] = synced_by  # which admin ran the sync
        sync_meta.setdefault("schema_hash", None)
        meta["schema_sync"] = sync_meta
        if turned_on:
            meta["enabled"] = True
        registry.atomic_write_yaml(yaml_path, meta)

        try:  # DatabaseContext is cached per db_id
            from src.sql.context import clear_context_cache

            clear_context_cache(db_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not clear context cache for %s: %s", db_id, exc)
        if turned_on:
            logger.info("Database %s is now active after its first successful schema sync", db_id)
        return turned_on
    except Exception as exc:  # noqa: BLE001 - never turn a successful sync into a failure
        logger.warning("Could not record sync result for %s: %s", db_id, exc)
        return False
