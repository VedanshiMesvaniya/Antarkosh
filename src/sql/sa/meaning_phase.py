"""The column-meaning phase of a schema sync (auto-schema Phase 9a + 9b + 9c).

Order: (9a) gather the evidence for every table from the files earlier phases wrote; (9b) ask the model, one batched call
per table whose evidence changed, check every answer, and write ``semantics/column_dictionary.json``; (9c) refresh the
glossary entries (high confidence only) and report which tables' embedded text changed so the sync can re-embed just those.

Cost and safety controls:

* A table whose evidence has not changed since its cards were made costs NOTHING: no call is made for it.
* At most ``max_tables`` tables per run and a wall-clock budget; whatever is left is done by the next run, so a first run on
  a large database is spread over several syncs and a sync is never held up for long.
* A few provider failures in a row stop the run (no hammering a dead or rate-limited provider).
* Only metadata is sent to the model. Nothing here reads a data row or contacts the database.
* Every failure becomes a report; this can never fail a sync. ``erp_main`` is never touched. The report holds counts only.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.mining.catalog_index import (
    CatalogIndex,  # noqa: F401  (re-exported for tests that build hints)
)
from src.sql.paths.links import select_nodes
from src.sql.profiles.writer import PROFILES_REL
from src.sql.relationships.model import EVIDENCE_FILE, load_graph
from src.sql.relationships.names import learn_common_columns
from src.sql.relationships.writer import APPLIED_REL, INFERRED_REL
from src.sql.semantics.dictionary import (
    empty_document,
    is_current,
    read_dictionary,
    write_dictionary,
)
from src.sql.semantics.evidence import TableEvidence, collect_table_evidence, evidence_summary
from src.sql.semantics.hints import HINTS_REL
from src.sql.semantics.meaning import LLM, PROMPT_VERSION, apply_consistency, generate_table
from src.sql.semantics.wiring import (
    build_glossary_entries,
    changed_tables,
    embedding_extras_from,
    merge_glossary,
)

logger = logging.getLogger(__name__)


def _env_number(name: str, default: float, cast: type = float) -> Any:
    try:
        return cast(os.environ.get(name, "")) if os.environ.get(name) else default
    except ValueError:
        return default


MEANING_BUDGET_SECONDS = _env_number("COLUMN_MEANING_BUDGET_SECONDS", 90.0)
MAX_TABLES_PER_RUN = _env_number("COLUMN_MEANING_MAX_TABLES", 40, int)
CONCURRENCY = _env_number("COLUMN_MEANING_CONCURRENCY", 4, int)
CALL_TIMEOUT_SECONDS = 60.0
MAX_CONSECUTIVE_FAILURES = 3
LLM_TASK = "extraction"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _prepare(db_id: str, databases_dir: Path | None) -> dict[str, Any] | None:
    """Everything the phase needs from disk. CPU-bound; call through ``asyncio.to_thread``. None if there is no catalog."""
    graph = load_graph(db_id, databases_dir)
    if graph is None:
        return None
    base = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    docs = {
        "hints": _read_json(base / HINTS_REL),
        "evidence": _read_json(base / EVIDENCE_FILE),
        "relationships": _read_json(base / APPLIED_REL),
        "inferred": _read_json(base / INFERRED_REL),
        "profiles": _read_json(base / PROFILES_REL),
    }
    evidence = collect_table_evidence(
        graph, hints_doc=docs["hints"], evidence_doc=docs["evidence"], relationships_doc=docs["relationships"],
        inferred_doc=docs["inferred"], profiles_doc=docs["profiles"],
    )
    nodes = select_nodes(graph, docs["profiles"])
    eligible = {q for q in evidence if q in nodes.nodes and q not in nodes.blocked_as_bridge}
    usage: dict[str, int] = {}
    for name, counts in ((docs["evidence"] or {}).get("tables") or {}).items():
        t = graph.table(str(name))
        if t is not None and isinstance(counts, dict):
            usage[t.qualified] = int(counts.get("read_in") or 0) + int(counts.get("written_in") or 0)
    return {
        "graph": graph, "evidence": evidence, "eligible": eligible, "usage": usage, "docs": docs, "nodes": nodes,
        "common": learn_common_columns(graph.tables.values()), "scope_unknown": bool(nodes.counts.get("scope_unknown")),
    }


def _priority(q: str, ev: TableEvidence, usage: dict[str, int]) -> tuple[int, int, str]:
    """Tables procedures use most come first, then tables with the most written-down evidence."""
    return (-usage.get(q, 0), -sum(1 for c in ev.columns if c.hard), q)


def _default_llm() -> tuple[LLM, Any]:
    from src.core.provider_client import ProviderRouter

    router = ProviderRouter()

    async def call(messages: list[dict[str, str]], max_tokens: int) -> str:
        return await asyncio.wait_for(
            router.chat(LLM_TASK, messages=messages, temperature=0.0, max_tokens=max_tokens), timeout=CALL_TIMEOUT_SECONDS
        )

    return call, router


async def run_meaning_phase(
    *,
    db_id: str,
    databases_dir: Path | None = None,
    llm: LLM | None = None,
    inject: bool = True,
    budget_seconds: float | None = None,
    max_tables: int | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """Generate, check and wire the column meanings of ``db_id``. Returns a counts-only report (plus ``embed_tables``, the
    names of tables whose embedded text changed, which the caller removes before it reports)."""
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files
        return {"ok": True, "skipped": "pinned database"}
    budget = MEANING_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    limit = MAX_TABLES_PER_RUN if max_tables is None else max_tables
    workers = max(1, CONCURRENCY if concurrency is None else concurrency)

    state, existing = read_dictionary(db_id, databases_dir)
    if state == "foreign":
        return {"ok": True, "skipped": "a file not generated by auto_column_meaning is present; left untouched"}
    prep = await asyncio.to_thread(_prepare, db_id, databases_dir)
    if prep is None:
        return {"ok": True, "skipped": "no schema.json to build meanings from"}
    graph, evidence, eligible = prep["graph"], prep["evidence"], prep["eligible"]
    report: dict[str, Any] = {"ok": True, "evidence": evidence_summary({q: evidence[q] for q in eligible})}
    if prep["scope_unknown"]:
        report["skipped_generation"] = "the schema the prompts see is unknown"

    doc = {**empty_document(), "tables": {q: e for q, e in (existing.get("tables") or {}).items() if q in eligible}}
    before = embedding_extras_from(existing)
    pending = sorted((q for q in eligible if not is_current(doc["tables"].get(q), evidence[q].hash, existing)),
                     key=lambda q: _priority(q, evidence[q], prep["usage"]))
    batch = pending[: max(0, limit)]
    stats = {"eligible": len(eligible), "up_to_date": len(eligible) - len(pending), "attempted": 0, "generated": 0, "failed": 0, "llm_calls": 0}

    if batch:
        try:
            call, router = (llm, None) if llm is not None else _default_llm()
        except Exception as exc:  # noqa: BLE001
            report["generation"] = f"no language model available ({type(exc).__name__})"
            call, router, batch = None, None, []
        if call is not None:
            deadline = time.monotonic() + budget
            queue: asyncio.Queue[str] = asyncio.Queue()
            for q in batch:
                queue.put_nowait(q)
            streak = {"failures": 0}

            async def worker() -> None:
                while not queue.empty() and time.monotonic() < deadline and streak["failures"] < MAX_CONSECUTIVE_FAILURES:
                    q = queue.get_nowait()
                    stats["attempted"] += 1
                    outcome = await generate_table(evidence[q], call)
                    if outcome is None:
                        stats["failed"] += 1
                        streak["failures"] += 1
                        continue
                    streak["failures"] = 0
                    stats["generated"] += 1
                    stats["llm_calls"] += outcome.calls
                    doc["tables"][q] = {
                        "evidence_hash": evidence[q].hash,
                        "model": getattr(router, "last_used", "") or "",
                        "skipped_common": outcome.skipped_common,
                        "unanswered": outcome.missing,
                        "columns": {name_of(evidence[q], k): m.to_json() for k, m in sorted(outcome.columns.items())},
                    }

            await asyncio.gather(*(worker() for _ in range(workers)))
            if streak["failures"] >= MAX_CONSECUTIVE_FAILURES:
                report["generation"] = "stopped after repeated language-model failures"

    stats["pending"] = len([q for q in eligible if not is_current(doc["tables"].get(q), evidence[q].hash, {**doc})])
    apply_consistency({q: e["columns"] for q, e in doc["tables"].items()}, prep["common"])
    levels = Counter(c["confidence"] for e in doc["tables"].values() for c in e["columns"].values())
    stats["cards"] = {k: levels[k] for k in ("high", "medium", "low")}
    report["meaning"] = stats

    if not doc["tables"] and state == "missing":  # nothing generated and nothing before: no empty file
        report["file"] = {"written": False, "unchanged": True, "skipped": None}
    else:
        report["file"] = await asyncio.to_thread(write_dictionary, db_id, doc, databases_dir)
        if report["file"].get("skipped"):
            return report

    try:  # 9c: the prompt channel. With injection off, every auto entry is removed again.
        entries = build_glossary_entries(doc, graph) if inject else {}
        report["glossary"] = await asyncio.to_thread(merge_glossary, db_id, entries, databases_dir)
        if report["glossary"].get("written"):
            from src.sql.prompt_builder import clear_prompt_caches

            clear_prompt_caches(db_id)
    except Exception as exc:  # noqa: BLE001
        report["glossary"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    report["embed_tables"] = changed_tables(before, embedding_extras_from(doc))
    return report


def name_of(ev: TableEvidence, lower: str) -> str:
    """The column's real spelling in the catalog."""
    return next((c.name for c in ev.columns if c.name.lower() == lower), lower)


__all__ = ["MEANING_BUDGET_SECONDS", "PROMPT_VERSION", "run_meaning_phase"]
