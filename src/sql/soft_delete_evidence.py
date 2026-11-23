"""Evidence-based soft-delete protection (metadata-driven SQL accuracy, Release 4).

``soft_delete_spec`` finds a table's "deleted" column from its NAME and TYPE and the application then filters on it. That is right
when the convention is the usual one and silently wrong when it is not: a column named ``deleted`` that is really a counter makes
``deleted = 0`` drop valid rows, a ``removed VARCHAR`` that holds codes is never filtered at all, and nobody is told. This module
adds one more kind of evidence, ASKED OF THE DATABASE WITH AGGREGATE COUNTS ONLY, and classifies every table as one of::

    known active-row condition      (declared, curated, atlas, discovered by name+type, or verified by the data)
    no soft-delete marker           (no likely column, or the data shows it is not a marker)
    uncertain possible marker       (a likely column whose convention the data does not prove)

What is asked: for each likely marker column of a table, over a bounded sample of its rows, how many rows there are, how many are
not NULL, how many distinct values, and how many equal the usual "deleted" and "live" spellings of its type (``'Y'``/``'N'``,
``1``/``0``, ``TRUE``/``FALSE``). Four or five integers. No value, key or row is selected, stored, logged or sent to a model.

Reading the counts (``classify``): a flag is ``supported`` only when every non-NULL value is one of the two usual spellings and at
least one row is live; a timestamp only when some rows are NULL (a column that is never NULL would make ``IS NULL`` drop every
row). A flag holding a few other codes is ``uncertain``; one with many distinct values is a counter or free text, not a marker.
Too few rows prove nothing (``insufficient``).

What it changes, per database, in stages (each flag starts off):

* ``auto_soft_delete_evidence_apply``: SHADOW. The sync asks the questions above and writes ``semantics/soft_delete_evidence.json``;
  at question time the guard only LOGS what it would have done (``soft_delete_guard_shadow``). No answer changes.
* ``auto_soft_delete_guard_apply``: ENFORCE (needs the flag above). A column the data verified becomes a filter like any other
  (``source = "verified"``); a name+type guess that the data contradicts is no longer enforced; and a query on a table with an
  UNCERTAIN marker is refused with a plain message instead of risking deleted rows or dropping valid ones.

Declared, curated and atlas specs are facts: never queried, never demoted. Name+type discovery is unchanged and stays the fallback.
With both flags off nothing here runs and nothing changes.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.soft_delete_spec import (
    _STRONG_FLAG_NAMES,
    _TIMESTAMP_NAMES,
    _WEAK_FLAG_NAMES,
    SoftDeleteSpec,
    _norm,
    _type_family,
    classify_column,
)

logger = logging.getLogger(__name__)

EVIDENCE_REL = "semantics/soft_delete_evidence.json"
GENERATED_BY = "auto_soft_delete_evidence"
EVIDENCE_VERSION = 1

# ---- budgets, per sync (the same shape as the relationship data checks)
MAX_QUERIES = 200
DATA_BUDGET_SECONDS = 60.0
QUERY_TIMEOUT_SECONDS = 15.0
MAX_CONSECUTIVE_ERRORS = 5
SAMPLE_ROWS = 50_000
CACHE_TTL_DAYS = 14
ERROR_TTL_HOURS = 24

# ---- reading the counts
MIN_ROWS = 20  # fewer rows prove nothing
MAX_CODE_DISTINCT = 6  # a column with at most this many distinct values may hold codes; more is a counter or free text

# Names that are likely markers but that ``soft_delete_spec`` does not know (the master list adds ``rescinded``).
_RESCINDED_NAMES = frozenset(_norm(n) for n in ("rescinded", "is_rescinded", "rescinded_at", "rescinded_on", "rescinded_date", "record_rescinded"))
_FLAG_LAST_WORDS = frozenset({"deleted", "removed", "rescinded"})
_NEGATIONS = frozenset({"un", "not", "non", "no"})

STATUSES = ("supported", "contradicted", "uncertain", "insufficient", "error")
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------------- the likely columns


def likely_kind(name: str) -> str | None:
    """``"timestamp"`` | ``"flag"`` | None: what a column's NAME says it might be. A name is a reason to ask, never a proof."""
    from src.sql.relationships.names import split_identifier

    n = _norm(name)
    if n in _TIMESTAMP_NAMES or n in {"rescindedat", "rescindedon", "rescindeddate"}:
        return "timestamp"
    if n in _STRONG_FLAG_NAMES or n in _WEAK_FLAG_NAMES or n in _RESCINDED_NAMES:
        return "flag"
    parts = split_identifier(name)
    if len(parts) >= 2 and parts[-1] in _FLAG_LAST_WORDS and parts[0] not in _NEGATIONS:
        return "flag"  # record_deleted, RowRemoved: still only a candidate, judged by the data
    return None


