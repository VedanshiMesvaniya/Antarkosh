"""Keep the last good set of generated files safe while a sync rewrites them (auto-schema Phase 11).

A sync runs many phases, and each writes its own file into ``databases/<db_id>/schema/`` (each file is already replaced
atomically, one at a time). What was missing is protection for the SET: if a sync died after the relationships file was
rewritten and before the join paths were, the folder held half of the new generation and half of the old one, and nothing noticed.

``SyncGuard`` closes that without moving where any phase reads or writes:

1. **One sync per database.** An exclusive OS lock on ``databases/<db_id>/.sync/lock``: a second sync (any process) is refused,
   and a crashed process releases its lock by dying.
2. **Snapshot.** Before the phases run, the generated-file paths that exist are copied to ``.sync/pending/`` and a marker
   ``.sync/in_progress.json`` is written (after the copy, so a marker always means a complete snapshot).
3. **Commit or roll back.** When the sync is good, the snapshot is dropped. When it raises, or validation finds a file this
   sync wrote that is broken, the files are put back from the snapshot.
4. **Crash recovery.** If the marker is still there when the next sync takes the lock, the previous run died: the snapshot is
   put back first.

Only files that carry this project's ``generated_by`` marker are ever restored or deleted. A file a person replaced, or the
mixed curated glossary, is never touched.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import portalocker

from src.sql import registry
from src.sql.knowledge.loaders import validate_db_id

logger = logging.getLogger(__name__)

SYNC_DIR = ".sync"
LOCK_FILE = "lock"
MARKER_FILE = "in_progress.json"
PENDING_DIR = "pending"
MANIFEST_FILE = "manifest.json"

# relative path -> the ``generated_by`` values that mean "this sync's phases wrote it". The curated-and-generated mix
# ``semantics/column_glossary.json`` is deliberately absent: a rollback must never discard a person's edits.
GENERATED_FILES: dict[str, frozenset[str]] = {
    "schema/schema.json": frozenset({"antarkosh-auto-catalog"}),
    "schema/table_inventory.json": frozenset({"antarkosh-auto-catalog"}),
    "schema/procedures.json": frozenset({"auto_procedures"}),
    "schema/sp_evidence.json": frozenset({"auto_sp_mining"}),
    "schema/column_hints.json": frozenset({"auto_sp_mining"}),
    "schema/inferred_relationships.json": frozenset({"auto_relationships"}),
    "schema/relationships.json": frozenset({"auto_relationships"}),
    "schema/table_profiles.json": frozenset({"auto_table_profiles"}),
    "schema/join_paths.json": frozenset({"auto_join_paths"}),
    "semantics/column_dictionary.json": frozenset({"auto_column_meaning"}),
}

PINNED_DB_IDS = frozenset({"erp_main"})


class SyncBusy(RuntimeError):
    """Another sync holds the lock for this database."""


def is_generated(rel: str, data: bytes | None) -> bool:
    """True when ``data`` is a JSON file this project's phases wrote for ``rel``."""
    if data is None:
        return False
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("generated_by") in GENERATED_FILES.get(rel, frozenset())


