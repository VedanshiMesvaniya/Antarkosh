"""Remove the vector chunks of tables that no longer exist (auto-schema Phase 13, part 4).

A sync embeds one chunk per table, with a stable id, and upserts them. It never removed a chunk, so a table that was dropped or
renamed kept its old chunk in the vector store, and schema retrieval could still hand that table's definition to the model.

What counts as "no longer exists" is strict: a table that was in the last GOOD sync's record and is absent from this sync's
complete read of the live schema. Nothing is ever guessed from the store's contents. A sync is refused the removal when:

* the schema read may have been cut short (it hit the row limit),
* there is no earlier record to compare with,
* or more than half of the earlier tables (and more than 5) vanished at once, which looks like a failed read, not a drop.

Shadow mode (the default) only reports what would be removed; ``STALE_CHUNKS_APPLY_DBS`` turns the removal on per database.
Never raises: a failure here never fails a sync.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from typing import Any

from src.sql.sync.drift import diff_tables, load_state
from src.sql.sync.modes import stale_chunks_mode

logger = logging.getLogger(__name__)

MIN_ALLOWED_REMOVALS = 5  # a drop of up to this many tables is always plausible
MAX_REMOVED_SHARE = 0.5


def chunk_id_for(db_id: str, table_name: str) -> str:
    """The id the sync gives a table's chunk (kept in step with ``_sync_live_schema_body``)."""
    return f"{db_id}_schema_{table_name}"


async def cleanup_stale_chunks(
    db_id: str,
    store: Any,
    document_id: str,
    fingerprints: dict[str, str],
    *,
    schema_complete: bool,
) -> dict[str, Any] | None:
    """``None`` when Phase 13 is off; otherwise a small report of what was (or would be) removed."""
    mode = stale_chunks_mode(db_id)
    if not mode:
        return None
    try:
        state = load_state(db_id)
        if state is None:
            return {"mode": mode, "removed": [], "note": "no earlier sync is on record to compare with"}
        gone = list(diff_tables(state["tables"], fingerprints).removed)
        if not gone:
            return {"mode": mode, "removed": []}
        if not schema_complete:
            return {"mode": mode, "removed": [], "refused": "the schema read may have been cut short", "candidates": gone[:20]}
        earlier = len(state["tables"])
        if len(gone) > MIN_ALLOWED_REMOVALS and len(gone) > earlier * MAX_REMOVED_SHARE:
            return {"mode": mode, "removed": [], "refused": f"{len(gone)} of {earlier} tables vanished at once: looks like a failed read", "candidates": gone[:20]}
        ids = [chunk_id_for(db_id, name) for name in gone]
        if mode != "apply":
            return {"mode": mode, "removed": [], "would_remove": gone[:20], "count": len(gone)}
        delete = getattr(store, "delete_chunks", None)
        if delete is None:
            return {"mode": mode, "removed": [], "refused": "this vector store cannot delete single chunks", "candidates": gone[:20]}
        await delete(document_id, ids)
        return {"mode": mode, "removed": gone[:20], "count": len(gone)}
    except Exception as exc:  # noqa: BLE001 - a leftover chunk is harmless compared with a failed sync
        logger.warning("Removing stale schema chunks failed for %s: %s", db_id, exc)
        return {"mode": mode, "removed": [], "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


__all__ = ["chunk_id_for", "cleanup_stale_chunks"]
