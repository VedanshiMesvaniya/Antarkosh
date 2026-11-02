"""Schema drift and the record of the last good sync (auto-schema Phase 13, part 1).

A sync used to forget everything when it ended. This keeps one small file, ``databases/<db_id>/.sync/state.json``, written
only by a sync that was committed (never by one that was rolled back):

* a fingerprint per table (its columns and types, plus its declared foreign keys), and one for the whole structure,
* a digest of every generated file as the sync left it,
* what each phase reported, which phases were switched on, and the version of each phase's rules.

From it, the next sync can say exactly what drifted (tables added, dropped, changed) and can tell whether anything its phases
depend on changed. Reading and writing it never raises and never touches a generated file.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import validate_db_id
from src.sql.sync.guard import PINNED_DB_IDS, SYNC_DIR, _write_atomic

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
STATE_VERSION = 1
MAX_LISTED = 20  # table names listed per kind of change; the rest are only counted
SCHEMA_ROW_LIMIT = 20000  # the sync reads at most this many column rows: a result this long may have been cut short


def db_root(db_id: str, databases_dir: Path | None = None) -> Path:
    validate_db_id(db_id)
    return (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id


# --------------------------------------------------------------------------- content digests

VOLATILE_KEYS = ("generated_at",)  # written fresh on every run by the phases; says when, not what


def content_digest(data: bytes | None) -> str | None:
    """sha256 of a file's CONTENT: a JSON document without its top-level ``generated_at`` (and with a canonical key order).

    Several phases (the catalog above all) stamp ``generated_at`` into their files and rewrite them on every run, so the raw
    bytes differ between two runs that found exactly the same thing. Comparing raw bytes would make every sync look like a
    change: a new kept version each time, and phases that read the file never judged unchanged. A file that is not a JSON
    object is digested as it is. ``None`` for a missing file.
    """
    if data is None:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return hashlib.sha256(data).hexdigest()
    if isinstance(doc, dict):
        doc = {k: v for k, v in doc.items() if k not in VOLATILE_KEYS}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- fingerprints


def _fk_lines(fk_rows: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    lines: dict[str, list[str]] = {}
    for row in fk_rows or []:
        if not isinstance(row, dict):
            continue
        src = row.get("table_name") or row.get("TABLE_NAME") or ""
        col = row.get("column_name") or row.get("COLUMN_NAME") or ""
        dst = row.get("referenced_table_name") or row.get("REFERENCED_TABLE_NAME") or ""
        dcol = row.get("referenced_column_name") or row.get("REFERENCED_COLUMN_NAME") or ""
        if src and dst:
            lines.setdefault(str(src).lower(), []).append(f"{str(col).lower()}->{str(dst).lower()}.{str(dcol).lower()}")
    return {k: sorted(v) for k, v in lines.items()}


def table_fingerprints(table_schemas: dict[str, str], fk_rows: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """``{table name: short hash}`` of each table's DDL and its declared foreign keys (not of anything this project derived)."""
    fks = _fk_lines(fk_rows)
    out: dict[str, str] = {}
    for name, ddl in table_schemas.items():
        raw = json.dumps([str(ddl), fks.get(str(name).lower(), [])], ensure_ascii=False)
        out[str(name)] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return out


