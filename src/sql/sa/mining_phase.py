"""The procedure-mining phase of a schema sync (auto-schema Phase 5).

Takes the routines Phase 4 already read (they stay in memory), finds the SQL statements inside them,
extracts facts (tables, joins, string values) and writes ``schema/sp_evidence.json``. Nothing is
executed and no database is contacted; the routine text is discarded when the sync ends.

Safety: SQLGlot parsing is CPU-bound, so it runs in a worker thread and under a time budget. It refuses
to run on an untested SQLGlot major version. Every failure becomes a report, so this can never fail
a sync, and ``erp_main`` is never mined.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import sqlglot

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.mining.catalog_index import CatalogIndex
from src.sql.mining.evidence import Evidence, summary, write_evidence
from src.sql.mining.extractor import extract_routine_facts
from src.sql.mining.splitter import split_statements
from src.sql.sa.routines import Routine
from src.sql.semantics.hints import HintsCollector, write_hints

logger = logging.getLogger(__name__)

TESTED_SQLGLOT_MAJOR = 30
MINING_BUDGET_SECONDS = 60.0


def sqlglot_supported() -> bool:
    try:
        return int(str(sqlglot.__version__).split(".")[0]) == TESTED_SQLGLOT_MAJOR
    except (ValueError, AttributeError):
        return False


def mine_routines(
    routines: list[Routine],
    engine: str,
    catalog: CatalogIndex | None,
    budget_seconds: float = MINING_BUDGET_SECONDS,
    hints: HintsCollector | None = None,
) -> Evidence:
    """Synchronous and CPU-bound. Call it through ``asyncio.to_thread``."""
    evidence = Evidence(catalog)
    deadline = time.monotonic() + budget_seconds
    sqlglot_logger = logging.getLogger("sqlglot")
    previous = sqlglot_logger.level
    sqlglot_logger.setLevel(logging.ERROR)  # SQLGlot warns on every statement it cannot fully parse
    try:
        for routine in routines:
            if not routine.definition:
                continue
            if time.monotonic() > deadline:
                evidence.stats["truncated"] = True
                break
            split = split_statements(routine.definition, engine, deadline=deadline)
            facts = extract_routine_facts(split.trees, engine, catalog)
            evidence.add_routine(split.found, split.parsed, split.dynamic, facts, truncated=split.truncated)
            if hints is not None:  # Phase 9a: aliases, parameter names and CASE labels from the same parsed statements
                hints.add(split.trees, catalog)
    finally:
        sqlglot_logger.setLevel(previous)
    return evidence


async def run_mining_phase(
    *,
    db_id: str,
    engine_key: str,
    routines: list[Routine],
    databases_dir: Path | None = None,
    budget_seconds: float = MINING_BUDGET_SECONDS,
    collect_hints: bool = False,
) -> dict[str, Any]:
    """Mine ``routines`` for ``db_id`` and record the evidence. Returns a counts-only report."""
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files
        return {"ok": True, "skipped": "pinned database"}
    if not sqlglot_supported():
        return {"ok": False, "skipped": f"sqlglot {sqlglot.__version__} is not the tested major version {TESTED_SQLGLOT_MAJOR}"}

    base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
    catalog = CatalogIndex.from_schema_json(base / db_id / "schema" / "schema.json")
    collector = HintsCollector() if collect_hints and catalog is not None else None
    if collector is not None:
        evidence = await asyncio.to_thread(mine_routines, routines, engine_key, catalog, budget_seconds, collector)
    else:  # exactly the call this phase always made
        evidence = await asyncio.to_thread(mine_routines, routines, engine_key, catalog, budget_seconds)
    doc = evidence.document(db_id, engine_key)
    report = {"ok": True, **summary(doc)}
    report["files"] = write_evidence(db_id, doc, databases_dir)
    if collector is not None:
        try:
            hints_doc = collector.document(db_id)
            report["column_hints"] = {**hints_doc["stats"], "files": write_hints(db_id, hints_doc, databases_dir)}
        except Exception as exc:  # noqa: BLE001 - optional output: it can never fail the mining phase
            report["column_hints"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return report


__all__ = ["MINING_BUDGET_SECONDS", "TESTED_SQLGLOT_MAJOR", "mine_routines", "run_mining_phase", "sqlglot_supported"]