@dataclass(frozen=True)
class Candidate:
    table: str  # lower case, as soft_delete_spec keys it
    schema: str | None
    column: str
    type: str
    name_kind: str  # "timestamp" | "flag"
    probe: str  # "timestamp" | "boolean" | "int" | "char" | "string_date"
    nullable: bool | None
    fingerprint: str  # structure of the table's columns: changes when the answer might


def _table_fingerprint(table: dict[str, Any]) -> str:
    cols = [
        [str(c.get("name")), str(c.get("type")), c.get("nullable"), str(c.get("default")), c.get("enum_values")]
        for c in table.get("columns") or [] if isinstance(c, dict)
    ]
    raw = json.dumps([str(table.get("name")), cols], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _probe_for(name_kind: str, type_text: str) -> str | None:
    fam = _type_family(type_text)
    if fam == "boolean":
        return "boolean"
    if fam in ("int", "smallint"):
        return "int"
    if fam == "timestamp":
        return "timestamp" if name_kind == "timestamp" else None  # a date column named like a flag is something else
    if fam in ("char", "other"):
        low = str(type_text).lower()
        if any(k in low for k in ("char", "text", "string", "enum")):
            return "string_date" if name_kind == "timestamp" else "char"
    return None


def candidates(schema_doc: Any) -> list[Candidate]:
    """Every likely marker column of every real table (views, keys and foreign keys left out), in catalog order."""
    out: list[Candidate] = []
    tables = schema_doc.get("tables") if isinstance(schema_doc, dict) else None
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict) or not table.get("name"):
            continue
        if str(table.get("kind") or "table").lower() in ("view", "materialized view"):
            continue
        pk = {str(c).lower() for c in table.get("primary_key") or []}
        fp = _table_fingerprint(table)
        for col in table.get("columns") or []:
            if not isinstance(col, dict) or not col.get("name") or str(col["name"]).lower() in pk or col.get("is_foreign_key"):
                continue
            kind = likely_kind(str(col["name"]))
            probe = _probe_for(kind, str(col.get("type") or "")) if kind else None
            if kind and probe:
                out.append(Candidate(
                    str(table["name"]).lower(), table.get("schema"), str(col["name"]), str(col.get("type") or ""), kind, probe,
                    col.get("nullable"), fp,
                ))
    return out


# ---------------------------------------------------------------------------------------------------- reading the counts


@dataclass(frozen=True)
class Facts:
    total_rows: int
    nonnull_rows: int
    distinct_values: int
    marked_like: int  # rows spelled the usual way for "deleted" ('Y', 1, TRUE)
    unmarked_like: int  # rows spelled the usual way for "live" ('N', 0, FALSE)

    @property
    def null_rows(self) -> int:
        return self.total_rows - self.nonnull_rows

    @property
    def other_rows(self) -> int:
        return self.nonnull_rows - self.marked_like - self.unmarked_like

    def as_dict(self) -> dict[str, int]:
        return {
            "sample_rows": self.total_rows, "null_rows": self.null_rows, "distinct_values": self.distinct_values,
            "marked_like": self.marked_like, "unmarked_like": self.unmarked_like, "other_rows": self.other_rows,
        }


def classify(c: Candidate, f: Facts) -> str:
    """``supported`` | ``contradicted`` | ``uncertain`` | ``insufficient``. Pure arithmetic on the counts and the column's type."""
    if f.total_rows < MIN_ROWS:
        return "insufficient"
    if c.probe == "timestamp":
        if c.nullable is False or f.nonnull_rows == f.total_rows:
            return "contradicted"  # never NULL: "IS NULL" would drop every row
        return "supported"
    if c.probe == "string_date":  # a date held in text: possibly a marker, but the data cannot show the text is a date
        if f.nonnull_rows == 0:
            return "insufficient"
        return "contradicted" if f.nonnull_rows == f.total_rows else "uncertain"
    if f.other_rows == 0 and f.marked_like + f.unmarked_like > 0:  # only the two usual spellings
        return "contradicted" if f.unmarked_like + f.null_rows == 0 else "supported"  # every row "deleted": the name is not the story
    if f.nonnull_rows == 0:
        return "insufficient"
    return "uncertain" if f.distinct_values <= MAX_CODE_DISTINCT else "contradicted"  # codes we cannot read / a counter or free text