def structure_hash(fingerprints: dict[str, str]) -> str:
    """One hash for the whole structure; changes when any table is added, dropped or changed."""
    raw = json.dumps(sorted((k.lower(), v) for k, v in fingerprints.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Drift:
    first_sync: bool
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged: int

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def as_dict(self) -> dict[str, Any]:
        def cap(names: tuple[str, ...]) -> list[str]:
            return list(names[:MAX_LISTED]) + ([f"... and {len(names) - MAX_LISTED} more"] if len(names) > MAX_LISTED else [])

        return {
            "first_sync": self.first_sync,
            "added": cap(self.added),
            "removed": cap(self.removed),
            "changed": cap(self.changed),
            "counts": {"added": len(self.added), "removed": len(self.removed), "changed": len(self.changed), "unchanged": self.unchanged},
        }


def diff_tables(old: dict[str, str] | None, new: dict[str, str]) -> Drift:
    """What differs between the last good sync's tables and these. ``old`` None means there was no earlier sync."""
    if old is None:
        return Drift(True, (), (), (), len(new))
    old_l = {k.lower(): (k, v) for k, v in old.items()}
    new_l = {k.lower(): (k, v) for k, v in new.items()}
    added = tuple(sorted(new_l[k][0] for k in new_l.keys() - old_l.keys()))
    removed = tuple(sorted(old_l[k][0] for k in old_l.keys() - new_l.keys()))  # the original spelling: chunk ids are built from it
    changed = tuple(sorted(new_l[k][0] for k in new_l.keys() & old_l.keys() if new_l[k][1] != old_l[k][1]))
    return Drift(False, added, removed, changed, len(new_l.keys() & old_l.keys()) - len(changed))


# --------------------------------------------------------------------------- rule versions


def phase_versions() -> dict[str, Any]:
    """The version of each derived phase's rules. A change means the old files were built by different rules."""
    out: dict[str, Any] = {}
    try:
        from src.sql.relationships.writer import RULESET_VERSION as rel

        out["relationships"] = rel
    except Exception:  # noqa: BLE001
        out["relationships"] = None
    try:
        from src.sql.profiles.authority import RULESET_VERSION as prof

        out["table_profiles"] = prof
    except Exception:  # noqa: BLE001
        out["table_profiles"] = None
    try:
        from src.sql.paths.search import RULESET_VERSION as paths

        out["join_paths"] = paths
    except Exception:  # noqa: BLE001
        out["join_paths"] = None
    return out


# --------------------------------------------------------------------------- the state file


def _state_path(db_id: str, databases_dir: Path | None) -> Path:
    return db_root(db_id, databases_dir) / SYNC_DIR / STATE_FILE


def _valid(doc: Any) -> bool:
    return (
        isinstance(doc, dict)
        and doc.get("version") == STATE_VERSION
        and isinstance(doc.get("tables"), dict)
        and isinstance(doc.get("structure_hash"), str)
        and isinstance(doc.get("files"), dict)
        and isinstance(doc.get("phases"), dict)
        and isinstance(doc.get("enabled"), list)
    )


def load_state(db_id: str, databases_dir: Path | None = None) -> dict[str, Any] | None:
    """The record of the last good sync, or None when there is none or it cannot be trusted (never raises)."""
    try:
        if db_id in PINNED_DB_IDS:
            return None
        doc = json.loads(_state_path(db_id, databases_dir).read_text(encoding="utf-8"))
        return doc if _valid(doc) else None
    except Exception:  # noqa: BLE001 - a missing or broken record just means "no earlier sync known"
        return None


def save_state(db_id: str, state: dict[str, Any], databases_dir: Path | None = None) -> bool:
    """Write the record atomically. Never raises; False when it could not be written."""
    try:
        if db_id in PINNED_DB_IDS or not db_root(db_id, databases_dir).is_dir():
            return False
        doc = {"version": STATE_VERSION, "committed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **state}
        if not _valid(doc):
            return False
        _write_atomic(_state_path(db_id, databases_dir), json.dumps(doc, indent=1, sort_keys=True).encode("utf-8"))
        return True
    except Exception as exc:  # noqa: BLE001 - keeping a record must never fail a sync
        logger.warning("Could not write the sync state for %s: %s", db_id, exc)
        return False


def invalidate_state(db_id: str, reason: str, databases_dir: Path | None = None) -> bool:
    """Mark the record as not trustworthy (files were changed behind it): the next sync then runs every phase."""
    state = load_state(db_id, databases_dir)
    if state is None:
        return False
    state = {**state, "clean": False, "invalid_reason": reason}
    return save_state(db_id, state, databases_dir)


__all__ = [
    "MAX_LISTED",
    "SCHEMA_ROW_LIMIT",
    "STATE_FILE",
    "VOLATILE_KEYS",
    "Drift",
    "content_digest",
    "db_root",
    "diff_tables",
    "invalidate_state",
    "load_state",
    "phase_versions",
    "save_state",
    "structure_hash",
    "table_fingerprints",
]
