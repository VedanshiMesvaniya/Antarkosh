"""Soft-delete evidence phase: ask the connected database how likely soft-delete columns are really used (aggregate counts only).

Runs after the catalog phase, behind ``auto_soft_delete_evidence_apply`` (per database, off by default). For each likely marker column
(see ``soft_delete_evidence.candidates``) it asks ONE statement that returns four or five integers over a bounded sample of the
table's rows: how many rows, how many not NULL, how many distinct values, and how many are spelled the usual way for "deleted" and
for "live". No value, key or row is selected, stored, logged or sent to a model; an error is recorded by its class name only.

What a table's column is, once counted, is decided by ``soft_delete_evidence.classify``; what the application does about it is decided
by the two flags described there (shadow first). This phase only writes ``semantics/soft_delete_evidence.json`` and never changes a
spec, a prompt or an answer.

Declared, curated and atlas tables are facts and are never queried. A table whose evidence is unchanged (same structure, within the cache
lifetime) costs no query. Budgets per sync: queries, total time, one statement's time, and a stop after repeated failures. Never raises.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from src.sql import registry
from src.sql import soft_delete_evidence as ev
from src.sql.dialects import get_dialect_profile
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.relationships.data_evidence import _PROBING, _install_log_filter
from src.sql.soft_delete_spec import clear_spec_cache, load_base_specs

logger = logging.getLogger(__name__)

Runner = Callable[..., Awaitable[list[dict[str, Any]]]]


# ---------------------------------------------------------------------------------------------------- the statement


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def build_probe_sql(c: ev.Candidate, dialect: str, sample_rows: int = ev.SAMPLE_ROWS) -> str:
    """One SELECT of integer counts over a bounded sample of the column. Identifiers are quoted per dialect from the catalog."""
    table = exp.Table(this=_ident(c.table), db=_ident(c.schema) if c.schema else None)
    inner = exp.select(exp.alias_(exp.column(c.column, quoted=True), "c", quoted=True)).from_(table).limit(sample_rows)
    sample = exp.Subquery(this=inner, alias=exp.TableAlias(this=_ident("s")))
    col = exp.column("c", table="s", quoted=True)
    outputs = [exp.alias_(exp.Count(this=exp.Star()), "total_rows"), exp.alias_(exp.Count(this=col.copy()), "nonnull_rows")]
    if c.probe in ("boolean", "int", "char"):
        if c.probe == "boolean":
            marked: list[exp.Expression] = [exp.true()]
            unmarked: list[exp.Expression] = [exp.false()]
        elif c.probe == "int":
            marked, unmarked = [exp.Literal.number(1)], [exp.Literal.number(0)]
        else:
            marked = [exp.Literal.string("Y"), exp.Literal.string("y")]
            unmarked = [exp.Literal.string("N"), exp.Literal.string("n")]

        def count_of(values: list[exp.Expression]) -> exp.Expression:
            test = exp.EQ(this=col.copy(), expression=values[0]) if len(values) == 1 else exp.In(this=col.copy(), expressions=values)
            return exp.Sum(this=exp.Case(ifs=[exp.If(this=test, true=exp.Literal.number(1))], default=exp.Literal.number(0)))

        outputs += [
            exp.alias_(exp.Count(this=exp.Distinct(expressions=[col.copy()])), "distinct_values"),
            exp.alias_(count_of(marked), "marked_like"),
            exp.alias_(count_of(unmarked), "unmarked_like"),
        ]
    sql = exp.select(*outputs).from_(sample).sql(dialect=dialect)
    tree = sqlglot.parse_one(sql, read=dialect)  # defence in depth: exactly one SELECT
    if not isinstance(tree, exp.Select) or any(tree.find(n) for n in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command)):
        raise ValueError("the probe is not a single SELECT")
    return sql


def parse_counts(rows: Any, probe: str) -> ev.Facts | None:
    """The counts as ``Facts``, or None when the result is not four or five consistent non-negative integers."""
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = {str(k).lower(): v for k, v in rows[0].items()}
    try:
        total, nonnull = int(row["total_rows"]), int(row["nonnull_rows"])
        if probe in ("boolean", "int", "char"):
            distinct, marked, unmarked = (int(row[k] or 0) for k in ("distinct_values", "marked_like", "unmarked_like"))
        else:  # a timestamp or a date held in text: only NULL / not NULL matters
            distinct, marked, unmarked = 0, nonnull, 0
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= nonnull <= total and 0 <= marked + unmarked <= nonnull and 0 <= distinct <= nonnull):
        return None
    return ev.Facts(total, nonnull, distinct, marked, unmarked)


async def _default_runner(sql: str, *, max_rows: int, db_id: str) -> list[dict[str, Any]]:
    from src.core.db_client import run_readonly_query

    return await run_readonly_query(sql, max_rows=max_rows, db_id=db_id)


async def _probe(c: ev.Candidate, dialect: str, runner: Runner, db_id: str, timeout: float, now: datetime) -> dict[str, Any]:
    token = _PROBING.set(True)  # the database client's error log says only that it failed while a probe runs
    try:
        rows = await asyncio.wait_for(runner(build_probe_sql(c, dialect), max_rows=2, db_id=db_id), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - recorded by class name only: a database message can quote a value
        return ev.make_entry(c, "error", now, error=type(exc).__name__)
    finally:
        _PROBING.reset(token)
    facts = parse_counts(rows, c.probe)
    if facts is None:
        return ev.make_entry(c, "error", now, error="UnexpectedResult")
    return ev.make_entry(c, ev.classify(c, facts), now, facts)


# ---------------------------------------------------------------------------------------------------- the phase


async def run_soft_delete_phase(
    *, db_id: str, databases_dir: Path | None = None, runner: Runner | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Collect the evidence for ``db_id`` and write ``semantics/soft_delete_evidence.json``. Counts-only report. Never raises."""
    started = time.monotonic()
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:
        return {"ok": True, "skipped": "pinned database"}
    try:
        return await _run(db_id, databases_dir, runner or _default_runner, now or ev._now(), started)
    except Exception as exc:  # noqa: BLE001 - optional evidence must never fail the sync
        logger.warning("Soft-delete evidence failed for %s: %s", db_id, type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__, "elapsed_s": round(time.monotonic() - started, 2)}


