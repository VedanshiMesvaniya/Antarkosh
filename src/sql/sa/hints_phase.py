"""The routing-hints and synonym-glossary phase of a schema sync (auto-schema Phase 9d).

Reads only files earlier phases already wrote (``schema.json``, ``table_inventory.json``, ``table_profiles.json``): it never
connects to the database, never calls a model and costs milliseconds, so it is not skipped or budgeted. It fills
``semantics/glossary.json`` and ``semantics/routing_hints.json`` (see ``semantics/routing.py`` for what is generated and the
rules that keep it safe), then clears the caches that hold the old text.

With ``inject`` off (the database does not use generated knowledge in prompts) the generated part is removed again and nothing a
person wrote is touched. Every failure becomes a report; this can never fail a sync. ``erp_main`` is never touched.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.paths.links import select_nodes
from src.sql.profiles.writer import PROFILES_REL
from src.sql.relationships.model import load_graph
from src.sql.semantics.routing import (
    Hints,
    build_hints,
    merge_routing_hints,
    merge_synonym_glossary,
)

logger = logging.getLogger(__name__)


def _prepare(db_id: str, databases_dir: Path | None) -> Hints | None:
    """The hints for this catalog, or None when there is no catalog yet. CPU-bound; call through ``asyncio.to_thread``."""
    graph = load_graph(db_id, databases_dir)
    if graph is None:
        return None
    base = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    try:
        profiles = json.loads((base / PROFILES_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        profiles = None
    nodes = select_nodes(graph, profiles if isinstance(profiles, dict) else None)
    eligible = set(nodes.nodes) - set(nodes.blocked_as_bridge)  # no look-alike copies, no test/backup/temp tables, no views
    return build_hints(graph, eligible)


def _clear_caches(db_id: str) -> None:
    from src.sql.prompt_builder import clear_prompt_caches
    from src.sql.table_router import load_routing_hints

    clear_prompt_caches(db_id)
    load_routing_hints.cache_clear(db_id)  # type: ignore[attr-defined]


async def run_hints_phase(*, db_id: str, inject: bool = True, databases_dir: Path | None = None) -> dict[str, Any]:
    """Generate the synonym glossary and the routing hints. Returns a report of counts only; never raises."""
    report: dict[str, Any] = {"ok": True}
    try:
        validate_db_id(db_id)
        if db_id == DEFAULT_DB_ID:
            report["skipped"] = "pinned database: curated files are never replaced"
            return report
        hints = await asyncio.to_thread(_prepare, db_id, databases_dir)
        if hints is None:
            report["skipped"] = "no catalog yet (schema/schema.json); nothing to generate from"
            return report
        if not inject:  # the generated knowledge is not used by this database: take back what an earlier run wrote
            hints = Hints()
            report["injection"] = "off"
        report["tables"] = hints.stats.get("tables", 0)
        report["glossary"] = await asyncio.to_thread(merge_synonym_glossary, db_id, hints.glossary, databases_dir)
        report["routing"] = await asyncio.to_thread(
            merge_routing_hints, db_id, hints.fallback_rules, hints.anchor_rules, databases_dir
        )
        for key in ("groups_present", "groups_ambiguous", "too_broad", "no_table_named"):
            if key in hints.stats:
                report[key] = hints.stats[key]
        if report["glossary"].get("written") or report["routing"].get("written"):
            await asyncio.to_thread(_clear_caches, db_id)
    except Exception as exc:  # noqa: BLE001 - this phase must never fail a sync
        logger.warning("Routing hints failed for %s: %s", db_id, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return report


__all__ = ["run_hints_phase"]
