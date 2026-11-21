"""Aggregate-only checks of likely joins against the connected database (metadata-driven SQL accuracy, Release 2).

Phase 6 proposes joins from names, keys and procedures and never looks at data, so a join that rests on names alone stays below the
publishing line. For a SHORT list of those pairs this module asks the connected database four counts and nothing else:

    rows read from the child column (a bounded sample) | how many are not null | how many distinct | how many of those distinct
    values exist in the parent key

``supported`` evidence (nearly every child value exists in the parent key, from enough distinct values) is added to the edge as the
independent witness ``data_match``; with the name evidence that carries an edge past the publishing threshold. ``contradicted``
evidence (most child values do not exist in the parent) keeps a soft edge out of ``relationships.json`` and out of route building.
Anything else leaves the edge as it was. Same column names alone never publish: ``data_match`` can only be added to an edge another
rule already proposed, and one witness of this weight is below the threshold by itself.

Safety, each point tested:

* **Aggregates only.** The statement returns four integers. No identifier, key or row value is selected, stored, logged or sent to a model.
  Errors are recorded by exception class only (a database message can quote a value), and the database client's own error log is
  redacted while a probe runs.
* **Read-only.** The statement is built with sqlglot, checked to be one SELECT, and run through ``run_readonly_query`` (read-only
  connection, hard timeout). Identifiers are quoted per dialect from the catalog, never from text.
* **Bounded.** Only a short list is checked (never every column against every other): edges another rule proposed, whose parent
  column is a key, whose types can match, in the schema the prompts see. Budgets per sync: candidates, live queries, total time,
  per-query time, and a stop after repeated failures. The child side is a sample (first rows the engine returns), so a huge table
  costs the same as a small one.
* **Cached.** Results are kept in ``schema/relationship_evidence.json`` by structural fingerprint of both tables, so an unchanged schema
  costs no query on the next sync. Entries expire (``CACHE_TTL_DAYS``) because data drifts; failures expire sooner.
* **Never fatal.** Any failure becomes a report; the edges are then exactly what Phase 6 produced without data.

Declared foreign keys and procedure-derived joins are facts already: they are never queried, never blocked.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from src.sql import registry
from src.sql.dialects import get_dialect_profile
from src.sql.relationships.candidates import WEIGHTS, Edge
from src.sql.relationships.model import CatalogGraph, Column, Table
from src.sql.relationships.writer import HIGH_THRESHOLD

logger = logging.getLogger(__name__)

EVIDENCE_REL = "schema/relationship_evidence.json"
GENERATED_BY = "auto_relationships_evidence"
EVIDENCE_VERSION = 1

# ---- budgets, per sync
MAX_CANDIDATES = 300  # pairs considered
MAX_QUERIES = 150  # live queries; a cached pair costs none
DATA_BUDGET_SECONDS = 60.0  # all probes together
QUERY_TIMEOUT_SECONDS = 15.0  # one probe
MAX_CONSECUTIVE_ERRORS = 5  # a database that keeps refusing is left alone
SAMPLE_ROWS = 50_000  # child rows read per pair
CACHE_TTL_DAYS = 14
ERROR_TTL_HOURS = 24

# ---- reading the counts
MIN_NONNULL_ROWS = 30  # a nearly empty column proves nothing
MIN_DISTINCT = 20  # a column with a handful of values fits almost any key by chance
STRONG_COVERAGE = 0.98  # share of distinct child values found in the parent key
CONTRADICTED_COVERAGE = 0.50  # below this, most child values have no parent
MIN_PARENT_USAGE = 0.50  # a small lookup table the child values mostly cover is informative with few distinct values
MIN_CONTRADICTING_DISTINCT = 3

_KEY_FAMILIES = frozenset({"integer", "numeric", "string", "uuid"})  # types worth comparing as keys

Runner = Callable[..., Awaitable[list[dict[str, Any]]]]


# ---------------------------------------------------------------------------------------------------- facts and reading them


@dataclass(frozen=True)
class Facts:
    """The four counts for one pair, plus what the catalog already knows about the parent."""

    total_rows: int  # rows read from the child (at most SAMPLE_ROWS)
    nonnull_rows: int
    child_distinct: int
    matched_distinct: int  # distinct child values that exist in the parent key
    parent_unique: str | None  # primary | unique | unique_index: from the catalog, not measured
    parent_rows_estimate: int | None  # the engine's own statistics

    @property
    def truncated(self) -> bool:
        return self.total_rows >= SAMPLE_ROWS

    @property
    def null_ratio(self) -> float:
        return round(1 - self.nonnull_rows / self.total_rows, 4) if self.total_rows else 0.0

    @property
    def coverage(self) -> float:
        return round(self.matched_distinct / self.child_distinct, 4) if self.child_distinct else 0.0

    @property
    def parent_usage(self) -> float | None:
        """Share of the parent's rows the child values reach. Only meaningful when the whole child was read."""
        if self.truncated or not self.parent_rows_estimate:
            return None
        return round(min(self.matched_distinct / self.parent_rows_estimate, 1.0), 4)

    @property
    def cardinality_estimate(self) -> str:
        if self.nonnull_rows and self.child_distinct == self.nonnull_rows:
            return "one_to_one" if not self.truncated else "unknown"
        return "many_to_one"

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_rows": self.total_rows,
            "truncated": self.truncated,
            "null_ratio": self.null_ratio,
            "child_distinct": self.child_distinct,
            "matched_distinct": self.matched_distinct,
            "coverage": self.coverage,
            "parent_unique": self.parent_unique,
            "parent_rows_estimate": self.parent_rows_estimate,
            "parent_usage": self.parent_usage,
            "cardinality_estimate": self.cardinality_estimate,
            "direction": "child_subset_of_parent" if self.coverage >= STRONG_COVERAGE else None,
        }


