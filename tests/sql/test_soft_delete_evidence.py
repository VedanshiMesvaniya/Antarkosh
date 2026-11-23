"""Release 4: evidence-based soft-delete protection (``soft_delete_evidence``, ``sa/soft_delete_phase`` and their wiring).

A real SQLite database is used wherever a statement is executed, so the generated SQL is run, not just parsed. Every schema here is a
small synthetic one; no real table or question is named.
"""

# ruff: noqa: F811, F401

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import sqlglot
import yaml
from sqlglot import exp

import src.sql.soft_delete_evidence as ev
import src.sql.soft_delete_spec as ss
from src.sql import registry
from src.sql.sa import soft_delete_phase as ph
from tests.sql.test_catalog_extraction import _sync_args, dbs, sqlite_db
from tests.sql.test_resync_e2e import DB, _flags, _sync, shop

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
N = 30  # rows per table: above MIN_ROWS


# ----------------------------------------------------------------------------------------------- a real database to ask


def _table_doc(name: str, columns: list[tuple[str, str]], *, kind: str = "table", **extra: Any) -> dict[str, Any]:
    cols = [{"name": "id", "type": "INTEGER", "is_primary_key": True}]
    for col_name, col_type in columns:
        cols.append({"name": col_name, "type": col_type, "is_primary_key": False})
    return {"name": name, "schema": None, "qualified_name": name, "kind": kind, "primary_key": ["id"], "unique_keys": [], "indexes": [],
            "comment": None, "columns": cols, **extra}


SCHEMA: list[tuple[str, list[tuple[str, str]]]] = [
    ("orders", [("record_deleted", "CHAR(1)")]),  # N/Y pattern: name+type already finds it, the data agrees
    ("invoices", [("is_deleted", "BIT")]),  # 0/1: found by name+type, the data agrees
    ("shipments", [("deleted_at", "DATETIME")]),  # NULL / non-NULL: found, the data agrees
    ("legacy", [("deleted", "INT")]),  # a weak name on an int: NOT found today; the data proves 0/1
    ("tickets", [("rescinded", "CHAR(1)")]),  # an unknown name: NOT found today; the data proves Y/N
    ("catalogue", [("removed", "VARCHAR(10)")]),  # a vague varchar holding codes: possible marker, not proven
    ("counters", [("deleted", "INT")]),  # a counter named deleted: not a marker
    ("audit_log", [("deleted_at", "DATETIME")]),  # found by name+type, but the data says it is never NULL
    ("archive", [("is_deleted", "INT")]),  # found by name+type, but every row is "deleted"
    ("tiny", [("is_deleted", "INT")]),  # too few rows to say
    ("vdate", [("deleted_on", "VARCHAR(30)")]),  # a date held as text: possible marker, not provable
    ("plain", [("name", "VARCHAR(20)")]),  # no marker at all
    ("hist", [("gone", "INT"), ("is_deleted", "INT")]),  # has a curated spec: a fact, never queried
]


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    for name, cols in SCHEMA:
        con.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, " + ", ".join(f"{c} {t}" for c, t in cols) + ")")
    rows = range(1, N + 1)
    con.executemany("INSERT INTO orders(id, record_deleted) VALUES (?, ?)", [(i, "Y" if i % 5 == 0 else "N") for i in rows])
    con.executemany("INSERT INTO invoices(id, is_deleted) VALUES (?, ?)", [(i, 1 if i % 4 == 0 else 0) for i in rows])
    con.executemany("INSERT INTO shipments(id, deleted_at) VALUES (?, ?)", [(i, "2024-01-01" if i % 6 == 0 else None) for i in rows])
    con.executemany("INSERT INTO legacy(id, deleted) VALUES (?, ?)", [(i, i % 2) for i in rows])
    con.executemany("INSERT INTO tickets(id, rescinded) VALUES (?, ?)", [(i, "Y" if i % 3 == 0 else "N") for i in rows])
    con.executemany("INSERT INTO catalogue(id, removed) VALUES (?, ?)", [(i, "AIX"[i % 3]) for i in rows])
    con.executemany("INSERT INTO counters(id, deleted) VALUES (?, ?)", [(i, i) for i in rows])
    con.executemany("INSERT INTO audit_log(id, deleted_at) VALUES (?, ?)", [(i, "2024-02-02") for i in rows])
    con.executemany("INSERT INTO archive(id, is_deleted) VALUES (?, ?)", [(i, 1) for i in rows])
    con.executemany("INSERT INTO tiny(id, is_deleted) VALUES (?, ?)", [(i, i % 2) for i in range(1, 6)])
    con.executemany("INSERT INTO vdate(id, deleted_on) VALUES (?, ?)", [(i, "2024-03-03" if i % 5 == 0 else None) for i in rows])
    con.executemany("INSERT INTO plain(id, name) VALUES (?, ?)", [(i, f"p{i}") for i in rows])
    con.executemany("INSERT INTO hist(id, gone, is_deleted) VALUES (?, ?, ?)", [(i, i % 2, 0) for i in rows])
    con.commit()
    con.close()


