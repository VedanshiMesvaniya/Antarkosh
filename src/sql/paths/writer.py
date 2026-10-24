"""The join-paths file (auto-schema Phase 8): ``databases/<db_id>/schema/join_paths.json``.

Safety rules, each enforced here and tested:

* ``erp_main`` is never read or written.
* The folder must already exist; this module never creates a database folder.
* Only a missing file, or one this phase generated (``"generated_by": "auto_join_paths"``), is replaced. Anything else
  in that place is treated as curated and left untouched.
* The write is atomic, and skipped when nothing that feeds the file has changed since the last write.
* Prompts do not read this file yet (Phase 12 does, in shadow mode first), so no prompt cache needs clearing.

Origin: new (no previous file)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.paths.links import Link
from src.sql.paths.search import RULESET_VERSION, Params, Route, route_dirs
from src.sql.relationships.model import CatalogGraph

GENERATED_BY = "auto_join_paths"
PATHS_REL = "schema/join_paths.json"
MAX_HUBS_LISTED = 50
MAX_FILE_BYTES = 12_000_000  # about what a prompt-time loader can read quickly; larger results are trimmed, weakest pairs first


def input_key(
    graph: CatalogGraph,
    params: Params,
    *,
    relationships_hash: str | None,
    inferred_key: str | None,
    profiles_key: str | None,
    size_limit: int = MAX_FILE_BYTES,
) -> str:
    """Changes only when something that would change the file changes. Timestamps are not part of it."""
    raw = json.dumps(
        [graph.inputs_hash, RULESET_VERSION, dataclasses.asdict(params), relationships_hash, inferred_key, profiles_key, size_limit], sort_keys=True
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _join_entries(route: Route, adj: dict[str, dict[str, Link]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for x, y, step in zip(route.tables, route.tables[1:], route_dirs(route.tables, adj)):
        link = adj[x][y]
        entry: dict[str, Any] = {
            "from": f"{x}.{link.join.column_of(x)}", "to": f"{y}.{link.join.column_of(y)}",
            "confidence": link.confidence, "evidence": list(link.join.kinds), "direction": step or "unknown",
        }
        if link.alternates:
            entry["other_column_pairs"] = link.alternates
        out.append(entry)
    return out


def paths_document(
    graph: CatalogGraph,
    pairs: dict[tuple[str, str], list[Route]],
    adj: dict[str, dict[str, Link]],
    params: Params,
    stats: dict[str, Any],
    inputs: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    from src.sql.paths.search import hub_factor

    hubs = sorted(((len(n), t) for t, n in adj.items() if len(n) > params.hub_free), key=lambda x: (-x[0], x[1]))[:MAX_HUBS_LISTED]
    return {
        "generated_by": GENERATED_BY,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "Indirect join routes between tables that do not join directly: tier 'near' is 2 to 3 joins, tier 'deep' is 4 to 5 joins and appears only for pairs with no shorter route. Built only from "
                "trusted edges (declared keys, relationships.json, high-confidence inferred edges, joins seen in 2+ procedures). "
                "A route's score falls with each join, with every hub it passes through, and with a many-to-many or shared-parent "
                "shape; deep routes also need a higher score and at most one change of direction. Copies of another table (Phase 7) are left out. Advisory only: prompts do not read this file yet.",
        "input_key": key,
        "inputs_hash": graph.inputs_hash,
        "ruleset_version": RULESET_VERSION,
        "params": dataclasses.asdict(params),
        "inputs": inputs,
        "stats": stats,
        "hubs": [{"table": t, "links": n, "damping": round(hub_factor(n, params.hub_free), 3)} for n, t in hubs],
        "pairs": [
            {
                "a": a, "b": b, "hops": len(routes[0].tables) - 1, "score": routes[0].score,
                "tier": "deep" if len(routes[0].tables) - 1 > params.max_depth else "near",
                "routes": [{"score": r.score, "shape": r.shape, "tables": list(r.tables), "joins": _join_entries(r, adj)} for r in routes],
            }
            for (a, b), routes in sorted(pairs.items())
        ],
    }


def fit_to_size(
    make: Callable[[dict[tuple[str, str], list[Route]]], dict[str, Any]],
    pairs: dict[tuple[str, str], list[Route]],
    params: Params,
    limit: int = MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Build the document and, if it would be larger than ``limit`` bytes on disk, rebuild it from fewer pairs: deep pairs go
    first (weakest first), then the weakest near pairs. A pair is dropped whole, so every route that remains is intact."""
    doc = make(pairs)
    keep = pairs
    for _ in range(4):  # the size per pair is only an estimate, so allow a few corrections
        size = len(json.dumps(doc, indent=2))
        if size <= limit or not keep:
            return doc
        n = max(0, int(len(keep) * limit * 0.9 / size))
        ranked = sorted(keep, key=lambda k: (len(keep[k][0].tables) - 1 > params.max_depth, -keep[k][0].score, k))
        keep = {k: keep[k] for k in sorted(ranked[:n])}
        doc = make(keep)
    return doc


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_paths_file(db_id: str, doc: dict[str, Any], *, databases_dir: Path | None = None) -> dict[str, Any]:
    """Write ``schema/join_paths.json``. Returns ``{"written": bool, "unchanged": bool, "skipped": str | None}``."""
    validate_db_id(db_id)
    out: dict[str, Any] = {"written": False, "unchanged": False, "skipped": None}
    if db_id == DEFAULT_DB_ID:
        out["skipped"] = "pinned database: curated files are never replaced"
        return out
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        out["skipped"] = "database folder does not exist"
        return out
    path = folder / PATHS_REL
    if path.exists():
        existing = _read(path)
        if not (isinstance(existing, dict) and existing.get("generated_by") == GENERATED_BY):
            out["skipped"] = "a file not generated by auto_join_paths is present; left untouched"
            return out
        if existing.get("input_key") == doc["input_key"]:
            out["unchanged"] = True
            return out
    registry.atomic_write_json(path, doc, mode=0o644)
    out["written"] = True
    return out


__all__ = ["GENERATED_BY", "MAX_FILE_BYTES", "MAX_HUBS_LISTED", "PATHS_REL", "fit_to_size", "input_key", "paths_document", "write_paths_file"]