def spec_kind(c: Candidate) -> str | None:
    """The soft-delete kind a SUPPORTED candidate is enforced as (the conditions are ``soft_delete_spec``'s own)."""
    if c.probe == "timestamp":
        return "timestamp"
    if c.probe == "boolean":
        return "boolean"
    if c.probe == "int":
        return "int_flag"
    if c.probe == "char":
        return "char_flag"
    return None


# ---------------------------------------------------------------------------------------------------- the evidence file


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_key(table: str, column: str) -> str:
    return f"{table}.{column}".lower()


def make_entry(c: Candidate, status: str, now: datetime, facts: Facts | None = None, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "table": c.table, "column": c.column, "probe": c.probe, "status": status, "table_fp": c.fingerprint,
        "checked_at": _stamp(now), "sample_rows_limit": SAMPLE_ROWS,
    }
    if facts is not None:
        out["facts"] = facts.as_dict()
    if error:
        out["error"] = error  # an exception class name: a database message can quote a value
    return out


def read_evidence(path: Path) -> tuple[dict[str, dict[str, Any]], bool]:
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
        return {}, True
    return {k: v for k, v in doc["entries"].items() if isinstance(v, dict)}, True


def fresh(entry: dict[str, Any], c: Candidate, now: datetime) -> bool:
    try:
        checked = datetime.strptime(str(entry["checked_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return False
    ttl = timedelta(hours=ERROR_TTL_HOURS) if entry.get("status") == "error" else timedelta(days=CACHE_TTL_DAYS)
    return (
        entry.get("table_fp") == c.fingerprint and entry.get("probe") == c.probe and entry.get("sample_rows_limit") == SAMPLE_ROWS
        and entry.get("status") in STATUSES and checked <= now < checked + ttl
    )


def write_evidence(db_id: str, entries: dict[str, dict[str, Any]], databases_dir: Path | None = None) -> str:
    """``"written"`` | ``"unchanged"`` | a short reason it was skipped. Atomic; a file this module did not write is left alone."""
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:
        return "pinned database"
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        return "database folder does not exist"
    path = folder / EVIDENCE_REL
    current, may_write = read_evidence(path)
    if not may_write:
        return "a file not written by this phase is present; left untouched"
    if current == entries and path.exists():
        return "unchanged"
    doc = {
        "generated_by": GENERATED_BY, "version": EVIDENCE_VERSION, "generated_at": _stamp(_now()),
        "note": "Aggregate counts that show how likely soft-delete columns are really used (no values). For review; never read by prompts.",
        "entries": dict(sorted(entries.items())),
    }
    registry.atomic_write_json(path, doc, mode=0o644)
    return "written"


# ---------------------------------------------------------------------------------------------------- the view: what the evidence means


@dataclass(frozen=True)
class EvidenceView:
    verified: dict[str, SoftDeleteSpec] = field(default_factory=dict)  # tables whose marker the data proved (none had a spec)
    uncertain: dict[str, str] = field(default_factory=dict)  # table -> "column (why)": possible marker the data does not prove
    demoted: dict[str, str] = field(default_factory=dict)  # table -> "column (why)": a name+type guess the data contradicts
    checked: int = 0  # entries read

    def risky(self) -> dict[str, str]:
        """Tables a query must not touch while the guard is enforced."""
        return {**self.uncertain, **self.demoted}


_PRIORITY = {"timestamp": 0, "boolean": 1, "int_flag": 2, "char_flag": 3}


def build_view(schema_doc: Any, base_specs: dict[str, SoftDeleteSpec], entries: dict[str, dict[str, Any]]) -> EvidenceView:
    """Combine the candidates, the specs already known and the evidence. Pure.

    A table with a curated, atlas or declared spec is a fact and is never touched. A name+type (``discovered``) spec stays unless the
    data of ITS column contradicts or cannot read it. A table with no spec is verified when a candidate is supported, uncertain when a
    candidate is uncertain, and otherwise has no marker (as before).
    """
    verified: dict[str, SoftDeleteSpec] = {}
    uncertain: dict[str, str] = {}
    demoted: dict[str, str] = {}
    by_table: dict[str, list[tuple[Candidate, dict[str, Any]]]] = {}
    for c in candidates(schema_doc):
        entry = entries.get(entry_key(c.table, c.column))
        if entry is not None and entry.get("table_fp") == c.fingerprint and entry.get("probe") == c.probe:
            by_table.setdefault(c.table, []).append((c, entry))
    for table, rows in by_table.items():
        base = base_specs.get(table)
        if base is not None and base.source != "discovered":
            continue  # a fact: curated, atlas or declared
        if base is not None:  # discovered by name + type: only its own column's evidence counts
            mine = [(c, e) for c, e in rows if c.column.lower() == base.column.lower()]
            for c, e in mine:
                status = str(e.get("status"))
                if status in ("contradicted", "uncertain"):
                    demoted[table] = f"{c.column} ({status}: the data does not match the usual {c.probe} convention)"
            continue
        supported = sorted(((c, e) for c, e in rows if e.get("status") == "supported" and spec_kind(c)), key=lambda r: _PRIORITY[spec_kind(r[0]) or "char_flag"])
        if supported:
            c = supported[0][0]
            kind = spec_kind(c)
            if kind and classify_column("deleted_at" if kind == "timestamp" else "is_deleted", c.type, nullable=c.nullable) is not None:
                verified[table] = SoftDeleteSpec(table, c.column, kind, "verified")
                continue
        unsure = [(c, e) for c, e in rows if e.get("status") == "uncertain"]
        if unsure:
            c = unsure[0][0]
            uncertain[table] = f"{c.column} (possible marker; its values do not match the usual convention)"
    return EvidenceView(verified, uncertain, demoted, sum(len(v) for v in by_table.values()))


_VIEW_CACHE: dict[str, tuple[tuple[Any, ...], EvidenceView]] = {}


def clear_view_cache(db_id: str | None = None) -> None:
    with _LOCK:
        if db_id is None:
            _VIEW_CACHE.clear()
        else:
            for key in [k for k in _VIEW_CACHE if k.endswith(f"/{db_id}")]:
                _VIEW_CACHE.pop(key, None)


def load_view(db_id: str, base_specs: dict[str, SoftDeleteSpec], *, databases_dir: Path | None = None) -> EvidenceView:
    """The evidence view of ``db_id``, rebuilt only when the catalog or the evidence file changes. Never raises."""
    try:
        validate_db_id(db_id)
        if db_id == DEFAULT_DB_ID:
            return EvidenceView()
        root = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
        files = [root / "schema" / "schema.json", root / EVIDENCE_REL]
        stamp: list[Any] = []
        for p in files:
            try:
                st = p.stat()
                stamp.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append(None)
        key = f"{root}"
        stamp_key = (*stamp, tuple(sorted((t, s.column, s.kind, s.source) for t, s in base_specs.items())))
        with _LOCK:
            cached = _VIEW_CACHE.get(key)
        if cached is not None and cached[0] == stamp_key:
            return cached[1]
        view = EvidenceView()
        if stamp[0] is not None and stamp[1] is not None:
            schema_doc = json.loads(files[0].read_text(encoding="utf-8"))
            entries, _ = read_evidence(files[1])
            view = build_view(schema_doc, base_specs, entries)
        with _LOCK:
            _VIEW_CACHE[key] = (stamp_key, view)
        return view
    except Exception as exc:  # noqa: BLE001 - without evidence the specs are exactly what they were
        logger.warning("Soft-delete evidence unavailable for %s: %s", db_id, type(exc).__name__)
        return EvidenceView()


def overlay(base_specs: dict[str, SoftDeleteSpec], view: EvidenceView) -> dict[str, SoftDeleteSpec]:
    """``base_specs`` with the data's verdicts applied: verified columns added, contradicted name+type guesses removed."""
    out = dict(base_specs)
    for table in view.demoted:
        if out.get(table) is not None and out[table].source == "discovered":
            del out[table]
    for table, spec in view.verified.items():
        out.setdefault(table, spec)
    return out


# ---------------------------------------------------------------------------------------------------- flags


def evidence_on(db_id: str) -> bool:
    try:
        from src.utils.feature_flags import is_db_feature_enabled

        return db_id != DEFAULT_DB_ID and bool(is_db_feature_enabled("auto_soft_delete_evidence_apply", db_id))
    except Exception:  # noqa: BLE001 - on any doubt: off
        return False


def guard_on(db_id: str) -> bool:
    """The guard is enforced only where the evidence is also on: a guard without evidence has nothing to enforce."""
    try:
        from src.utils.feature_flags import is_db_feature_enabled

        return evidence_on(db_id) and bool(is_db_feature_enabled("auto_soft_delete_guard_apply", db_id))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------------- the guard at question time

GUARD_ERROR_PREFIX = "soft_delete_guard:"


def _tables_in(sql: str, dialect: str) -> set[str]:
    import sqlglot
    import sqlglot.expressions as exp

    return {t.name.lower() for t in sqlglot.parse_one(sql, read=dialect).find_all(exp.Table) if t.name}


def guard_error(blocked: dict[str, str]) -> str:
    """The infra-style error string carried to the answer: ``soft_delete_guard:table=column (why);table=...`` (names only)."""
    return GUARD_ERROR_PREFIX + ";".join(f"{t}={why}" for t, why in sorted(blocked.items())[:5])


def guard_message(error: str) -> str:
    """The plain-language reply for a guarded query (names of tables and columns only)."""
    body = error.removeprefix(GUARD_ERROR_PREFIX)
    items = [p for p in body.split(";") if p][:3]
    names = "; ".join(f"{p.split('=', 1)[0]} (column {p.split('=', 1)[1].split(' ', 1)[0]})" if "=" in p else p for p in items)
    return (
        "I can't answer this reliably yet. " + (f"The data in {names} appears to mark deleted records, " if names else "A table in this question appears to mark deleted records, ")
        + "but the way it does so has not been confirmed, so I can't tell which rows are current. Answering anyway could include deleted "
        "records or leave out valid ones. Ask an administrator to confirm that column's convention, then ask again."
    )


def soft_delete_guard(sql: str, intent: str, dialect: str, db_id: str, *, databases_dir: Path | None = None) -> dict[str, str]:
    """The tables of ``sql`` that must not be queried now, ``{table: "column (why)"}``; empty when nothing is blocked.

    Evidence off: always empty, nothing logged. Evidence on and the guard off (shadow): empty, but what would have happened is logged
    (``soft_delete_guard_shadow``). Both on: the blocked tables are returned and logged (``soft_delete_guard_applied``). An audit or
    history question adds no filter and is never blocked. Never raises.
    """
    try:
        if not sql or intent == "INCLUDE_ARCHIVED" or not evidence_on(db_id):
            return {}
        from src.sql.soft_delete_spec import load_base_specs

        base = load_base_specs(db_id, databases_dir=databases_dir)
        view = load_view(db_id, base, databases_dir=databases_dir)
        touched = _tables_in(sql, dialect)
        risky = {t: why for t, why in view.risky().items() if t in touched}
        would_filter = sorted(t for t in view.verified if t in touched)
        enforcing = guard_on(db_id)
        if risky or would_filter:
            from src.utils.telemetry import log_telemetry

            log_telemetry(
                query_id="",
                stage="soft_delete_guard_applied" if enforcing else "soft_delete_guard_shadow",
                extra={"db_id": db_id, "intent": intent, "refused": sorted(risky) if enforcing else [], "would_refuse": sorted(risky),
                       "would_filter": would_filter},
            )
        return risky if enforcing else {}
    except Exception as exc:  # noqa: BLE001 - a guard that cannot run lets the query through exactly as before
        logger.debug("Soft-delete guard skipped (%s): %s", type(exc).__name__, exc)
        return {}


__all__ = [
    "EVIDENCE_REL",
    "GENERATED_BY",
    "GUARD_ERROR_PREFIX",
    "Candidate",
    "EvidenceView",
    "Facts",
    "build_view",
    "candidates",
    "classify",
    "clear_view_cache",
    "evidence_on",
    "guard_error",
    "guard_message",
    "guard_on",
    "likely_kind",
    "load_view",
    "overlay",
    "read_evidence",
    "soft_delete_guard",
    "write_evidence",
]
