"""Which Phase 13 behaviours are on for a database (auto-schema Phase 13).

Everything in Phase 13 needs the per-database ``auto_sync_orchestration`` flag (Phase 11). With it on, drift detection, the
kept state and the kept versions run for the database. The two behaviours that change what a sync does are *shadow* until
their own per-database flag is turned on (both start off), so turning orchestration on never changes a sync's work by itself:

* ``auto_resync_apply``       - skip the phases whose inputs did not change since the last good sync.
* ``auto_stale_chunk_cleanup`` - delete the vector chunks of tables that no longer exist.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _base_on(db_id: str) -> bool:
    try:
        from src.utils.feature_flags import is_db_feature_enabled

        return bool(is_db_feature_enabled("auto_sync_orchestration", db_id))
    except Exception:  # noqa: BLE001 - on any doubt Phase 13 is off
        return False


def _mode(apply_flag: str, db_id: str) -> str:
    try:
        if not _base_on(db_id):
            return ""
        from src.utils.feature_flags import is_db_feature_enabled

        return "apply" if is_db_feature_enabled(apply_flag, db_id) else "shadow"
    except Exception as exc:  # noqa: BLE001
        logger.debug("Phase 13 mode unreadable for %s (%s): %s", db_id, apply_flag, exc)
        return ""


def resync_mode(db_id: str) -> str:
    """``"apply"``, ``"shadow"`` or ``""`` (off) for dependency-aware skipping of phases."""
    return _mode("auto_resync_apply", db_id)


def stale_chunks_mode(db_id: str) -> str:
    """``"apply"``, ``"shadow"`` or ``""`` (off) for removing the vector chunks of dropped tables."""
    return _mode("auto_stale_chunk_cleanup", db_id)


__all__ = ["resync_mode", "stale_chunks_mode"]