def _schema_doc(extra_tables: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tables = [_table_doc(n, c) for n, c in SCHEMA] + [_table_doc("v_orders", [("is_deleted", "INT")], kind="view")]
    return {"engine": "sqlite", "default_schema": None, "tables": tables + (extra_tables or [])}


def _sqlite_runner(path: Path, log: list[str] | None = None):
    async def run(sql: str, *, max_rows: int, db_id: str) -> list[dict[str, Any]]:
        if log is not None:
            log.append(sql)

        def work() -> list[dict[str, Any]]:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only, like the application's own connector
            try:
                cur = con.execute(sql)
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row, strict=True)) for row in cur.fetchmany(max_rows)]
            finally:
                con.close()

        return await asyncio.to_thread(work)

    return run


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A databases folder holding ``DB`` with the schema, a curated spec for ``hist``, and the SQLite file to ask."""
    db = tmp_path / "world.db"
    _make_db(db)
    root = tmp_path / "dbs"
    for sub in ("schema", "semantics"):
        (root / DB / sub).mkdir(parents=True)
    (root / DB / "schema" / "schema.json").write_text(json.dumps(_schema_doc()), encoding="utf-8")
    (root / DB / ss.CURATED_REL).write_text(json.dumps({"tables": {"hist": {"column": "is_deleted", "kind": "int_flag"}}}), encoding="utf-8")
    monkeypatch.setattr(registry, "DATABASES_DIR", root)
    ss.clear_spec_cache()
    ev.clear_view_cache()
    yield db, root
    ss.clear_spec_cache()
    ev.clear_view_cache()


def _collect(root: Path, db: Path, log: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    return asyncio.run(ph.run_soft_delete_phase(db_id=DB, databases_dir=root, runner=kw.pop("runner", None) or _sqlite_runner(db, log), now=kw.pop("now", NOW), **kw))


def _evidence(root: Path) -> dict[str, Any]:
    return json.loads((root / DB / ev.EVIDENCE_REL).read_text(encoding="utf-8"))


def _flags_on(monkeypatch: pytest.MonkeyPatch, *on: str) -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in on)


# ----------------------------------------------------------------------------------------------- the likely columns


@pytest.mark.parametrize("name", ["deleted_at", "deleted_on", "is_deleted", "record_deleted", "deleted", "removed", "rescinded", "IsDeleted",
                                  "RowRemoved", "is_rescinded", "rescinded_at", "del_flag"])
def test_the_master_lists_names_are_likely_markers(name: str) -> None:
    assert ev.likely_kind(name) in ("timestamp", "flag")


@pytest.mark.parametrize("name", ["deleted_by", "deleted_user_id", "deleted_count", "rows_deleted_total", "undeleted", "not_deleted", "name", "id", "removed_reason"])
def test_names_that_are_not_markers_are_not_candidates(name: str) -> None:
    assert ev.likely_kind(name) is None


def test_the_kind_a_name_suggests() -> None:
    assert ev.likely_kind("deleted_at") == "timestamp" and ev.likely_kind("rescinded_on") == "timestamp"
    assert ev.likely_kind("is_deleted") == "flag" and ev.likely_kind("rescinded") == "flag" and ev.likely_kind("record_deleted") == "flag"


def test_candidates_skip_views_keys_foreign_keys_and_columns_whose_type_cannot_be_probed() -> None:
    doc = _schema_doc([
        _table_doc("with_fk", [("deleted", "INT")], columns_extra=None) | {"columns": [
            {"name": "id", "type": "INT", "is_primary_key": True},
            {"name": "deleted", "type": "INT", "is_foreign_key": True},  # a foreign key is never a marker
            {"name": "removed", "type": "BLOB"},  # not a type a flag can have
            {"name": "is_deleted", "type": "DATE"},  # a date named like a flag is something else
        ]},
    ])
    found = {(c.table, c.column, c.probe) for c in ev.candidates(doc)}
    assert ("v_orders", "is_deleted", "int") not in found  # a view
    assert not any(t == "with_fk" for t, _c, _p in found)
    assert {("orders", "record_deleted", "char"), ("invoices", "is_deleted", "int"), ("shipments", "deleted_at", "timestamp"),
            ("tickets", "rescinded", "char"), ("vdate", "deleted_on", "string_date"), ("catalogue", "removed", "char")} <= found
    assert ev.candidates(None) == [] and ev.candidates({"tables": "x"}) == []


# ----------------------------------------------------------------------------------------------- reading the counts


def _cand(probe: str, *, nullable: bool | None = None, type_: str = "T") -> ev.Candidate:
    return ev.Candidate("t", None, "c", type_, "flag" if probe != "timestamp" else "timestamp", probe, nullable, "fp")


def _f(total: int = 100, nonnull: int = 100, distinct: int = 2, marked: int = 20, unmarked: int = 80) -> ev.Facts:
    return ev.Facts(total, nonnull, distinct, marked, unmarked)


@pytest.mark.parametrize(
    ("candidate", "facts", "status"),
    [
        (_cand("char"), _f(), "supported"),  # only 'Y' and 'N', some live
        (_cand("char"), _f(nonnull=90, marked=10, unmarked=80), "supported"),  # NULL rows count as live
        (_cand("char"), _f(marked=0, unmarked=100, distinct=1), "supported"),  # nothing deleted yet: still the usual convention
        (_cand("int"), _f(marked=100, unmarked=0, distinct=1), "contradicted"),  # every row "deleted": the name is not the story
        (_cand("boolean"), _f(), "supported"),
        (_cand("char"), _f(distinct=4, marked=10, unmarked=60), "uncertain"),  # other codes: a marker we cannot read
        (_cand("char"), _f(distinct=3, marked=30, unmarked=30), "uncertain"),
        (_cand("int"), _f(distinct=50, marked=10, unmarked=10), "contradicted"),  # a counter or free text
        (_cand("char"), _f(total=19, nonnull=19, marked=5, unmarked=14), "insufficient"),
        (_cand("char"), _f(nonnull=0, distinct=0, marked=0, unmarked=0), "insufficient"),  # all NULL: nothing to read
        (_cand("timestamp", nullable=True), _f(nonnull=10, distinct=0, marked=10, unmarked=0), "supported"),
        (_cand("timestamp", nullable=True), _f(nonnull=0, distinct=0, marked=0, unmarked=0), "supported"),  # nothing deleted yet
        (_cand("timestamp", nullable=True), _f(nonnull=100, distinct=0, marked=100, unmarked=0), "contradicted"),  # "IS NULL" would drop every row
        (_cand("timestamp", nullable=False), _f(nonnull=10, distinct=0, marked=10, unmarked=0), "contradicted"),
        (_cand("string_date"), _f(nonnull=10, distinct=0, marked=10, unmarked=0), "uncertain"),  # NULL and text: a date we cannot verify
        (_cand("string_date"), _f(nonnull=0, distinct=0, marked=0, unmarked=0), "insufficient"),
        (_cand("string_date"), _f(nonnull=100, distinct=0, marked=100, unmarked=0), "contradicted"),
    ],
)
def test_classification_of_the_counts(candidate: ev.Candidate, facts: ev.Facts, status: str) -> None:
    assert ev.classify(candidate, facts) == status


def test_the_bounds_of_the_rules_are_the_documented_ones() -> None:
    assert ev.MIN_ROWS == 20 and ev.MAX_CODE_DISTINCT == 6
    assert ev.classify(_cand("char"), _f(distinct=ev.MAX_CODE_DISTINCT, marked=10, unmarked=60)) == "uncertain"
    assert ev.classify(_cand("char"), _f(distinct=ev.MAX_CODE_DISTINCT + 1, marked=10, unmarked=60)) == "contradicted"
    assert ev.classify(_cand("char"), _f(total=ev.MIN_ROWS, nonnull=ev.MIN_ROWS, marked=5, unmarked=15)) == "supported"


def test_a_supported_candidate_is_enforced_as_the_kind_its_type_holds() -> None:
    assert [ev.spec_kind(_cand(p)) for p in ("timestamp", "boolean", "int", "char", "string_date")] == ["timestamp", "boolean", "int_flag", "char_flag", None]


# ----------------------------------------------------------------------------------------------- the statement


@pytest.mark.parametrize("dialect", ["mysql", "postgres", "tsql", "oracle", "sqlite"])
@pytest.mark.parametrize("probe", ["timestamp", "string_date", "boolean", "int", "char"])
def test_every_probe_is_one_select_of_integer_counts_in_every_dialect(dialect: str, probe: str) -> None:
    c = ev.Candidate("orders", "sales", "record_deleted", "T", "flag", probe, True, "fp")
    sql = ph.build_probe_sql(c, dialect)
    tree = sqlglot.parse_one(sql, read=dialect)
    assert isinstance(tree, exp.Select) and len(sqlglot.parse(sql, read=dialect)) == 1
    for expression in tree.expressions:  # only counts: no column, key or value is ever selected bare
        assert isinstance(expression.this, (exp.Count, exp.Sum)), expression.sql()
    assert str(ev.SAMPLE_ROWS) in sql  # bounded: LIMIT / TOP / FETCH FIRST


def test_hostile_names_stay_inside_the_quotes() -> None:
    c = ev.Candidate('x"; DROP TABLE y; --', 'a"b', 'c"d', "CHAR(1)", "flag", "char", True, "fp")
    for dialect in ("mysql", "postgres", "tsql", "oracle", "sqlite"):
        sql = ph.build_probe_sql(c, dialect)
        tree = sqlglot.parse_one(sql, read=dialect)
        assert isinstance(tree, exp.Select) and not tree.find(exp.Drop) and len(sqlglot.parse(sql, read=dialect)) == 1


def test_counts_are_read_in_lower_case_and_inconsistent_results_are_refused() -> None:
    good = [{"TOTAL_ROWS": 30, "NONNULL_ROWS": 30, "DISTINCT_VALUES": 2, "MARKED_LIKE": 6, "UNMARKED_LIKE": "24"}]
    assert ph.parse_counts(good, "char") == ev.Facts(30, 30, 2, 6, 24)
    assert ph.parse_counts([{"total_rows": 30, "nonnull_rows": 10}], "timestamp") == ev.Facts(30, 10, 0, 10, 0)
    assert ph.parse_counts([{"total_rows": 30, "nonnull_rows": 30, "distinct_values": 2, "marked_like": None, "unmarked_like": None}], "int") == ev.Facts(30, 30, 2, 0, 0)
    for bad in (
        [], None, "x", [good[0], good[0]], [{}],
        [{"total_rows": 5, "nonnull_rows": 9, "distinct_values": 1, "marked_like": 0, "unmarked_like": 0}],
        [{"total_rows": 30, "nonnull_rows": 30, "distinct_values": 2, "marked_like": 20, "unmarked_like": 20}],
        [{"total_rows": 30, "nonnull_rows": 30, "distinct_values": 99, "marked_like": 1, "unmarked_like": 1}],
        [{"total_rows": -1, "nonnull_rows": 0, "distinct_values": 0, "marked_like": 0, "unmarked_like": 0}],
    ):
        assert ph.parse_counts(bad, "char") is None


def test_the_probe_runs_for_real_and_returns_only_counts(world) -> None:
    db, _root = world
    c = ev.Candidate("orders", None, "record_deleted", "CHAR(1)", "flag", "char", True, "fp")
    rows = asyncio.run(_sqlite_runner(db)(ph.build_probe_sql(c, "sqlite"), max_rows=2, db_id=DB))
    assert rows == [{"total_rows": N, "nonnull_rows": N, "distinct_values": 2, "marked_like": 6, "unmarked_like": 24}]


# ----------------------------------------------------------------------------------------------- the phase, on a real database


def test_each_table_is_classified_from_the_counts(world) -> None:
    db, root = world
    seen: list[str] = []
    report = _collect(root, db, seen)
    entries = _evidence(root)["entries"]
    status = {k: v["status"] for k, v in entries.items()}
    assert status == {
        "orders.record_deleted": "supported", "invoices.is_deleted": "supported", "shipments.deleted_at": "supported",
        "legacy.deleted": "supported", "tickets.rescinded": "supported", "catalogue.removed": "uncertain", "counters.deleted": "contradicted",
        "audit_log.deleted_at": "contradicted", "archive.is_deleted": "contradicted", "tiny.is_deleted": "insufficient", "vdate.deleted_on": "uncertain",
    }
    assert report["ok"] and report["candidates"] == 11 and report["queried"] == 11 and report["cached"] == 0 and report["errors"] == 0
    assert (report["supported"], report["uncertain"], report["contradicted"], report["insufficient"]) == (5, 2, 3, 1)
    assert len(seen) == 11 and not any("hist" in s for s in seen)  # a curated spec is a fact: never asked about
    assert all(s.startswith("SELECT COUNT(*) AS total_rows") for s in seen)


def test_the_view_says_what_the_data_proved_and_what_it_did_not(world) -> None:
    db, root = world
    _collect(root, db)
    base = ss.load_base_specs(DB)
    assert {t: s.source for t, s in base.items()} == {
        "orders": "discovered", "invoices": "discovered", "shipments": "discovered", "audit_log": "discovered", "archive": "discovered",
        "tiny": "discovered", "hist": "curated", "v_orders": "discovered",  # name+type discovery has always included views: unchanged
    }
    view = ev.load_view(DB, base)
    assert {t: (s.column, s.kind, s.source) for t, s in view.verified.items()} == {
        "legacy": ("deleted", "int_flag", "verified"), "tickets": ("rescinded", "char_flag", "verified"),
    }
    assert set(view.uncertain) == {"catalogue", "vdate"} and set(view.demoted) == {"audit_log", "archive"}
    # no marker: a counter, no column at all, too few rows to say, a curated fact, and the specs the data agrees with
    assert not {"counters", "plain", "tiny", "hist", "orders", "invoices", "shipments"} & (set(view.risky()) | set(view.verified))


def test_the_evidence_file_holds_counts_and_names_never_values(world) -> None:
    db, root = world
    _collect(root, db)
    raw = (root / DB / ev.EVIDENCE_REL).read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert doc["generated_by"] == ev.GENERATED_BY and "no values" in doc["note"]
    for entry in doc["entries"].values():
        assert set(entry.get("facts", {})) <= {"sample_rows", "null_rows", "distinct_values", "marked_like", "unmarked_like", "other_rows"}
        assert all(isinstance(v, int) for v in entry.get("facts", {}).values())
    assert not any(token in raw for token in ("2024-01-01", "2024-02-02", "'Y'", "\"Y\"", "AIX"))  # none of the values stored in the data tables


def test_an_unchanged_schema_costs_no_query_and_a_changed_table_only_its_own(world) -> None:
    db, root = world
    _collect(root, db)
    seen: list[str] = []
    again = _collect(root, db, seen, now=NOW + timedelta(days=1))
    assert seen == [] and again["cached"] == 11 and again["queried"] == 0 and "evidence_file" not in again
    doc = _schema_doc()
    for table in doc["tables"]:
        if table["name"] == "legacy":
            table["columns"].append({"name": "note", "type": "TEXT", "is_primary_key": False})
    (root / DB / "schema" / "schema.json").write_text(json.dumps(doc), encoding="utf-8")
    seen.clear()
    changed = _collect(root, db, seen, now=NOW + timedelta(days=2))
    assert len(seen) == 1 and '"legacy"' in seen[0] and changed["cached"] == 10
    seen.clear()
    _collect(root, db, seen, now=NOW + timedelta(days=ev.CACHE_TTL_DAYS + 3))
    assert len(seen) == 11  # data drifts: everything is looked at again after the cache lifetime


def test_failures_are_recorded_by_class_name_only_and_remembered_briefly(world) -> None:
    _db, root = world

    async def boom(sql: str, *, max_rows: int, db_id: str):
        raise RuntimeError("Data truncated for value 'ACME-SECRET-123'")  # a database message can quote a value

    report = _collect(root, _db, runner=boom)
    raw = (root / DB / ev.EVIDENCE_REL).read_text(encoding="utf-8")
    assert "ACME-SECRET-123" not in raw and "RuntimeError" in raw and "ACME-SECRET-123" not in json.dumps(report)
    view = ev.load_view(DB, ss.load_base_specs(DB))
    assert not view.verified and not view.uncertain and not view.demoted  # a database that cannot be read proves nothing: nothing changes
    seen: list[str] = []
    assert _collect(root, _db, seen, now=NOW + timedelta(hours=1))["cached"] >= 1  # the failure itself is cached for a short while


def test_repeated_failures_the_query_budget_and_the_time_budget_stop_the_checks(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    calls = 0

    async def refuse(sql: str, *, max_rows: int, db_id: str):
        nonlocal calls
        calls += 1
        raise PermissionError

    monkeypatch.setattr(ev, "MAX_CONSECUTIVE_ERRORS", 2)
    report = _collect(root, db, runner=refuse)
    assert calls == 2 and report["stopped"] == "repeated failures" and report["not_checked"] == 9
    monkeypatch.setattr(ev, "MAX_CONSECUTIVE_ERRORS", 5)
    monkeypatch.setattr(ev, "MAX_QUERIES", 3)
    seen: list[str] = []
    report = _collect(root, db, seen, now=NOW + timedelta(days=30))
    assert len(seen) == 3 and report["stopped"] == "query budget used"
    monkeypatch.setattr(ev, "MAX_QUERIES", 200)
    monkeypatch.setattr(ev, "DATA_BUDGET_SECONDS", 0.0)
    assert _collect(root, db, now=NOW + timedelta(days=60))["stopped"] == "time budget used"


def test_a_slow_statement_is_cut_off(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world

    async def slow(sql: str, *, max_rows: int, db_id: str):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(ev, "QUERY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ev, "MAX_CONSECUTIVE_ERRORS", 1)
    report = _collect(root, db, runner=slow)
    assert report["errors"] == 1 and report["stopped"] == "repeated failures"


def test_a_file_the_phase_did_not_write_is_never_read_or_overwritten(world) -> None:
    db, root = world
    path = root / DB / ev.EVIDENCE_REL
    path.write_text('{"mine": true}', encoding="utf-8")
    report = _collect(root, db)
    assert path.read_text(encoding="utf-8") == '{"mine": true}' and "left untouched" in report["evidence_file"] and report["supported"] == 5


def test_no_catalog_an_unknown_engine_or_the_pinned_database_ask_nothing(world, tmp_path: Path) -> None:
    db, root = world
    seen: list[str] = []
    assert "no schema.json" in asyncio.run(ph.run_soft_delete_phase(db_id=DB, databases_dir=tmp_path / "nowhere", runner=_sqlite_runner(db, seen)))["skipped"]
    doc = _schema_doc()
    doc["engine"] = "somethingelse"
    (root / DB / "schema" / "schema.json").write_text(json.dumps(doc), encoding="utf-8")
    assert _collect(root, db, seen)["skipped"] == "the database engine is unknown"
    assert asyncio.run(ph.run_soft_delete_phase(db_id="erp_main", databases_dir=root, runner=_sqlite_runner(db, seen)))["skipped"] == "pinned database"
    assert seen == []


def test_the_clients_error_log_is_redacted_during_a_probe_and_nothing_else(world, caplog: pytest.LogCaptureFixture) -> None:
    db, root = world
    client_log = logging.getLogger("src.core.db_client")

    async def noisy(sql: str, *, max_rows: int, db_id: str):
        client_log.error("SQL execution failed: Data truncated for value 'ACME-SECRET-123'")
        raise RuntimeError("x")

    with caplog.at_level(logging.ERROR, logger="src.core.db_client"):
        _collect(root, db, runner=noisy)
        client_log.error("SQL execution failed: an ordinary user query failed")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ACME-SECRET-123" not in text and "details withheld" in text and "an ordinary user query failed" in text


# ----------------------------------------------------------------------------------------------- what the flags change


def test_with_both_flags_off_the_specs_are_exactly_what_they_always_were(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    _flags_on(monkeypatch)
    assert ss.load_specs(DB) is ss.load_base_specs(DB)  # the very same cached object: not a copy, not a rebuild


def test_shadow_mode_collects_evidence_but_changes_no_spec(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply")
    assert ss.load_specs(DB) is ss.load_base_specs(DB)


def test_the_guard_flag_alone_does_nothing_without_the_evidence_flag(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    _flags_on(monkeypatch, "auto_soft_delete_guard_apply")
    assert ev.guard_on(DB) is False and ss.load_specs(DB) is ss.load_base_specs(DB)


def test_enforcing_applies_the_data_verdicts(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    specs = ss.load_specs(DB)
    assert {t: s.source for t, s in specs.items()} == {
        "orders": "discovered", "invoices": "discovered", "shipments": "discovered", "tiny": "discovered", "hist": "curated",
        "v_orders": "discovered", "legacy": "verified", "tickets": "verified",  # proven by the data; audit_log and archive (contradicted guesses) are gone
    }
    assert "audit_log" not in specs and "archive" not in specs and specs["hist"].source == "curated"  # a curated fact is never demoted


def test_declared_and_atlas_specs_are_facts_never_asked_about_and_never_demoted(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    monkeypatch.setattr(ss, "_declared_column", lambda _db: "deleted_at")  # db.yaml declares deleted_at: audit_log and shipments get a declared spec
    (root / DB / "semantics" / "behavioral_atlas.json").write_text(json.dumps({"tables": {"vdate": {"columns": {"deleted_at": {}}}}}), encoding="utf-8")
    ss.clear_spec_cache()
    base = ss.load_base_specs(DB)
    assert (base["audit_log"].source, base["shipments"].source, base["vdate"].source) == ("declared", "declared", "atlas")
    seen: list[str] = []
    _collect(root, db, seen)
    assert not any(t in sql for sql in seen for t in ('"audit_log"', '"shipments"', '"vdate"'))  # facts: never asked about
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    specs = ss.load_specs(DB)
    assert specs["audit_log"].source == "declared" and specs["vdate"].source == "atlas"  # even though audit_log's data says it is never NULL
    assert set(_guard("SELECT * FROM audit_log, vdate")) == set()  # and a fact is never refused


def test_a_verified_marker_filters_like_any_other_and_a_demoted_guess_no_longer_does(world, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.soft_delete import enforce_soft_delete_filter

    db, root = world
    _collect(root, db)
    sql = "SELECT * FROM legacy"
    _flags_on(monkeypatch)
    assert enforce_soft_delete_filter(sql, "ACTIVE_ONLY", "sqlite", DB) == sql  # today: nothing is known about legacy
    assert "deleted_at" in enforce_soft_delete_filter("SELECT * FROM audit_log", "ACTIVE_ONLY", "sqlite", DB)  # and audit_log would drop every row
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    assert enforce_soft_delete_filter(sql, "ACTIVE_ONLY", "sqlite", DB) == "SELECT * FROM legacy WHERE (deleted = 0 OR deleted IS NULL)"
    assert enforce_soft_delete_filter("SELECT * FROM tickets", "ACTIVE_ONLY", "sqlite", DB) == (
        "SELECT * FROM tickets WHERE (rescinded IN ('N', 'n') OR rescinded IS NULL)"
    )
    assert enforce_soft_delete_filter("SELECT * FROM audit_log", "ACTIVE_ONLY", "sqlite", DB) == "SELECT * FROM audit_log"
    assert enforce_soft_delete_filter("SELECT * FROM legacy", "DELETED_ONLY", "sqlite", DB) == "SELECT * FROM legacy WHERE deleted <> 0"
    assert enforce_soft_delete_filter("SELECT * FROM legacy", "INCLUDE_ARCHIVED", "sqlite", DB) == sql
    # a table the name+type already found, and one the data agrees with, are filtered exactly as before
    assert enforce_soft_delete_filter("SELECT * FROM orders", "ACTIVE_ONLY", "sqlite", DB) == "SELECT * FROM orders WHERE (record_deleted IN ('N', 'n') OR record_deleted IS NULL)"


# ----------------------------------------------------------------------------------------------- the guard at question time


def _guard(sql: str, intent: str = "ACTIVE_ONLY") -> dict[str, str]:
    return ev.soft_delete_guard(sql, intent, "sqlite", DB)


def test_the_guard_is_silent_and_free_when_the_evidence_flag_is_off(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    seen: list[Any] = []
    monkeypatch.setattr("src.utils.telemetry.log_telemetry", lambda **kw: seen.append(kw))
    _flags_on(monkeypatch)
    assert _guard("SELECT * FROM catalogue") == {} and seen == []


def test_in_shadow_mode_the_guard_only_logs_what_it_would_have_done(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr("src.utils.telemetry.log_telemetry", lambda **kw: seen.append(kw))
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply")
    assert _guard("SELECT c.id FROM catalogue c JOIN legacy l ON l.id = c.id") == {}  # nothing is blocked
    [event] = seen
    assert event["stage"] == "soft_delete_guard_shadow" and event["extra"]["would_refuse"] == ["catalogue"] and event["extra"]["would_filter"] == ["legacy"]
    assert event["extra"]["refused"] == [] and event["extra"]["db_id"] == DB


def test_when_enforcing_a_query_on_an_unconfirmed_marker_is_blocked(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr("src.utils.telemetry.log_telemetry", lambda **kw: seen.append(kw))
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    blocked = _guard("SELECT * FROM catalogue")
    assert set(blocked) == {"catalogue"} and blocked["catalogue"].startswith("removed (possible marker")
    assert set(_guard("SELECT * FROM audit_log a, orders o")) == {"audit_log"}  # a guess the data contradicts
    assert set(_guard("WITH x AS (SELECT * FROM vdate) SELECT * FROM x")) == {"vdate"}  # a CTE is still a use of the table
    assert set(_guard("SELECT * FROM orders WHERE id IN (SELECT id FROM catalogue)")) == {"catalogue"}  # and so is a subquery
    assert _guard("SELECT * FROM orders") == {} and _guard("SELECT * FROM legacy") == {} and _guard("SELECT * FROM plain") == {}
    assert _guard("SELECT * FROM counters") == {} and _guard("SELECT * FROM tiny") == {} and _guard("SELECT * FROM hist") == {}
    assert seen[0]["stage"] == "soft_delete_guard_applied" and seen[0]["extra"]["refused"] == ["catalogue"]


def test_an_audit_or_history_question_is_never_blocked_because_it_adds_no_filter(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _collect(root, db)
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    assert _guard("SELECT * FROM catalogue", "INCLUDE_ARCHIVED") == {}
    assert set(_guard("SELECT * FROM catalogue", "DELETED_ONLY")) == {"catalogue"}  # "which rows are deleted" cannot be answered either


def test_without_evidence_or_with_a_broken_view_the_guard_lets_the_query_through(world, monkeypatch: pytest.MonkeyPatch) -> None:
    db, root = world
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    assert _guard("SELECT * FROM catalogue") == {}  # the evidence file does not exist yet
    _collect(root, db)
    monkeypatch.setattr(ev, "load_view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _guard("SELECT * FROM catalogue") == {}
    assert _guard("SELECT FROM WHERE (", "ACTIVE_ONLY") == {} and _guard("") == {}


def test_the_pinned_database_is_never_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    assert ev.evidence_on("erp_main") is False and ev.guard_on("erp_main") is False
    assert ev.soft_delete_guard("SELECT * FROM party", "ACTIVE_ONLY", "mysql", "erp_main") == {}


# ----------------------------------------------------------------------------------------------- the refusal reaches the answer


def test_the_refusal_message_is_plain_and_carries_names_only() -> None:
    err = ev.guard_error({"catalogue": "removed (possible marker; its values do not match the usual convention)", "vdate": "deleted_on (x)"})
    assert err.startswith(ev.GUARD_ERROR_PREFIX) and "catalogue=removed" in err
    message = ev.guard_message(err)
    assert "catalogue (column removed)" in message and "vdate (column deleted_on)" in message and "can't answer this reliably" in message
    assert "SELECT" not in message and "(possible marker" not in message


def test_the_answer_path_uses_the_guard_message_and_keeps_the_old_one_for_everything_else() -> None:
    from src.pipeline import query as q

    err = ev.guard_error({"catalogue": "removed (x)"})
    assert q._sql_unavailable_message(err) == ev.guard_message(err) and q._sql_unavailable_task(err) == "sql_guarded"
    for other in ("empty_llm_response", "All providers exhausted", None, "", AsyncMock(), 5):
        assert q._sql_unavailable_message(other) == q._SQL_UNAVAILABLE_MSG and q._sql_unavailable_task(other) == "sql_unavailable"


@pytest.mark.asyncio
async def test_generation_returns_no_sql_and_the_guard_error_for_an_unconfirmed_table(world, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.sql.generation as gen
    from src.sql.dialects.sqlite import SQLITE_PROFILE

    db, root = world
    await ph.run_soft_delete_phase(db_id=DB, databases_dir=root, runner=_sqlite_runner(db), now=NOW)
    router = AsyncMock()
    router.chat.return_value = "```sql\nSELECT id FROM catalogue\n```"
    schema = "CREATE TABLE catalogue (id INTEGER, removed VARCHAR(10));"
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    sql, _cot, infra = await gen.generate_sql(router=router, query="list the catalogue", schema=schema, dialect=SQLITE_PROFILE, db_id=DB)
    assert sql == "" and infra is not None and infra.startswith(ev.GUARD_ERROR_PREFIX) and "catalogue=removed" in infra
    _flags_on(monkeypatch, "auto_soft_delete_evidence_apply")  # shadow: the query goes through exactly as before
    sql, _cot, infra = await gen.generate_sql(router=router, query="list the catalogue", schema=schema, dialect=SQLITE_PROFILE, db_id=DB)
    assert "catalogue" in sql.lower() and infra is None
    _flags_on(monkeypatch)
    sql, _cot, infra = await gen.generate_sql(router=router, query="list the catalogue", schema=schema, dialect=SQLITE_PROFILE, db_id=DB)
    assert "catalogue" in sql.lower() and infra is None


# ----------------------------------------------------------------------------------------------- the whole sync, on the real connector


@pytest.fixture()
def marked_shop(shop: Path) -> Path:
    con = sqlite3.connect(shop)
    con.executescript(
        """
        ALTER TABLE customer    ADD COLUMN IsDeleted INTEGER DEFAULT 0;
        ALTER TABLE orders      ADD COLUMN deleted INTEGER;
        ALTER TABLE order_items ADD COLUMN removed VARCHAR(10);
        DELETE FROM customer; DELETE FROM orders; DELETE FROM order_items;
        """
    )
    con.executemany("INSERT INTO customer(id, email, name, IsDeleted) VALUES (?, ?, ?, ?)", [(i, f"c{i}@x.com", f"C{i}", i % 4 == 0) for i in range(1, 31)])
    con.executemany("INSERT INTO orders(id, customer_id, total, deleted) VALUES (?, ?, ?, ?)", [(i, 1, 10, i % 2) for i in range(1, 31)])
    con.executemany("INSERT INTO order_items(order_id, line_no, sku, removed) VALUES (?, ?, ?, ?)", [(i, 1, f"s{i}", "AIX"[i % 3]) for i in range(1, 31)])
    con.commit()
    con.close()
    return shop


@pytest.mark.asyncio
async def test_a_sync_with_the_flag_writes_the_evidence_and_the_guard_then_acts_on_it(marked_shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_scaffold", "auto_catalog", "auto_soft_delete_evidence_apply")
    result = await _sync()
    report = result["soft_delete"]
    assert report["ok"] and report["candidates"] == 3 and report["queried"] == 3 and (report["supported"], report["uncertain"]) == (2, 1)
    assert (report["verified"], report["uncertain"], report["contradicted_guesses"]) == (1, 1, 0)
    entries = json.loads((dbs / DB / ev.EVIDENCE_REL).read_text(encoding="utf-8"))["entries"]
    assert {k: v["status"] for k, v in entries.items()} == {"customer.isdeleted": "supported", "orders.deleted": "supported", "order_items.removed": "uncertain"}
    ss.clear_spec_cache()
    ev.clear_view_cache()
    assert {t: s.source for t, s in ss.load_specs(DB).items()} == {"customer": "discovered"}  # shadow: nothing changed
    _flags(monkeypatch, "auto_scaffold", "auto_catalog", "auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply")
    ss.clear_spec_cache()
    assert {t: (s.column, s.source) for t, s in ss.load_specs(DB).items()} == {"customer": ("IsDeleted", "discovered"), "orders": ("deleted", "verified")}
    assert set(ev.soft_delete_guard("SELECT sku FROM order_items", "ACTIVE_ONLY", "sqlite", DB)) == {"order_items"}
    assert ev.soft_delete_guard("SELECT id FROM orders", "ACTIVE_ONLY", "sqlite", DB) == {}


@pytest.mark.asyncio
async def test_a_sync_without_the_flag_asks_nothing_and_writes_nothing(marked_shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_scaffold", "auto_catalog")
    result = await _sync()
    assert "soft_delete" not in result and not (dbs / DB / ev.EVIDENCE_REL).exists()


@pytest.mark.asyncio
async def test_the_sync_report_knows_the_phase_and_a_failure_never_fails_the_sync(marked_shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.sync.report import PHASES, phase_status

    assert ("soft_delete", "soft_delete") in PHASES
    _flags(monkeypatch, "auto_scaffold", "auto_catalog", "auto_soft_delete_evidence_apply")
    monkeypatch.setattr(ph, "run_soft_delete_phase", AsyncMock(side_effect=RuntimeError("boom")))
    result = await _sync()
    assert result["status"] == "ok" and phase_status(result["soft_delete"])[0] == "failed"


# ----------------------------------------------------------------------------------------------- the flags


def test_flags_are_registered_off_by_default_and_documented() -> None:
    from src.utils.feature_flags import DB_FEATURE_FLAGS, DEFAULT_NEW_DB_FLAGS, OFF_BY_DEFAULT_FLAGS
    from src.utils.telemetry import ALLOWED_STAGES

    cfg = yaml.safe_load((ROOT / "config" / "feature_flags.example.yaml").read_text(encoding="utf-8"))
    for flag in ("auto_soft_delete_evidence_apply", "auto_soft_delete_guard_apply"):
        assert flag in DB_FEATURE_FLAGS and flag in OFF_BY_DEFAULT_FLAGS and DEFAULT_NEW_DB_FLAGS[flag] is False
        assert cfg["db_defaults"][flag] is False and cfg["db_features"]["_example_db"][flag] is False
    assert {"soft_delete_guard_shadow", "soft_delete_guard_applied"} <= ALLOWED_STAGES
