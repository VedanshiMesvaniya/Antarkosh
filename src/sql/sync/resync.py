"""Re-run only the phases whose inputs changed (auto-schema Phase 13, part 2).

A sync used to run every phase every time. The phases that only read files this project already wrote (relationships,
profiles, join paths) and the catalog reflection can be skipped when nothing they depend on changed since the last good sync.
This decides, phase by phase and just before each one runs, whether it has to.

A phase is skipped only when EVERY one of these holds, otherwise it runs exactly as before:

* the last good sync is on record, was clean, and is not older than ``MAX_STATE_AGE_DAYS`` (so row estimates refresh weekly);
* the structure of the database is the same (every table and foreign key hashes the same);
* the same phases are switched on, with the same rule versions, as at that sync;
* the phase's last result was ``ok`` (or a deliberate skip), not a failure;
* the phase's own output files are byte-for-byte what the last sync left (nobody edited them, nothing replaced them);
* every file it reads that a phase before it writes is unchanged, evaluated at the moment it would run (so a changed
  procedure-evidence file made this sync is seen).

Never skipped: the procedure reading and mining (they read the database, so only running them shows a change) and the
column-meaning phase (it makes its own per-table decisions and may be mid-way through a large database). Skipping is *shadow* until
``auto_resync_apply`` is turned on for the database: the planner then only records what it would have skipped.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql.sync.drift import (
    content_digest,
    db_root,
    load_state,
    phase_versions,
    structure_hash,
    table_fingerprints,
)
from src.sql.sync.guard import GENERATED_FILES, _read
from src.sql.sync.modes import resync_mode

logger = logging.getLogger(__name__)

MAX_STATE_AGE_DAYS = 7
GLOSSARY_REL = "semantics/column_glossary.json"  # curated and generated entries mixed: tracked for changes, never restored

CATALOG_FILES = ("schema/schema.json", "schema/table_inventory.json")
EVIDENCE_FILES = ("schema/procedures.json", "schema/sp_evidence.json", "schema/column_hints.json")
RELATIONSHIP_FILES = ("schema/relationships.json", "schema/inferred_relationships.json")
PROFILE_FILES = ("schema/table_profiles.json",)
PATH_FILES = ("schema/join_paths.json",)

# What each skippable phase writes, and the files written by phases before it that it reads. These are deliberately a superset
# of what each phase really reads: a skip is only wrong if a real input is missing from the list.
OUTPUTS: dict[str, tuple[str, ...]] = {
    "catalog": CATALOG_FILES,
    "relationships": RELATIONSHIP_FILES,
    "table_profiles": PROFILE_FILES,
    "join_paths": PATH_FILES,
}
UPSTREAM: dict[str, tuple[str, ...]] = {
    "catalog": (),
    "relationships": CATALOG_FILES + EVIDENCE_FILES,
    "table_profiles": CATALOG_FILES + EVIDENCE_FILES + RELATIONSHIP_FILES,
    "join_paths": CATALOG_FILES + EVIDENCE_FILES + RELATIONSHIP_FILES + PROFILE_FILES,
}
SKIPPABLE = tuple(OUTPUTS)

# Switch name -> the phase name used in the state file and the report.
_SWITCHES = (
    ("auto_catalog", "catalog"),
    ("auto_sqlalchemy", "sqlalchemy"),
    ("auto_procedures", "procedures"),
    ("auto_sp_mining", "sp_mining"),
    ("auto_relationships", "relationships"),
    ("auto_relationships_apply", "relationships_apply"),
    ("auto_table_profiles", "table_profiles"),
    ("auto_join_paths", "join_paths"),
    ("auto_column_meaning", "column_meaning"),
)

TRACKED_FILES: tuple[str, ...] = (*GENERATED_FILES, GLOSSARY_REL)


def enabled_phases(db_id: str) -> list[str]:
    """The phases switched on for ``db_id`` right now."""
    from src.utils.feature_flags import is_db_feature_enabled

    return sorted(name for flag, name in _SWITCHES if is_db_feature_enabled(flag, db_id))


def file_digests(db_id: str, databases_dir: Path | None = None) -> dict[str, str | None]:
    """Content digest of every tracked file, ignoring the ``generated_at`` stamp (None when the file does not exist)."""
    root = db_root(db_id, databases_dir)
    return {rel: content_digest(_read(root / rel)) for rel in TRACKED_FILES}


class ResyncPlanner:
    def __init__(
        self,
        db_id: str,
        *,
        mode: str,
        state: dict[str, Any] | None,
        structure: str,
        enabled: list[str],
        versions: dict[str, Any],
        databases_dir: Path | None = None,
        now: datetime | None = None,
    ) -> None:
        self.db_id = db_id
        self.mode = mode
        self.state = state
        self.structure = structure
        self.enabled = sorted(enabled)
        self.versions = versions
        self.databases_dir = databases_dir
        self.now = now or datetime.now(UTC)
        self.ran: dict[str, str] = {}  # phase -> why it had to run
        self.skipped: list[str] = []  # phases not run (apply mode)
        self.would_skip: list[str] = []  # phases that could have been skipped (shadow mode)
        self._global_reason = self._check_global()

    # ------------------------------------------------------------------ decisions

    def _check_global(self) -> str | None:
        state = self.state
        if state is None:
            return "no earlier sync is on record"
        if not state.get("clean"):
            return str(state.get("invalid_reason") or "the earlier sync was not clean")
        if state.get("structure_hash") != self.structure:
            return "the database structure changed"
        if sorted(state.get("enabled") or []) != self.enabled:
            return "the switched-on phases changed"
        if state.get("phase_versions") != self.versions:
            return "a phase's rules changed"
        try:
            committed = datetime.fromisoformat(str(state.get("committed_at")))
            if (self.now - committed).days >= MAX_STATE_AGE_DAYS:
                return f"the earlier sync is {MAX_STATE_AGE_DAYS} or more days old"
        except (TypeError, ValueError):
            return "the earlier sync has no valid date"
        return None

    def _reason(self, phase: str) -> str | None:
        """Why ``phase`` has to run, or None when it can be skipped."""
        if self._global_reason:
            return self._global_reason
        state = self.state or {}
        last = (state.get("phases") or {}).get(phase)
        if last not in ("ok", "skipped"):
            return f"its last result was {last or 'unknown'}"
        recorded = state.get("files") or {}
        current = file_digests(self.db_id, self.databases_dir)
        for rel in OUTPUTS[phase]:
            if current.get(rel) != recorded.get(rel):
                return f"{rel} was changed since the last sync"
        for rel in UPSTREAM[phase]:
            if current.get(rel) != recorded.get(rel):
                return f"its input {rel} changed"
        return None

    def should_run(self, phase: str) -> bool:
        """True when ``phase`` must run now. In shadow mode this is always True; the answer is only recorded."""
        try:
            reason = self._reason(phase) if phase in SKIPPABLE else "this phase is never skipped"
        except Exception as exc:  # noqa: BLE001 - on any doubt, run it
            logger.warning("Re-sync plan for %s/%s failed (%s): running the phase", self.db_id, phase, exc)
            reason = "the plan could not be worked out"
        if reason is None:
            if self.mode == "apply":
                self.skipped.append(phase)
                return False
            self.would_skip.append(phase)
            return True
        self.ran[phase] = reason
        return True

    # ------------------------------------------------------------------ reports

    def skipped_report(self, phase: str) -> dict[str, Any]:
        """What the sync records for a phase that was not run: counted as fine, and marked so nothing re-embeds for it."""
        return {"ok": True, "skipped": "unchanged since the last sync", "unchanged": True, "phase": phase}

    def carried_statuses(self) -> dict[str, str]:
        """For each skipped phase, the status to keep in the next record: its last real result."""
        last = (self.state or {}).get("phases") or {}
        return {p: str(last.get(p) or "ok") for p in self.skipped}

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "skipped": list(self.skipped),
            "would_skip": list(self.would_skip),
            "ran": dict(self.ran),
            "carried": self.carried_statuses(),
        }


def start_resync(
    db_id: str,
    table_schemas: dict[str, str],
    fk_rows: list[dict[str, Any]] | None,
    *,
    schema_complete: bool,
    databases_dir: Path | None = None,
) -> tuple[ResyncPlanner | None, dict[str, str]]:
    """``(planner, table fingerprints)`` for this sync. The planner is None when Phase 13 is off or anything goes wrong
    (the sync then runs every phase, as it always did). Never raises."""
    try:
        mode = resync_mode(db_id)
        if not mode:
            return None, {}
        fps = table_fingerprints(table_schemas, fk_rows)
        state = load_state(db_id, databases_dir) if schema_complete else None
        planner = ResyncPlanner(
            db_id,
            mode=mode,
            state=state,
            structure=structure_hash(fps),
            enabled=enabled_phases(db_id),
            versions=phase_versions(),
            databases_dir=databases_dir,
        )
        if not schema_complete:
            planner._global_reason = "the schema read may have been cut short"
        return planner, fps
    except Exception as exc:  # noqa: BLE001
        logger.warning("Re-sync planning is unavailable for %s: %s", db_id, exc)
        return None, {}


def skip_phase(planner: ResyncPlanner | None, phase: str, enabled: bool) -> bool:
    """True when ``phase`` is switched on and the planner says to skip it (apply mode only). Never raises."""
    try:
        return bool(enabled and planner is not None and not planner.should_run(phase))
    except Exception:  # noqa: BLE001 - on any doubt the phase runs
        return False


def phase13_info(
    db_id: str,
    fingerprints: dict[str, str],
    planner: ResyncPlanner | None,
    stale_report: dict[str, Any] | None,
    *,
    schema_complete: bool,
) -> dict[str, Any]:
    """What the sync body hands the orchestrator for Phase 13 (empty when Phase 13 is off)."""
    if not fingerprints:
        return {}
    info: dict[str, Any] = {
        "table_fingerprints": fingerprints,
        "structure_hash": structure_hash(fingerprints),
        "schema_complete": schema_complete,
        "enabled": enabled_phases(db_id),
        "phase_versions": phase_versions(),
    }
    if planner is not None:
        info["resync"] = planner.summary()
    if stale_report:
        info["stale_chunks"] = stale_report
    return info


__all__ = [
    "MAX_STATE_AGE_DAYS",
    "OUTPUTS",
    "SKIPPABLE",
    "TRACKED_FILES",
    "UPSTREAM",
    "ResyncPlanner",
    "enabled_phases",
    "file_digests",
    "phase13_info",
    "skip_phase",
    "start_resync",
]
