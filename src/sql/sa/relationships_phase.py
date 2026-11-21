"""The relationship-inference phase of a schema sync (auto-schema Phase 6, step 6f).

Reads the files earlier phases already wrote (``schema.json``, ``table_inventory.json``, ``sp_evidence.json``),
infers relationships between tables from names, keys and procedure evidence, and writes
``schema/inferred_relationships.json`` (and ``schema/relationships.json`` when ``apply`` is set).

Safety: by default it never connects to the database and never reads data rows. The work is CPU-bound, so it runs in a worker
thread under a time budget. On a timeout the thread is told to stop before it writes anything. Every failure becomes
a report, so this can never fail a sync, and ``erp_main`` is never touched. The report holds counts only.

Release 2 (``validate=True``, per-database flag ``auto_relationships_data_apply``, off by default): between inferring and writing,
a short list of likely joins is checked against the connected database with aggregate counts only (see
``relationships/data_evidence.py``). The checks have their own budgets and can only add evidence to, or hold back, a soft edge;
when they fail or are switched off, the edges are exactly what the steps above produced.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.relationships.candidates import Candidate, Edge
from src.sql.relationships.context import Context, prepare
from src.sql.relationships.detectors import detect
from src.sql.relationships.gates import GateReport, resolve
from src.sql.relationships.model import CatalogGraph, load_graph
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


@dataclass
class _Resolved:
    """What steps 6b to 6d produced: everything the data checks and the writer need."""

    graph: CatalogGraph
    ctx: Context
    candidates: list[Candidate]
    edges: list[Edge]
    gates: GateReport


def _resolve_stage(db_id: str, databases_dir: Path | None, cancel: threading.Event) -> _Resolved | dict[str, Any]:
    """Steps 6b to 6d. Returns a finished report instead when there is nothing to infer from."""
    graph = load_graph(db_id, databases_dir)
    if graph is None:
        return {"ok": True, "skipped": "no schema.json to infer relationships from"}
    _check(cancel)
    ctx = prepare(graph)
    _check(cancel)
    candidates = detect(ctx)
    _check(cancel)
    edges, gates = resolve(ctx, candidates)
    return _Resolved(graph, ctx, candidates, edges, gates)


def _write_stage(
    db_id: str, r: _Resolved, apply: bool, databases_dir: Path | None, threshold: float, max_per_table: int, cancel: threading.Event,
    evidence_key: str = "",
) -> dict[str, Any]:
    """Steps 6e: score, write and report."""
    _check(cancel)  # last chance to stop: past this line the files are written
    extra: dict[str, Any] = {"evidence_key": evidence_key} if evidence_key else {}  # only passed when there is something to say
    files = write_relationship_files(
        db_id, r.graph, r.edges, r.gates.as_counts(), apply=apply, threshold=threshold, max_per_table=max_per_table,
        databases_dir=databases_dir, **extra,
    )
    bands = [band(e.score, threshold) for e in r.edges]
    return {
        "ok": True,
        "mode": "apply" if apply else "review",
        "tables": len(r.ctx.tables),
        "candidates": len(r.candidates),
        "edges": len(r.edges),
        "high": bands.count("high"),
        "medium": bands.count("medium"),
        "low": bands.count("low"),
        "direction_unknown": r.gates.direction_unknown,
        "ambiguous": r.gates.ambiguous,
        "dropped": dict(sorted(r.gates.dropped.items())),
        "applied_edges": files.pop("applied_edges"),
        "held_back": files.pop("held_back"),
        "warnings": len(r.graph.warnings),
        "files": files,
    }


def _infer(db_id: str, apply: bool, databases_dir: Path | None, threshold: float, max_per_table: int, cancel: threading.Event) -> dict[str, Any]:
    resolved = _resolve_stage(db_id, databases_dir, cancel)
    if isinstance(resolved, dict):
        return resolved
    return _write_stage(db_id, resolved, apply, databases_dir, threshold, max_per_table, cancel)


async def run_relationships_phase(
    *,
    db_id: str,
    apply: bool = False,
    databases_dir: Path | None = None,
    budget_seconds: float = RELATIONSHIPS_BUDGET_SECONDS,
    threshold: float = HIGH_THRESHOLD,
    max_per_table: int = MAX_EDGES_PER_TABLE,
    validate: bool = False,
    runner: Any = None,
) -> dict[str, Any]:
    """Infer relationships for ``db_id`` and record them. Returns a counts-only report.

    ``validate`` (Release 2) checks a short list of likely joins against the connected database first, with aggregate counts only.
    ``runner`` replaces the read-only query function (tests).
    """
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files
        return {"ok": True, "skipped": "pinned database"}
    cancel = threading.Event()
    if validate:
        return await _run_validated(db_id, apply, databases_dir, budget_seconds, threshold, max_per_table, cancel, runner)
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


async def _run_validated(
    db_id: str, apply: bool, databases_dir: Path | None, budget_seconds: float, threshold: float, max_per_table: int,
    cancel: threading.Event, runner: Any,
) -> dict[str, Any]:
    """Infer, check the short list against the database, then write. Each CPU stage has the time budget; the checks have their own."""
    timed_out = {"ok": False, "skipped": f"time budget of {budget_seconds:g}s exceeded; nothing was written"}
    try:
        resolved = await asyncio.wait_for(asyncio.to_thread(_resolve_stage, db_id, databases_dir, cancel), timeout=budget_seconds)
    except TimeoutError:
        cancel.set()
        return timed_out
    if isinstance(resolved, dict):
        return resolved
    validation: dict[str, Any]
    evidence_key = ""
    try:
        from src.sql.relationships.data_evidence import collect_evidence

        outcome = await collect_evidence(db_id, resolved.graph, resolved.edges, runner=runner, databases_dir=databases_dir, threshold=threshold)
        validation, evidence_key = outcome.report, outcome.key
    except Exception as exc:  # noqa: BLE001 - the edges stay exactly as inferred without data
        logger.warning("Relationship data checks failed for %s: %s", db_id, type(exc).__name__)
        validation = {"ok": False, "error": type(exc).__name__}
    try:
        report = await asyncio.wait_for(
            asyncio.to_thread(_write_stage, db_id, resolved, apply, databases_dir, threshold, max_per_table, cancel, evidence_key),
            timeout=budget_seconds,
        )
    except TimeoutError:
        cancel.set()
        return timed_out
    report["validation"] = validation
    return report


__all__ = ["RELATIONSHIPS_BUDGET_SECONDS", "infer_relationships", "run_relationships_phase"]
