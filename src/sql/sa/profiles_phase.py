"""The table-profiles phase of a schema sync (auto-schema Phase 7).

Reads the files earlier phases already wrote (``schema.json``, ``table_inventory.json``, ``sp_evidence.json`` and, when
it is fresh, ``inferred_relationships.json``), works out which tables are twins and which of them holds the real data,
and writes ``schema/table_profiles.json``.

Safety: it never connects to the database and never reads data rows. The work is CPU-bound, so it runs in a worker
thread under a time budget; on a timeout the thread is told to stop before it writes anything. Every failure becomes a
report, so this can never fail a sync, and ``erp_main`` is never touched. The report holds counts only.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.profiles.authority import build_profiles, check_complete, summary
from src.sql.profiles.writer import INFERRED_REL, write_profiles_file
from src.sql.relationships.context import prepare
from src.sql.relationships.model import EVIDENCE_FILE, load_graph

logger = logging.getLogger(__name__)

PROFILES_BUDGET_SECONDS = 20.0


class _Cancelled(Exception):
    """The time budget ran out; stop before the next stage and write nothing."""


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise _Cancelled


def _read_json(path: Path) -> dict[str, Any] | None:
    import json

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def build_table_profiles(db_id: str, *, databases_dir: Path | None, cancel: threading.Event) -> dict[str, Any]:
    """Synchronous and CPU-bound. Call it through ``asyncio.to_thread``."""
    try:
        return _build(db_id, databases_dir, cancel)
    except _Cancelled:
        return {"ok": False, "skipped": "cancelled by the time budget; nothing was written"}


def _build(db_id: str, databases_dir: Path | None, cancel: threading.Event) -> dict[str, Any]:
    graph = load_graph(db_id, databases_dir)
    if graph is None:
        return {"ok": True, "skipped": "no schema.json to build profiles from"}
    _check(cancel)
    base = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    evidence_doc = _read_json(base / EVIDENCE_FILE)
    inferred_doc = _read_json(base / INFERRED_REL)
    ctx = prepare(graph)
    _check(cancel)
    result = build_profiles(ctx, evidence_doc=evidence_doc, inferred_doc=inferred_doc)
    problems = check_complete(graph, result)
    if problems:  # the never-hide gate: a profile set that is not complete is never written
        return {"ok": False, "error": "completeness check failed: " + "; ".join(problems)}
    _check(cancel)  # last chance to stop: past this line the file is written
    inferred_key = inferred_doc.get("input_key") if result.inferred_used and inferred_doc else None
    files = write_profiles_file(db_id, graph, result, inferred_key=inferred_key, databases_dir=databases_dir)
    return {"ok": True, **summary(result), "warnings": len(graph.warnings), "file": files}


async def run_profiles_phase(
    *,
    db_id: str,
    databases_dir: Path | None = None,
    budget_seconds: float = PROFILES_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Build and record the table profiles for ``db_id``. Returns a counts-only report."""
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files
        return {"ok": True, "skipped": "pinned database"}
    cancel = threading.Event()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(build_table_profiles, db_id, databases_dir=databases_dir, cancel=cancel),
            timeout=budget_seconds,
        )
    except TimeoutError:
        cancel.set()  # the worker stops at its next stage boundary, before it can write
        return {"ok": False, "skipped": f"time budget of {budget_seconds:g}s exceeded; nothing was written"}


__all__ = ["PROFILES_BUDGET_SECONDS", "build_table_profiles", "run_profiles_phase"]
