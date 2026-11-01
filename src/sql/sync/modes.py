"""Which Phase 13 behaviours are on for a database (auto-schema Phase 13).

Everything in Phase 13 needs the per-database ``auto_sync_orchestration`` flag (Phase 11). With it on, drift detection, the
kept state and the kept versions run for the database. The two behaviours that change what a sync does are *shadow* until a
database is listed, so turning the flag on never changes a sync's work by itself:

* ``RESYNC_APPLY_DBS``       - skip the phases whose inputs did not change since the last good sync.
* ``STALE_CHUNKS_APPLY_DBS`` - delete the vector chunks of tables that no longer exist.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def listed(value: str | None, db_id: str) -> bool:
    """True when ``db_id`` is in the comma-separated ``value`` (case-insensitive) or the list contains ``*``."""
    items = {x.strip().lower() for x in str(value or "").split(",") if x.strip()}
    return "*" in items or db_id.strip().lower() in items


def _base_on(db_id: str) -> bool:
    try:
        from src.utils.feature_flags import is_db_feature_enabled

        return bool(is_db_feature_enabled("auto_sync_orchestration", db_id))
    except Exception:  # noqa: BLE001 - on any doubt Phase 13 is off
        return False


def _mode(setting_name: str, db_id: str) -> str:
    try:
        if not _base_on(db_id):
            return ""
        from src.core.config import settings

        return "apply" if listed(getattr(settings, setting_name, ""), db_id) else "shadow"
    except Exception as exc:  # noqa: BLE001
        logger.debug("Phase 13 mode unreadable for %s (%s): %s", db_id, setting_name, exc)
        return ""


def resync_mode(db_id: str) -> str:
    """``"apply"``, ``"shadow"`` or ``""`` (off) for dependency-aware skipping of phases."""
    return _mode("resync_apply_dbs", db_id)


def stale_chunks_mode(db_id: str) -> str:
    """``"apply"``, ``"shadow"`` or ``""`` (off) for removing the vector chunks of dropped tables."""
    return _mode("stale_chunks_apply_dbs", db_id)


__all__ = ["listed", "resync_mode", "stale_chunks_mode"]