def _parses(data: bytes | None) -> bool:
    if data is None:
        return False
    try:
        json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SyncGuard:
    def __init__(self, db_id: str, databases_dir: Path | None = None) -> None:
        validate_db_id(db_id)
        self.db_id = db_id
        self.root = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
        self.dir = self.root / SYNC_DIR
        self._lock: portalocker.Lock | None = None
        self._began = False

    # ------------------------------------------------------------------ availability

    @property
    def active(self) -> bool:
        """The guard protects an existing, non-pinned database folder; for anything else it does nothing."""
        return self.db_id not in PINNED_DB_IDS and self.root.is_dir()

    # ------------------------------------------------------------------ lock

    def acquire(self) -> None:
        if not self.active:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(str(self.dir / LOCK_FILE), mode="a", timeout=0, fail_when_locked=True, flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException as exc:
            raise SyncBusy(f"a schema sync for {self.db_id} is already running") from exc
        self._lock = lock

    def release(self) -> None:
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception as exc:  # noqa: BLE001 - releasing must never raise out of a finally block
                logger.debug("Sync lock for %s was already released: %s", self.db_id, exc)
            self._lock = None

    # ------------------------------------------------------------------ snapshot

    def interrupted(self) -> bool:
        return (self.dir / MARKER_FILE).is_file()

    def begin(self) -> None:
        """Copy the generated files that exist now, then write the marker (last, so a marker means a complete snapshot)."""
        if not self.active:
            return
        pending = self.dir / PENDING_DIR
        shutil.rmtree(pending, ignore_errors=True)
        manifest: dict[str, Any] = {"created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": {}}
        for rel in GENERATED_FILES:
            data = _read(self.root / rel)
            if data is not None:
                _write_atomic(pending / rel, data)  # every existing file, so a placeholder a phase replaces can be put back too
            manifest["files"][rel] = {"existed": data is not None, "ours": is_generated(rel, data), "sha256": _digest(data)}
        _write_atomic(pending / MANIFEST_FILE, json.dumps(manifest, indent=2).encode("utf-8"))
        _write_atomic(self.dir / MARKER_FILE, json.dumps({"started_at": manifest["created_at"], "pid": os.getpid()}).encode("utf-8"))
        self._began = True

    def _manifest(self) -> dict[str, Any] | None:
        raw = _read(self.dir / PENDING_DIR / MANIFEST_FILE)
        try:
            doc = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("files"), dict) else None

    def changed_files(self) -> set[str]:
        """Generated files whose bytes differ from the snapshot: what THIS sync wrote or created."""
        manifest = self._manifest()
        if manifest is None:
            return set()
        changed = set()
        for rel in GENERATED_FILES:
            before = (manifest["files"].get(rel) or {}).get("sha256")
            if _digest(_read(self.root / rel)) != before:
                changed.add(rel)
        return changed

    # ------------------------------------------------------------------ end of the sync

    def _clear(self) -> None:
        try:
            (self.dir / MARKER_FILE).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove the sync marker for %s: %s", self.db_id, exc)
        shutil.rmtree(self.dir / PENDING_DIR, ignore_errors=True)
        self._began = False

    def commit(self) -> None:
        if self.active:
            self._clear()

    def rollback(self) -> dict[str, Any]:
        """Put the snapshot back. Returns ``{"restored": [...], "removed": [...], "kept": [...]}``; ``kept`` are files a person changed."""
        result: dict[str, Any] = {"restored": [], "removed": [], "kept": []}
        if not self.active:
            return result
        manifest = self._manifest()
        if manifest is not None:
            for rel in GENERATED_FILES:
                entry = manifest["files"].get(rel) or {}
                live_path = self.root / rel
                live = _read(live_path)
                if _digest(live) == entry.get("sha256"):
                    continue  # this sync did not change it
                if live is not None and _parses(live) and not is_generated(rel, live):
                    result["kept"].append(rel)  # valid JSON without our marker: a person replaced it during the sync, not ours to undo
                    continue  # (bytes that are not JSON at all are a broken write, never a person's file, and are undone)
                if entry.get("existed"):
                    saved = _read(self.dir / PENDING_DIR / rel)
                    if saved is None:
                        result["kept"].append(rel)
                        continue
                    _write_atomic(live_path, saved)
                    result["restored"].append(rel)
                elif live is not None:
                    live_path.unlink(missing_ok=True)  # created by this sync; there was nothing before
                    result["removed"].append(rel)
        self._clear()
        return result

    def recover(self) -> dict[str, Any]:
        """A previous run died: put its snapshot back and clear the marker."""
        logger.warning("Schema sync for %s was interrupted; restoring the last good files", self.db_id)
        return self.rollback()


__all__ = ["GENERATED_FILES", "SyncBusy", "SyncGuard", "is_generated"]
