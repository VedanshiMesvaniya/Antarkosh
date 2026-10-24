"""The join-paths phase of a schema sync (auto-schema Phase 8).

Reads the files earlier phases already wrote (``schema.json``, ``table_inventory.json``, ``sp_evidence.json``,
``relationships.json`` and, when each is fresh, ``inferred_relationships.json`` and ``table_profiles.json``), searches
for indirect join routes of 2 to 3 joins, and writes ``schema/join_paths.json``.

Safety: it never connects to the database and never reads data rows. The work is CPU-bound, so it runs in a worker
thread under a time budget that is checked between starting tables; on a timeout the thread stops before it writes
anything. Every route is verified against the trusted graph before the file is written. Every failure becomes a report,
so this can never fail a sync, and ``erp_main`` is never touched. The report holds counts only.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.paths.links import (
    EDGE_COUNT_KEYS,
    build_links,
    collect_joins,
    content_hash,
    select_nodes,
)
from src.sql.paths.search import Params, check_routes, find_paths, summary_counts
from src.sql.paths.writer import input_key, paths_document, write_paths_file
from src.sql.profiles.writer import PROFILES_REL
from src.sql.relationships.model import EVIDENCE_FILE, load_graph
from src.sql.relationships.writer import APPLIED_REL, INFERRED_REL

logger = logging.getLogger(__name__)

PATHS_BUDGET_SECONDS = 30.0


class _Cancelled(Exception):
    """The time budget ran out; stop before the next stage and write nothing."""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def build_join_paths(db_id: str, *, databases_dir: Path | None, cancel: threading.Event, params: Params | None = None) -> dict[str, Any]:
    """Synchronous and CPU-bound. Call it through ``asyncio.to_thread``."""
    try:
        return _build(db_id, databases_dir, cancel, params or Params())
    except _Cancelled:
        return {"ok": False, "skipped": "cancelled by the time budget; nothing was written"}


def _build(db_id: str, databases_dir: Path | None, cancel: threading.Event, params: Params) -> dict[str, Any]:
    def stop() -> None:
        if cancel.is_set():
            raise _Cancelled

    graph = load_graph(db_id, databases_dir)
    if graph is None:
        return {"ok": True, "skipped": "no schema.json to build paths from"}
    stop()
    base = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    relationships_doc = _read_json(base / APPLIED_REL)
    inferred_doc = _read_json(base / INFERRED_REL)
    profiles_doc = _read_json(base / PROFILES_REL)
    _read_json(base / EVIDENCE_FILE)  # sp_evidence is already folded into the graph by load_graph

    inputs = select_nodes(graph, profiles_doc)
    joins = collect_joins(graph, inputs, relationships_doc=relationships_doc, inferred_doc=inferred_doc)
    adj = build_links(joins)
    stop()
    pairs, search_stats = find_paths(adj, inputs.blocked_as_bridge, params, should_stop=stop)
    problems = check_routes(pairs, adj, inputs.nodes, params)
    if problems:  # the safety gate: a route that is not real is never written
        return {"ok": False, "error": "route check failed: " + "; ".join(problems[:3])}
    stop()  # last chance to stop: past this line the file is written

    stats = summary_counts(adj, pairs, search_stats, params)
    used = {
        "tables_considered": len(graph.tables),
        "nodes": len(inputs.nodes),
        "excluded_tables": {k: inputs.counts[k] for k in sorted(inputs.counts) if k not in EDGE_COUNT_KEYS},
        "edges_dropped": {k: inputs.counts[k] for k in sorted(inputs.counts) if k in EDGE_COUNT_KEYS},
        "bridge_blocked": len(inputs.blocked_as_bridge),
        "relationships_file": bool(relationships_doc and relationships_doc.get("relationships")),
        "inferred_edges_used": inputs.inferred_used,
        "table_profiles_used": inputs.profiles_used,
    }
    key = input_key(
        graph, params,
        relationships_hash=content_hash(relationships_doc),
        inferred_key=(inferred_doc or {}).get("input_key") if inputs.inferred_used else None,
        profiles_key=(profiles_doc or {}).get("input_key") if inputs.profiles_used else None,
    )
    doc = paths_document(graph, pairs, adj, params, stats, used, key)
    files = write_paths_file(db_id, doc, databases_dir=databases_dir)
    return {"ok": True, **stats, **{k: used[k] for k in ("nodes", "inferred_edges_used", "table_profiles_used")}, "warnings": len(graph.warnings), "file": files}


async def run_paths_phase(
    *,
    db_id: str,
    databases_dir: Path | None = None,
    budget_seconds: float = PATHS_BUDGET_SECONDS,
    params: Params | None = None,
) -> dict[str, Any]:
    """Build and record the join paths for ``db_id``. Returns a counts-only report."""
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files
        return {"ok": True, "skipped": "pinned database"}
    cancel = threading.Event()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(build_join_paths, db_id, databases_dir=databases_dir, cancel=cancel, params=params),
            timeout=budget_seconds,
        )
    except TimeoutError:
        cancel.set()  # the worker stops at its next starting table, before it can write
        return {"ok": False, "skipped": f"time budget of {budget_seconds:g}s exceeded; nothing was written"}


__all__ = ["PATHS_BUDGET_SECONDS", "build_join_paths", "run_paths_phase"]
