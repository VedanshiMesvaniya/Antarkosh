"""Keep the last few good generations of the generated files, and roll back to one (auto-schema Phase 13, part 3).

The guard (Phase 11) protects one sync: if it breaks, the files go back. Once a sync is committed the old files were gone, so a
sync that was technically fine but made answers worse could not be undone. This keeps a copy of the generated files after
every committed sync that changed something, in ``databases/<db_id>/.sync/versions/v0001/``, ``v0002/`` ... The newest
``KEEP_VERSIONS`` are kept; older ones are removed.

Rolling back to a version:

* is refused while a sync runs for that database, or if the version's files fail their checksums;
* first keeps the current files as a version of their own, so the rollback itself can be undone;
* restores only files that carry this project's ``generated_by`` marker, and never overwrites a valid file a person put in
  its place (reported as ``kept``); removes generated files that did not exist in that version;
* leaves the vector store alone (it is rebuilt from the schema by the next sync) and marks the sync record as untrusted, so
  the next sync runs every phase instead of believing nothing changed;
* clears every cache.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.sync.drift import db_root, invalidate_state, load_state
from src.sql.sync.guard import (
    GENERATED_FILES,
    PINNED_DB_IDS,
    SYNC_DIR,
    SyncBusy,
    SyncGuard,
    _digest,
    _parses,
    _read,
    _write_atomic,
    is_generated,
)

logger = logging.getLogger(__name__)

VERSIONS_DIR = "versions"
VERSION_FILE = "version.json"
FILES_DIR = "files"
KEEP_VERSIONS = 5
_ID = re.compile(r"^v\d{4}$")


class VersionError(RuntimeError):
    """A version cannot be used (unknown id, checksum mismatch, sync running, ...)."""


def _vroot(db_id: str, databases_dir: Path | None) -> Path:
    return db_root(db_id, databases_dir) / SYNC_DIR / VERSIONS_DIR


def _read_manifest(vdir: Path) -> dict[str, Any] | None:
    raw = _read(vdir / VERSION_FILE)
    try:
        doc = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("files"), dict) and _ID.match(str(doc.get("id", ""))) else None


def list_versions(db_id: str, databases_dir: Path | None = None) -> list[dict[str, Any]]:
    """Complete versions, newest first (a version whose ``version.json`` is missing was interrupted and is ignored)."""
    out: list[dict[str, Any]] = []
    try:
        root = _vroot(db_id, databases_dir)
        for vdir in sorted((p for p in root.iterdir() if p.is_dir() and _ID.match(p.name)), reverse=True):
            manifest = _read_manifest(vdir)
            if manifest is not None:
                out.append({k: v for k, v in manifest.items() if k != "files"} | {"files": sorted(manifest["files"])})
    except OSError:
        pass
    return out


def _all_ids(root: Path) -> list[str]:
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and _ID.match(p.name))
    except OSError:
        return []


def _current_ours(db_id: str, databases_dir: Path | None) -> dict[str, bytes]:
    root = db_root(db_id, databases_dir)
    files: dict[str, bytes] = {}
    for rel in GENERATED_FILES:
        data = _read(root / rel)
        if data is not None and is_generated(rel, data):
            files[rel] = data
    return files


def save_version(
    db_id: str,
    *,
    schema_hash: str | None,
    structure: str | None,
    tables: int | None,
    note: str = "",
    protect: tuple[str, ...] = (),
    databases_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Copy the generated files as they are now. Returns the version, ``{"id": ..., "unchanged": True}`` when the newest
    version already holds exactly these files, or None when there is nothing to keep or it failed (never raises)."""
    try:
        if db_id in PINNED_DB_IDS or not db_root(db_id, databases_dir).is_dir():
            return None
        files = _current_ours(db_id, databases_dir)
        if not files:
            return None
        digests = {rel: _digest(data) for rel, data in files.items()}
        newest = list_versions(db_id, databases_dir)
        if newest:
            last = _read_manifest(_vroot(db_id, databases_dir) / newest[0]["id"])
            if last is not None and {r: e.get("sha256") for r, e in last["files"].items()} == digests and last.get("structure_hash") == structure:
                return {"id": newest[0]["id"], "unchanged": True}
        root = _vroot(db_id, databases_dir)
        ids = _all_ids(root)
        number = (int(ids[-1][1:]) + 1) if ids else 1
        if number > 9999:
            return None
        vid = f"v{number:04d}"
        vdir = root / vid
        for rel, data in files.items():
            _write_atomic(vdir / FILES_DIR / rel, data)
        manifest = {
            "id": vid,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schema_hash": schema_hash,
            "structure_hash": structure,
            "tables": tables,
            "note": note,
            "files": {rel: {"sha256": digests[rel]} for rel in files},
        }
        _write_atomic(vdir / VERSION_FILE, json.dumps(manifest, indent=1).encode("utf-8"))  # last: its presence means "complete"
        _prune(db_id, protect=(vid, *protect), databases_dir=databases_dir)
        return {"id": vid, "created_at": manifest["created_at"], "files": len(files)}
    except Exception as exc:  # noqa: BLE001 - keeping a copy must never fail a sync
        logger.warning("Could not keep a version of the generated files for %s: %s", db_id, exc)
        return None