def classify(f: Facts) -> str:
    """``supported`` | ``contradicted`` | ``partial`` | ``insufficient``. Pure arithmetic on the counts."""
    if f.nonnull_rows < MIN_NONNULL_ROWS or f.child_distinct < 2:
        return "insufficient"
    usage = f.parent_usage
    informative = f.child_distinct >= MIN_DISTINCT or (usage is not None and usage >= MIN_PARENT_USAGE)
    if f.coverage >= STRONG_COVERAGE:
        return "supported" if informative else "partial"
    if f.coverage < CONTRADICTED_COVERAGE and f.child_distinct >= MIN_CONTRADICTING_DISTINCT:
        return "contradicted"
    return "partial"


# ---------------------------------------------------------------------------------------------------- the statement


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _table(t: Table, alias: str | None = None) -> exp.Table:
    node = exp.Table(this=_ident(t.name), db=_ident(t.schema) if t.schema else None)
    if alias:
        node.set("alias", exp.TableAlias(this=_ident(alias)))
    return node


def build_probe_sql(child: Table, child_column: str, parent: Table, parent_column: str, dialect: str, sample_rows: int = SAMPLE_ROWS) -> str:
    """One SELECT returning four counts. The parent column is a key, so the left join cannot multiply child rows."""
    inner = exp.select(exp.alias_(exp.column(child_column, quoted=True), "c", quoted=True)).from_(_table(child)).limit(sample_rows)
    sample = exp.Subquery(this=inner, alias=exp.TableAlias(this=_ident("s")))
    c = exp.column("c", table="s", quoted=True)
    k = exp.column(parent_column, table="p", quoted=True)
    found = exp.Case(ifs=[exp.If(this=exp.Not(this=exp.Is(this=k.copy(), expression=exp.Null())), true=c.copy())])
    query = exp.select(
        exp.alias_(exp.Count(this=exp.Star()), "total_rows"),
        exp.alias_(exp.Count(this=c.copy()), "nonnull_rows"),
        exp.alias_(exp.Count(this=exp.Distinct(expressions=[c.copy()])), "child_distinct"),
        exp.alias_(exp.Count(this=exp.Distinct(expressions=[found])), "matched_distinct"),
    ).from_(sample).join(_table(parent, "p"), on=exp.EQ(this=k.copy(), expression=c.copy()), join_type="left")
    sql = query.sql(dialect=dialect)
    tree = sqlglot.parse_one(sql, read=dialect)  # defence in depth: it must be exactly one SELECT
    if not isinstance(tree, exp.Select) or any(tree.find(n) for n in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command)):
        raise ValueError("the probe is not a single SELECT")
    return sql


