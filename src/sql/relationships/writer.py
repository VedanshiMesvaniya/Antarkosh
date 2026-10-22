"""Scoring bands and the relationship files (auto-schema Phase 6, step 6e: step 23 of the 23).

Takes the edges 6d resolved and writes up to two files into an EXISTING ``databases/<db_id>/schema/`` folder:

* ``inferred_relationships.json``  every surviving edge with its score, band and evidence, for review. Never read by prompts.
* ``relationships.json``           only the edges safe to show a model, written only when the caller says ``apply``.

Safety rules, each enforced here and tested:

* ``erp_main`` is never read or written.
* A curated ``relationships.json`` is never replaced. The only files this module overwrites are ones it generated
  (``"generated_by": "auto_relationships"``), a missing file, or the empty placeholder the template ships.
* An edge reaches ``relationships.json`` only if it scores at least the high threshold, its direction is known, it is
  not ambiguous (unless a declared key or a procedure vouches for it), and both tables live in the schema the SQL
  prompts can see. In a multi-schema database whose answering schema is unknown, nothing is applied.
* The per-table cap (default 40) never drops a declared foreign key.
* Writes are atomic, and skipped when the inputs have not changed since the last write.
* After ``relationships.json`` changes, the prompt caches for the database are cleared. Without that, prompts keep
  serving the old file until the process restarts.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.relationships.candidates import Edge
from src.sql.relationships.model import CatalogGraph

logger = logging.getLogger(__name__)

GENERATED_BY = "auto_relationships"
RULESET_VERSION = 1  # bump when a rule or weight changes: it forces a rewrite even if the catalog did not
INFERRED_REL = "schema/inferred_relationships.json"
APPLIED_REL = "schema/relationships.json"

HIGH_THRESHOLD = 0.70  # starting values; measured and set in step 6i
MEDIUM_THRESHOLD = 0.50
MAX_EDGES_PER_TABLE = 40
MAX_INFERRED_EDGES = 20000  # keeps the review file a sane size on a very large catalog


def band(score: float, threshold: float = HIGH_THRESHOLD) -> str:
    return "high" if score >= threshold else "medium" if score >= MEDIUM_THRESHOLD else "low"


# ---------------------------------------------------------------- selection


def select_applied(
    graph: CatalogGraph, edges: list[Edge], *, threshold: float = HIGH_THRESHOLD, max_per_table: int = MAX_EDGES_PER_TABLE
) -> tuple[list[Edge], Counter[str]]:
    """The edges safe to show a model, and how many were held back and why."""
    held: Counter[str] = Counter()
    if graph.multi_schema and not graph.scope_known:
        held["scope_unknown"] = len(edges)  # we cannot tell which schema the prompts see
        return [], held
    eligible: list[Edge] = []
    for e in edges:
        child, parent = graph.tables[e.child], graph.tables[e.parent]
        if e.score < threshold:
            held["below_threshold"] += 1
        elif not e.direction_known:
            held["direction_unknown"] += 1
        elif e.ambiguous and not e.has_hard_evidence:
            held["ambiguous"] += 1
        elif not (graph.in_answering_scope(child) and graph.in_answering_scope(parent)):
            held["outside_scope"] += 1
        else:
            eligible.append(e)
    load: Counter[str] = Counter()
    chosen: list[Edge] = []
    for e in sorted(eligible, key=lambda e: (-e.score, e.key)):
        ends = {e.child, e.parent}
        if "declared" not in e.evidence and any(load[t] >= max_per_table for t in ends):
            held["over_table_cap"] += 1
            continue
        for t in ends:
            load[t] += 1
        chosen.append(e)
    return sorted(chosen, key=lambda e: e.key), held


# ---------------------------------------------------------------- documents


def edge_entry(graph: CatalogGraph, e: Edge, threshold: float, *, review: bool = False) -> dict[str, Any]:
    child, parent = graph.tables[e.child], graph.tables[e.parent]
    entry: dict[str, Any] = {
        "from_table": child.name, "from_column": e.child_column, "to_table": parent.name, "to_column": e.parent_column,
        "from_schema": child.schema, "to_schema": parent.schema,
        "confidence": e.score, "cardinality": e.cardinality,
        "evidence": [{"rule": r, "weight": w} for r, w in sorted(e.evidence.items())],
    }
    if review:
        entry.update({"band": band(e.score, threshold), "direction_known": e.direction_known, "ambiguous": e.ambiguous})
    return entry


def input_key(graph: CatalogGraph, kind: str, threshold: float, max_per_table: int) -> str:
    """Changes only when something that would change the file changes. Timestamps are not part of it."""
    raw = json.dumps([graph.inputs_hash, RULESET_VERSION, kind, threshold, max_per_table, MAX_INFERRED_EDGES])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def inferred_document(graph: CatalogGraph, edges: list[Edge], gate_counts: dict[str, int], key: str, threshold: float) -> tuple[dict[str, Any], int]:
    ranked = sorted(edges, key=lambda e: (-e.score, e.key))
    kept = ranked[:MAX_INFERRED_EDGES]
    bands = Counter(band(e.score, threshold) for e in ranked)
    doc = {
        "generated_by": GENERATED_BY,
        "generated_at": _now(),
        "note": "Inferred from names, keys and procedure evidence. For review: prompts never read this file. "
                "Turn on auto_relationships_apply to publish the high-confidence edges to schema/relationships.json.",
        "input_key": key,
        "inputs_hash": graph.inputs_hash,
        "ruleset_version": RULESET_VERSION,
        "threshold": threshold,
        "stats": {"edges": len(ranked), "high": bands["high"], "medium": bands["medium"], "low": bands["low"], **gate_counts},
        "relationships": [edge_entry(graph, e, threshold, review=True) for e in kept],
    }
    return doc, len(ranked) - len(kept)


def applied_document(graph: CatalogGraph, edges: list[Edge], key: str, threshold: float) -> dict[str, Any]:
    return {
        "generated_by": GENERATED_BY,
        "generated_at": _now(),
        "note": "Join map inferred by auto_relationships (high-confidence edges only). Replace this file with your own to take "
                "control: a file without the generated_by marker is never overwritten.",
        "input_key": key,
        "threshold": threshold,
        "relationships": [edge_entry(graph, e, threshold) for e in edges],
    }


# ---------------------------------------------------------------- writing


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _state(path: Path) -> str:
    """``missing`` | ``placeholder`` | ``ours`` | ``foreign`` (curated, unreadable, or anything else: never touched)."""
    if not path.exists():
        return "missing"
    doc = _read(path)
    if isinstance(doc, dict) and doc.get("generated_by") == GENERATED_BY:
        return "ours"
    rels = doc.get("relationships") if isinstance(doc, dict) else doc
    if isinstance(rels, list) and not rels and not (isinstance(doc, dict) and doc.get("generated_by")):
        return "placeholder"
    return "foreign"


def _clear_prompt_caches(db_id: str) -> None:
    try:
        from src.sql.prompt_builder import clear_prompt_caches

        clear_prompt_caches(db_id)
    except Exception as exc:  # noqa: BLE001 - a cache that could not be cleared must not fail the sync
        logger.warning("Could not clear prompt caches for %s: %s", db_id, exc)


def write_relationship_files(
    db_id: str,
    graph: CatalogGraph,
    edges: list[Edge],
    gate_counts: dict[str, int],
    *,
    apply: bool,
    threshold: float = HIGH_THRESHOLD,
    max_per_table: int = MAX_EDGES_PER_TABLE,
    databases_dir: Path | None = None,
) -> dict[str, Any]:
    """Write the review file, and the prompt-facing file when ``apply``. Counts and file names only."""
    validate_db_id(db_id)
    result: dict[str, Any] = {"written": [], "unchanged": [], "skipped": {}, "truncated": 0, "applied_edges": 0, "held_back": {}}
    if db_id == DEFAULT_DB_ID:
        result["skipped"]["all"] = "pinned database: curated files are never replaced"
        return result
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        result["skipped"]["all"] = "database folder does not exist"
        return result

    inferred_path = folder / INFERRED_REL
    state = _state(inferred_path)
    key = input_key(graph, "inferred", threshold, max_per_table)
    if state == "foreign":
        result["skipped"][INFERRED_REL] = "a file not generated by auto_relationships is present; left untouched"
    elif state == "ours" and (_read(inferred_path) or {}).get("input_key") == key:
        result["unchanged"].append(INFERRED_REL)
    else:
        doc, truncated = inferred_document(graph, edges, gate_counts, key, threshold)
        registry.atomic_write_json(inferred_path, doc, mode=0o644)
        result["written"].append(INFERRED_REL)
        result["truncated"] = truncated

    if not apply:
        return result

    chosen, held = select_applied(graph, edges, threshold=threshold, max_per_table=max_per_table)
    result["applied_edges"], result["held_back"] = len(chosen), dict(held)
    applied_path = folder / APPLIED_REL
    state = _state(applied_path)
    key = input_key(graph, "applied", threshold, max_per_table)
    if state == "foreign":
        result["skipped"][APPLIED_REL] = "a curated relationships.json is present; left untouched"
    elif state == "ours" and (_read(applied_path) or {}).get("input_key") == key:
        result["unchanged"].append(APPLIED_REL)
    elif not chosen and state != "ours":
        result["skipped"][APPLIED_REL] = "no edge is safe to apply yet"
    else:
        registry.atomic_write_json(applied_path, applied_document(graph, chosen, key, threshold), mode=0o644)
        result["written"].append(APPLIED_REL)
        _clear_prompt_caches(db_id)
    return result


__all__ = [
    "APPLIED_REL", "GENERATED_BY", "HIGH_THRESHOLD", "INFERRED_REL", "MAX_EDGES_PER_TABLE", "MEDIUM_THRESHOLD", "RULESET_VERSION",
    "applied_document", "band", "edge_entry", "inferred_document", "input_key", "select_applied", "write_relationship_files",
]