def _prune(db_id: str, *, protect: tuple[str, ...], databases_dir: Path | None) -> None:
    root = _vroot(db_id, databases_dir)
    ids = _all_ids(root)  # oldest first; includes interrupted ones, which are removed first
    complete = [i for i in ids if _read_manifest(root / i) is not None]
    for vid in ids:
        if vid not in complete and vid not in protect:
            shutil.rmtree(root / vid, ignore_errors=True)
    excess = len(complete) - KEEP_VERSIONS
    for vid in complete:
        if excess <= 0:
            break
        if vid in protect:
            continue
        shutil.rmtree(root / vid, ignore_errors=True)
        excess -= 1


def verify_version(db_id: str, version_id: str, databases_dir: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """``(manifest, problems)``: every file of the version must exist, match its checksum and carry this project's marker."""
    if not _ID.match(version_id or ""):
        raise VersionError(f"{version_id!r} is not a version id (expected something like v0001)")
    vdir = _vroot(db_id, databases_dir) / version_id
    manifest = _read_manifest(vdir)
    if manifest is None:
        raise VersionError(f"version {version_id} does not exist for {db_id} (or was never completed)")
    problems: list[str] = []
    for rel, entry in manifest["files"].items():
        if rel not in GENERATED_FILES:
            problems.append(f"{rel}: not a generated file of this project")
            continue
        data = _read(vdir / FILES_DIR / rel)
        if data is None:
            problems.append(f"{rel}: missing from the version")
        elif _digest(data) != (entry or {}).get("sha256"):
            problems.append(f"{rel}: checksum mismatch")
        elif not is_generated(rel, data):
            problems.append(f"{rel}: lacks this project's generated_by marker")
    return manifest, problems


def _record_rollback(db_id: str, version_id: str, schema_hash: str | None, databases_dir: Path | None) -> None:
    try:
        import yaml

        path = db_root(db_id, databases_dir) / "db.yaml"
        if not path.is_file():
            return
        with open(path, encoding="utf-8") as fh:
            meta = yaml.safe_load(fh) or {}
        if not isinstance(meta, dict):
            return
        sync_meta = meta.get("schema_sync") if isinstance(meta.get("schema_sync"), dict) else {}
        sync_meta["schema_hash"] = schema_hash  # the files describe that version's schema: the next sync must not think nothing changed
        sync_meta["rolled_back_to"] = {"version": version_id, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        meta["schema_sync"] = sync_meta
        registry.atomic_write_yaml(path, meta)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record the rollback in db.yaml for %s: %s", db_id, exc)


def rollback_to_version(db_id: str, version_id: str, *, databases_dir: Path | None = None) -> dict[str, Any]:
    """Put the generated files back as they were in ``version_id``. Raises :class:`VersionError` when it must not be done."""
    if db_id in PINNED_DB_IDS:
        raise VersionError(f"{db_id} uses curated files and has no versions")
    manifest, problems = verify_version(db_id, version_id, databases_dir)
    if problems:
        raise VersionError(f"version {version_id} is damaged, nothing was changed: " + "; ".join(problems[:5]))
    guard = SyncGuard(db_id, databases_dir)
    if not guard.active:
        raise VersionError(f"no database folder for {db_id}")
    try:
        guard.acquire()
    except SyncBusy as exc:
        raise VersionError(str(exc)) from exc
    try:
        if guard.interrupted():
            raise VersionError("an earlier sync was interrupted: run a sync (it repairs the files first) before rolling back")

        state = load_state(db_id, databases_dir) or {}
        before = save_version(
            db_id,
            schema_hash=None,
            structure=state.get("structure_hash"),
            tables=len(state.get("tables") or {}) or None,
            note=f"kept before rolling back to {version_id}",
            protect=(version_id,),
            databases_dir=databases_dir,
        )

        root = db_root(db_id, databases_dir)
        vdir = _vroot(db_id, databases_dir) / version_id
        result: dict[str, Any] = {"version": version_id, "restored": [], "removed": [], "kept": []}
        for rel in GENERATED_FILES:
            live_path = root / rel
            live = _read(live_path)
            if rel in manifest["files"]:
                saved = _read(vdir / FILES_DIR / rel)
                if saved is None:
                    continue
                if live is not None and live != saved and _parses(live) and not is_generated(rel, live):
                    result["kept"].append(rel)  # a valid file a person put there: not ours to replace
                    continue
                if live != saved:
                    _write_atomic(live_path, saved)
                    result["restored"].append(rel)
            elif live is not None and is_generated(rel, live):
                live_path.unlink(missing_ok=True)  # generated after that version: it would not match the restored set
                result["removed"].append(rel)
        result["kept_current_as"] = (before or {}).get("id")
        invalidate_state(db_id, f"rolled back to {version_id}", databases_dir)
        _record_rollback(db_id, version_id, manifest.get("schema_hash"), databases_dir)
    finally:
        guard.release()

    try:
        from src.sql.sync.orchestrate import refresh_caches

        result["caches_cleared"] = refresh_caches(db_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Caches were not cleared after rolling %s back: %s", db_id, exc)
    result["note"] = "The vector store is not changed; the next sync rebuilds it and runs every phase."
    return result


__all__ = ["KEEP_VERSIONS", "VersionError", "list_versions", "rollback_to_version", "save_version", "verify_version"]