async def _run(db_id: str, databases_dir: Path | None, runner: Runner, now: datetime, started: float) -> dict[str, Any]:
    root = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    try:
        schema_doc = json.loads((root / "schema" / "schema.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "skipped": "no schema.json to look for soft-delete columns in"}
    try:
        dialect = get_dialect_profile(str(schema_doc.get("engine") or "")).sqlglot_dialect
    except ValueError:
        return {"ok": True, "skipped": "the database engine is unknown"}

    base = load_base_specs(db_id, databases_dir=databases_dir)
    wanted: list[ev.Candidate] = []
    for c in ev.candidates(schema_doc):
        spec = base.get(c.table)
        if spec is not None and spec.source != "discovered":
            continue  # declared, curated and atlas specs are facts
        if spec is not None and spec.column.lower() != c.column.lower():
            continue  # a name+type spec: only its own column is in question
        wanted.append(c)

    cached, may_write = ev.read_evidence(root / ev.EVIDENCE_REL)
    entries: dict[str, dict[str, Any]] = {}
    todo: list[ev.Candidate] = []
    for c in wanted:
        old = cached.get(ev.entry_key(c.table, c.column))
        if old is not None and ev.fresh(old, c, now):
            entries[ev.entry_key(c.table, c.column)] = old
        else:
            todo.append(c)
    report: dict[str, Any] = {"ok": True, "candidates": len(wanted), "cached": len(entries), "queried": 0, "errors": 0, "not_checked": 0}

    _install_log_filter()
    deadline = started + ev.DATA_BUDGET_SECONDS
    streak = 0
    stopped: str | None = None
    for position, c in enumerate(todo):
        remaining = deadline - time.monotonic()
        if report["queried"] >= ev.MAX_QUERIES:
            stopped = "query budget used"
        elif remaining <= 0:
            stopped = "time budget used"
        elif streak >= ev.MAX_CONSECUTIVE_ERRORS:
            stopped = "repeated failures"
        if stopped:
            report["not_checked"] = len(todo) - position
            report["stopped"] = stopped
            break
        report["queried"] += 1
        entry = await _probe(c, dialect, runner, db_id, min(ev.QUERY_TIMEOUT_SECONDS, remaining), now)
        entries[ev.entry_key(c.table, c.column)] = entry
        if entry["status"] == "error":
            report["errors"] += 1
            streak += 1
        else:
            streak = 0

    statuses = [str(e.get("status")) for e in entries.values()]
    report.update({s: statuses.count(s) for s in ev.STATUSES if s != "error"})
    if may_write and (report["queried"] or set(cached) != set(entries)):
        report["evidence_file"] = ev.write_evidence(db_id, entries, databases_dir)
    elif not may_write:
        report["evidence_file"] = "a file not written by this phase is present; left untouched"
    clear_spec_cache(db_id)
    ev.clear_view_cache(db_id)
    view = ev.load_view(db_id, base, databases_dir=databases_dir)
    report.update({"verified": len(view.verified), "uncertain": len(view.uncertain), "contradicted_guesses": len(view.demoted)})
    report["elapsed_s"] = round(time.monotonic() - started, 2)
    return report


__all__ = ["build_probe_sql", "parse_counts", "run_soft_delete_phase"]
