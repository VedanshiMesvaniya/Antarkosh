"""Run Sync Schema under the guard, validate what it wrote, refresh caches and report (auto-schema Phase 11).

``run_orchestrated`` wraps the existing sync body; with the ``auto_sync_orchestration`` flag off, ``sync_live_schema`` calls the
body directly and none of this runs. The body itself is unchanged: same phases, same order, same files in the same places.

    take the lock -> recover an interrupted run -> snapshot -> body -> validate -> commit or roll back
        -> clear every cache -> build the report -> record it in db.yaml

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from src.sql.sync.drift import diff_tables, load_state, save_state
from src.sql.sync.guard import SyncBusy, SyncGuard
from src.sql.sync.report import PHASES, build_sync_report, phase_status, record_sync_report
from src.sql.sync.resync import file_digests
from src.sql.sync.validate import validate_generation
from src.sql.sync.versions import save_version

logger = logging.getLogger(__name__)


def orchestration_enabled(db_id: str) -> bool:
    try:
        from src.utils.feature_flags import is_db_feature_enabled

        return bool(is_db_feature_enabled("auto_sync_orchestration", db_id))
    except Exception:  # noqa: BLE001 - on any doubt the sync runs exactly as it always did
        return False


def refresh_caches(db_id: str) -> list[str]:
    """Drop everything that was computed from the old files or the old schema. Returns what was cleared (never raises)."""
    cleared: list[str] = []

    def attempt(name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            cleared.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cache %s not cleared for %s: %s", name, db_id, exc)

    def knowledge() -> None:
        from src.sql.pipeline import clear_knowledge_caches

        clear_knowledge_caches(db_id)

    def schema() -> None:
        from src.sql.pipeline import SQLRetriever

        SQLRetriever.clear_schema_cache(db_id=db_id)

    def results() -> None:
        from src.sql.pipeline import SQLRetriever

        SQLRetriever.clear_result_cache(db_id)

    def semantic() -> None:
        from src.sql.semantic_cache import get_semantic_cache

        get_semantic_cache().clear(db_id)

    def routes() -> None:
        from src.sql.paths.route_index import clear_route_index_cache

        clear_route_index_cache(db_id)

    for name, fn in (("knowledge", knowledge), ("schema", schema), ("results", results), ("semantic", semantic), ("routes", routes)):
        attempt(name, fn)
    return cleared


def persist_generation(
    result: dict[str, Any], sync_info: dict[str, Any], validation: Any, db_id: str, *, committed: bool
) -> dict[str, Any]:
    """Phase 13: work out the drift, keep the record of this sync and a version of its files. Never raises.

    Returns ``{"drift": ..., "version": ...}`` (keys only when there is something to say). Only a committed sync is recorded:
    after a rollback the old record stays, because the files are the old ones.
    """
    out: dict[str, Any] = {}
    try:
        fps = sync_info.get("table_fingerprints")
        if not fps:
            return out
        old = load_state(db_id)
        drift = diff_tables(old["tables"] if old else None, fps).as_dict()
        if sync_info.get("schema_complete") is False:
            drift["partial"] = True
        out["drift"] = drift
        if not committed:
            return out

        statuses = {name: phase_status(result.get(key))[0] for name, key in PHASES}
        statuses.update((sync_info.get("resync") or {}).get("carried") or {})  # a phase skipped as unchanged keeps its last result
        errors = bool(getattr(validation, "errors", None))
        clean = bool(sync_info.get("schema_complete", True)) and not errors and "failed" not in statuses.values()
        saved = save_state(
            db_id,
            {
                "schema_hash": sync_info.get("schema_hash"),
                "structure_hash": sync_info.get("structure_hash"),
                "tables": fps,
                "files": file_digests(db_id),
                "phases": statuses,
                "enabled": sync_info.get("enabled") or [],
                "phase_versions": sync_info.get("phase_versions") or {},
                "clean": clean,
            },
        )
        if saved:  # a committed sync's files are what is live now, so they are worth being able to return to, clean or not
            version = save_version(
                db_id,
                schema_hash=sync_info.get("schema_hash"),
                structure=sync_info.get("structure_hash"),
                tables=len(fps),
                note="after a committed sync",
            )
            if version:
                out["version"] = version
    except Exception as exc:  # noqa: BLE001 - keeping records must never fail a sync
        logger.warning("Could not keep the sync record for %s: %s", db_id, exc)
    return out


async def run_orchestrated(body: Callable[[], Awaitable[dict[str, Any]]], db_id: str) -> dict[str, Any]:
    started = time.monotonic()
    guard = SyncGuard(db_id)
    try:
        guard.acquire()
    except SyncBusy as exc:
        return {"status": "error", "db_id": db_id, "message": str(exc)}

    guard_info: dict[str, Any] = {}
    try:
        if guard.active and guard.interrupted():
            recovered = guard.recover()
            guard_info["recovered_previous"] = True
            guard_info["recovered_files"] = recovered.get("restored", []) + recovered.get("removed", [])
        guard.begin()
        try:
            result = await body()
        except BaseException:
            guard.rollback()
            refresh_caches(db_id)
            raise

        sync_info = result.pop("_sync", {}) if isinstance(result, dict) else {}
        if not isinstance(result, dict) or result.get("status") != "ok":
            guard.rollback()  # nothing finished: leave the files as they were
            refresh_caches(db_id)
            return result

        changed = guard.changed_files() if guard.active else set()
        validation = validate_generation(db_id, changed=changed) if guard.active else None
        guard_info["changed_files"] = sorted(changed)
        rolled_back = False
        if validation is not None and validation.errors:
            outcome = guard.rollback()
            rolled_back = True
            guard_info["rolled_back"] = True
            guard_info["rollback"] = outcome
            logger.warning("Schema sync for %s was rolled back: %s", db_id, "; ".join(validation.errors[:3]))
        else:
            guard.commit()
        guard_info["snapshot"] = guard.active
        guard_info["caches_cleared"] = refresh_caches(db_id)
        sync_info.update(persist_generation(result, sync_info, validation, db_id, committed=not rolled_back))

        report = build_sync_report(
            result, db_id=db_id, duration_s=time.monotonic() - started, validation=validation, guard=guard_info, sync_info=sync_info
        )
        record_sync_report(db_id, report, committed=not rolled_back, schema_hash=sync_info.get("schema_hash"))
        result["orchestration"] = report
        return result
    finally:
        guard.release()


__all__ = ["orchestration_enabled", "refresh_caches", "run_orchestrated"]