def _parse_counts(rows: Any) -> tuple[int, int, int, int] | None:
    """Four non-negative integers in a consistent order, or None. Column names are matched in lower case (Oracle upper-cases them)."""
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = {str(k).lower(): v for k, v in rows[0].items()}
    try:
        total, nonnull, distinct, matched = (int(row[k]) for k in ("total_rows", "nonnull_rows", "child_distinct", "matched_distinct"))
    except (KeyError, TypeError, ValueError):
        return None
    ok = 0 <= matched <= distinct <= nonnull <= total
    return (total, nonnull, distinct, matched) if ok else None


# ---------------------------------------------------------------------------------------------------- the short list


def table_fingerprint(t: Table) -> str:
    """Structure only (names, types, keys, indexes). Row counts are left out: they change on every sync."""
    raw = json.dumps(
        [t.qualified, [(c.name, c.type) for c in t.columns], list(t.primary_key), [list(k) for k in t.unique_keys],
         [(i.name, list(i.columns), i.unique) for i in t.indexes]],
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def pair_key(e: Edge) -> str:
    return f"{e.child}.{e.child_column} -> {e.parent}.{e.parent_column}".lower()


def _usable(c: Column | None) -> bool:
    return c is not None and c.family in _KEY_FAMILIES


def shortlist(graph: CatalogGraph, edges: list[Edge], *, threshold: float = HIGH_THRESHOLD, limit: int | None = None) -> list[Edge]:
    """The edges worth a query: proposed by names or structure only, parent column is a key, types fit, in the prompts' schema.

    Edges already established by a declared key or a procedure are skipped (facts need no check). Those that cannot reach the
    publishing line even with data are not skipped when they already sit at or above it: a soft edge may be refuted. Order: the
    ones data could promote first, then the ones data could only refute, each best score first.
    """
    if graph.multi_schema and not graph.scope_known:
        return []  # select_applied holds every edge in this case: nothing the data says could change what is published
    picked: list[Edge] = []
    for e in edges:
        child, parent = graph.tables.get(e.child), graph.tables.get(e.parent)
        if child is None or parent is None or child.is_view or parent.is_view:
            continue
        if e.has_hard_evidence or not e.direction_known or "data_match" in e.evidence:
            continue
        if parent.key_strength(e.parent_column) is None:
            continue
        if not (_usable(child.column(e.child_column)) and _usable(parent.column(e.parent_column))):
            continue
        if not (graph.in_answering_scope(child) and graph.in_answering_scope(parent)):
            continue
        picked.append(e)
    picked.sort(key=lambda e: (e.score >= threshold, -e.score, e.key))
    return picked[: MAX_CANDIDATES if limit is None else limit]


# ---------------------------------------------------------------------------------------------------- the cache file


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_cache(path: Path) -> tuple[dict[str, dict[str, Any]], bool]:
    """``(entries, may_write)``. A file this module did not write is never read from or overwritten."""
    if not path.exists():
        return {}, True
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, False
    if not (isinstance(doc, dict) and doc.get("generated_by") == GENERATED_BY):
        return {}, False
    if doc.get("version") != EVIDENCE_VERSION or not isinstance(doc.get("entries"), dict):
        return {}, True  # ours but from another version: start over
    return {k: v for k, v in doc["entries"].items() if isinstance(v, dict)}, True


def _fresh(entry: dict[str, Any], child_fp: str, parent_fp: str, now: datetime) -> bool:
    try:
        checked = datetime.strptime(str(entry["checked_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return False
    ttl = timedelta(hours=ERROR_TTL_HOURS) if entry.get("status") == "error" else timedelta(days=CACHE_TTL_DAYS)
    same = entry.get("child_fp") == child_fp and entry.get("parent_fp") == parent_fp and entry.get("sample_rows_limit") == SAMPLE_ROWS
    known = entry.get("status") in ("supported", "partial", "contradicted", "insufficient", "error")
    return same and known and now - checked < ttl and now >= checked


# ---------------------------------------------------------------------------------------------------- redacting the client's error log

_PROBING: contextvars.ContextVar[bool] = contextvars.ContextVar("relationship_probe_running", default=False)


class _WithholdProbeErrors(logging.Filter):
    """While a probe of THIS task runs, the database client's error log says that it failed and nothing more.

    A database message can quote a value from the data. The context variable keeps other tasks' logging untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _PROBING.get():
            record.msg, record.args, record.exc_info, record.exc_text = "Relationship probe failed (details withheld)", (), None, None
        return True


def _install_log_filter() -> None:
    target = logging.getLogger("src.core.db_client")
    if not any(isinstance(f, _WithholdProbeErrors) for f in target.filters):
        target.addFilter(_WithholdProbeErrors())


async def _default_runner(sql: str, *, max_rows: int, db_id: str) -> list[dict[str, Any]]:
    from src.core.db_client import run_readonly_query

    return await run_readonly_query(sql, max_rows=max_rows, db_id=db_id)


# ---------------------------------------------------------------------------------------------------- collecting and applying


@dataclass
class Outcome:
    report: dict[str, Any]  # counts only
    key: str = ""  # changes when the data decisions on edges change; part of the written files' input key


def _entry(e: Edge, child: Table, parent: Table, status: str, now: datetime, facts: Facts | None = None, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "child": e.child, "child_column": e.child_column, "parent": e.parent, "parent_column": e.parent_column,
        "child_fp": table_fingerprint(child), "parent_fp": table_fingerprint(parent), "checked_at": _stamp(now),
        "sample_rows_limit": SAMPLE_ROWS, "status": status,
    }
    if facts is not None:
        out["facts"] = facts.as_dict()
    if error:
        out["error"] = error  # an exception class name, never a database message
    return out


def apply_evidence(edges: list[Edge], entries: dict[str, dict[str, Any]], *, threshold: float = HIGH_THRESHOLD) -> dict[str, int]:
    """Put the data decisions on the edges. Returns ``{"promoted": n, "blocked": n, "disambiguated": n}``."""
    promoted = blocked = 0
    for e in edges:
        entry = entries.get(pair_key(e))
        if entry is None:
            continue
        status = str(entry.get("status"))
        before = e.score
        e.data_check = status
        if isinstance(entry.get("facts"), dict):
            e.data_facts = {**entry["facts"], "checked_at": entry.get("checked_at")}
        if status == "supported":
            e.add("data_match", WEIGHTS["data_match"])
            promoted += before < threshold <= e.score
        elif status == "contradicted":
            blocked += before >= threshold
    return {"promoted": promoted, "blocked": blocked, "disambiguated": _disambiguate(edges)}


def _disambiguate(edges: list[Edge]) -> int:
    """One column that could point at several parents: the data may pick one. Only when exactly one parent is supported and the
    data refuted every other candidate; a partial or unchecked rival leaves the column ambiguous."""
    by_col: dict[tuple[str, str], list[Edge]] = {}
    for e in edges:
        if e.ambiguous:
            by_col.setdefault((e.child, e.child_column), []).append(e)
    cleared = 0
    for members in by_col.values():
        supported = [e for e in members if e.data_check == "supported"]
        others = [e for e in members if e.data_check != "supported"]
        if len(supported) == 1 and others and all(o.data_check == "contradicted" for o in others) and not any(e.has_hard_evidence for e in others):
            supported[0].ambiguous = False
            cleared += 1
    return cleared


def decision_key(edges: list[Edge]) -> str:
    """A short hash of what the data decided, for the written files' input key. Empty when no edge was checked."""
    decided = sorted([e.key, e.data_check, "data_match" in e.evidence, e.ambiguous] for e in edges if e.data_check)
    return hashlib.sha256(json.dumps(decided).encode("utf-8")).hexdigest()[:16] if decided else ""


async def collect_evidence(
    db_id: str,
    graph: CatalogGraph,
    edges: list[Edge],
    *,
    runner: Runner | None = None,
    databases_dir: Path | None = None,
    threshold: float = HIGH_THRESHOLD,
    now: datetime | None = None,
) -> Outcome:
    """Check the short list against the database, cache the counts, and apply them to ``edges`` in place. Never raises."""
    started = time.monotonic()
    report: dict[str, Any] = {"ok": True}
    try:
        return await _collect(db_id, graph, edges, runner or _default_runner, databases_dir, threshold, now or _now(), started, report)
    except Exception as exc:  # noqa: BLE001 - optional evidence must never fail the sync
        logger.warning("Relationship data checks failed for %s: %s", db_id, type(exc).__name__)
        return Outcome({"ok": False, "error": type(exc).__name__, "elapsed_s": round(time.monotonic() - started, 2)})


async def _collect(
    db_id: str, graph: CatalogGraph, edges: list[Edge], runner: Runner, databases_dir: Path | None, threshold: float, now: datetime,
    started: float, report: dict[str, Any],
) -> Outcome:
    engine = graph.engine or ""
    try:
        dialect = get_dialect_profile(engine).sqlglot_dialect
    except ValueError:
        return Outcome({"ok": True, "skipped": "the database engine is unknown", "elapsed_s": 0.0})
    picked = shortlist(graph, edges, threshold=threshold)
    report.update({"shortlisted": len(picked), "cached": 0, "queried": 0, "errors": 0, "not_checked": 0})
    if not picked:
        return Outcome({**report, "skipped": "no pair needs a data check", "elapsed_s": round(time.monotonic() - started, 2)})

    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    path = folder / EVIDENCE_REL
    cached, may_write = _read_cache(path)
    entries: dict[str, dict[str, Any]] = {}
    todo: list[Edge] = []
    for e in picked:
        child, parent = graph.tables[e.child], graph.tables[e.parent]
        old = cached.get(pair_key(e))
        if old is not None and _fresh(old, table_fingerprint(child), table_fingerprint(parent), now):
            entries[pair_key(e)] = old
            report["cached"] += 1
        else:
            todo.append(e)

    _install_log_filter()
    deadline = started + DATA_BUDGET_SECONDS
    streak = 0
    stopped: str | None = None
    for position, e in enumerate(todo):
        remaining = deadline - time.monotonic()
        if report["queried"] >= MAX_QUERIES:
            stopped = "query budget used"
        elif remaining <= 0:
            stopped = "time budget used"
        elif streak >= MAX_CONSECUTIVE_ERRORS:
            stopped = "repeated failures"
        if stopped:
            report["not_checked"] = len(todo) - position
            break
        child, parent = graph.tables[e.child], graph.tables[e.parent]
        report["queried"] += 1
        entry = await _probe(e, child, parent, dialect, runner, db_id, min(QUERY_TIMEOUT_SECONDS, remaining), now)
        entries[pair_key(e)] = entry
        if entry["status"] == "error":
            report["errors"] += 1
            streak += 1
        else:
            streak = 0
    if stopped:
        report["stopped"] = stopped

    applied = apply_evidence(edges, entries, threshold=threshold)
    statuses = [str(v.get("status")) for v in entries.values()]
    report.update({s: statuses.count(s) for s in ("supported", "partial", "contradicted", "insufficient")}, **applied)
    if may_write and report["queried"]:
        _write_cache(path, entries, report)
    elif not may_write:
        report["cache"] = "a file not written by this phase is present; left untouched"
    report["elapsed_s"] = round(time.monotonic() - started, 2)
    return Outcome(report, decision_key(edges))


async def _probe(e: Edge, child: Table, parent: Table, dialect: str, runner: Runner, db_id: str, timeout: float, now: datetime) -> dict[str, Any]:
    token = _PROBING.set(True)
    try:
        sql = build_probe_sql(child, e.child_column, parent, e.parent_column, dialect)
        rows = await asyncio.wait_for(runner(sql, max_rows=2, db_id=db_id), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - recorded by class name only
        return _entry(e, child, parent, "error", now, error=type(exc).__name__)
    finally:
        _PROBING.reset(token)
    counts = _parse_counts(rows)
    if counts is None:
        return _entry(e, child, parent, "error", now, error="UnexpectedResult")
    total, nonnull, distinct, matched = counts
    est = parent.row_estimate if isinstance(parent.row_estimate, int) and parent.row_estimate > 0 else None
    facts = Facts(total, nonnull, distinct, matched, parent.key_strength(e.parent_column), est)
    return _entry(e, child, parent, classify(facts), now, facts)


def _write_cache(path: Path, entries: dict[str, dict[str, Any]], report: dict[str, Any]) -> None:
    doc = {
        "generated_by": GENERATED_BY,
        "generated_at": _stamp(_now()),
        "note": "Aggregate counts from checking likely joins against the database (no values). For review: prompts never read this file.",
        "version": EVIDENCE_VERSION,
        "entries": dict(sorted(entries.items())),
    }
    try:
        registry.atomic_write_json(path, doc, mode=0o644)
    except OSError as exc:
        report["cache"] = f"not saved ({type(exc).__name__})"


__all__ = [
    "EVIDENCE_REL", "GENERATED_BY", "Facts", "Outcome", "apply_evidence", "build_probe_sql", "classify", "collect_evidence",
    "decision_key", "pair_key", "shortlist", "table_fingerprint",
]
