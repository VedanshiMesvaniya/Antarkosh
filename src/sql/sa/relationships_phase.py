"""The relationship-inference phase of a schema sync (auto-schema Phase 6, step 6f).

Reads the files earlier phases already wrote (``schema.json``, ``table_inventory.json``, ``sp_evidence.json``),
infers relationships between tables from names, keys and procedure evidence, and writes
``schema/inferred_relationships.json`` (and ``schema/relationships.json`` when ``apply`` is set).

Safety: it never connects to the database and never reads data rows. The work is CPU-bound, so it runs in a worker
thread under a time budget. On a timeout the thread is told to stop before it writes anything. Every failure becomes
a report, so this can never fail a sync, and ``erp_main`` is never touched. The report holds counts only.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.relationships.context import prepare
from src.sql.relationships.detectors import detect
from src.sql.relationships.gates import resolve
from src.sql.relationships.model import load_graph
from src.sql.relationships.writer import (
    HIGH_THRESHOLD,
    MAX_EDGES_PER_TABLE,
    band,
    write_relationship_files,
)

logger = logging.getLogger(__name__)

RELATIONSHIPS_BUDGET_SECONDS = 30.0


class _Cancelled(Exception):
    """The time budget ran out; stop before the next stage and write nothing."""


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise _Cancelled


def infer_relationships(
    db_id: str,
    *,
    apply: bool,
    databases_dir: Path | None,
    threshold: float,
    max_per_table: int,
    cancel: threading.Event,
) -> dict[str, Any]:
    """Synchronous and CPU-bound. Call it through ``asyncio.to_thread``."""
    try:
        return _infer(db_id, apply, databases_dir, threshold, max_per_table, cancel)
    except _Cancelled:
        return {"ok": False, "skipped": "cancelled by the time budget; nothing was written"}


def _infer(db_id: str, apply: bool, databases_dir: Path | None, threshold: float, max_per_table: int, cancel: threading.Event) -> dict[str, Any]:
    graph = load_graph(db_id, databases_dir)
    if graph is None:
        return {"ok": True, "skipped": "no schema.json to infer relationships from"}
    _check(cancel)
    ctx = prepare(graph)
    _check(cancel)
    candidates = detect(ctx)
    _check(cancel)
    edges, gates = resolve(ctx, candidates)
    _check(cancel)  # last chance to stop: past this line the files are written
    files = write_relationship_files(
        db_id, graph, edges, gates.as_counts(), apply=apply, threshold=threshold, max_per_table=max_per_table, databases_dir=databases_dir
    )
    bands = [band(e.score, threshold) for e in edges]
    return {
        "ok": True,
        "mode": "apply" if apply else "review",
        "tables": len(ctx.tables),
        "candidates": len(candidates),
        "edges": len(edges),
        "high": bands.count("high"),
        "medium": bands.count("medium"),
        "low": bands.count("low"),
        "direction_unknown": gates.direction_unknown,
        "ambiguous": gates.ambiguous,
        "dropped": dict(sorted(gates.dropped.items())),
        "applied_edges": files.pop("applied_edges"),
        "held_back": files.pop("held_back"),
        "warnings": len(graph.warnings),
        "files": files,
    }


async def run_relationships_phase(
    *,
    db_id: str,
    apply: bool = False,
    databases_dir: Path | None = None,
    budget_seconds: float = RELATIONSHIPS_BUDGET_SECONDS,
    threshold: float = HIGH_THRESHOLD,
    max_per_table: int = MAX_EDGES_PER_TABLE,
) -> dict[str, Any]:
    """Infer relationships for ``db_id`` and record them. Returns a counts-only report."""
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files
        return {"ok": True, "skipped": "pinned database"}
    cancel = threading.Event()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                infer_relationships, db_id, apply=apply, databases_dir=databases_dir, threshold=threshold,
                max_per_table=max_per_table, cancel=cancel,
            ),
            timeout=budget_seconds,
        )
    except TimeoutError:
        cancel.set()  # the worker stops at its next stage boundary, before it can write
        return {"ok": False, "skipped": f"time budget of {budget_seconds:g}s exceeded; nothing was written"}


__all__ = ["RELATIONSHIPS_BUDGET_SECONDS", "infer_relationships", "run_relationships_phase"]
