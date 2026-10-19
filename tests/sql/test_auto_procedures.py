"""Auto-schema Phase 4: stored-procedure acquisition (read from the database's catalog).

Offline: engines are faked with canned catalog rows. SQLite runs for real through the real sync.
MySQL, SQL Server and Oracle queries are NOT proven against a live server by these tests.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.sql.sa import procedures_phase as phase
from src.sql.sa import routines as rt
from src.sql.sa.procedures_phase import (
    build_report,
    manifest_document,
    run_procedures_phase,
    write_manifest,
)
from src.sql.sa.routines import ROUTINE_SQL, Routine, normalize_rows

# fixtures and helpers shared with the Phase 3 sync tests
from tests.sql.test_catalog_extraction import _sync_args, dbs, sqlite_db  # noqa: F401

SECRET = "SELECT secret_column FROM payroll WHERE ssn = 1"


# ============================================================== the queries are catalog reads only


@pytest.mark.parametrize("engine", sorted(ROUTINE_SQL))
def test_every_routine_query_is_a_single_read_only_select(engine: str):
    sql = ROUTINE_SQL[engine].strip()
    assert sql.upper().startswith("SELECT") and ";" not in sql
    forbidden = r"\b(EXEC|EXECUTE|CALL|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|MERGE|TRUNCATE|INTO)\b"
    assert re.search(forbidden, sql, re.IGNORECASE) is None


def test_every_engine_with_routines_is_covered_and_sqlite_is_not():
    assert set(ROUTINE_SQL) == {"postgresql", "mysql", "mssql", "oracle"}


@pytest.mark.parametrize("engine", sorted(ROUTINE_SQL))
def test_the_query_parses_in_its_own_dialect(engine: str):
    import sqlglot

    dialect = {"postgresql": "postgres", "mysql": "mysql", "mssql": "tsql", "oracle": "oracle"}[engine]
    assert sqlglot.parse_one(ROUTINE_SQL[engine], read=dialect) is not None


def test_mysql_reads_only_the_connected_database():
    assert "DATABASE()" in ROUTINE_SQL["mysql"]


# ============================================================== row normalisation


def _row(schema="dbo", name="p", kind="PROCEDURE", definition="CREATE PROCEDURE p AS SELECT 1") -> dict[str, Any]:
    return {"routine_schema": schema, "routine_name": name, "routine_type": kind, "definition": definition}


def test_rows_become_routines_with_a_stable_hash():
    routines, limited = normalize_rows("mssql", [_row(), _row(name="f", kind="FUNCTION")])
    assert limited is False and [(r.schema, r.name, r.kind, r.source) for r in routines] == [
        ("dbo", "p", "PROCEDURE", "database"), ("dbo", "f", "FUNCTION", "database"),
    ]
    assert routines[0].sha256 == normalize_rows("mssql", [_row()])[0][0].sha256 and len(routines[0].sha256) == 16


def test_a_routine_whose_text_is_hidden_or_empty_is_recorded_not_dropped():
    routines, _ = normalize_rows("mssql", [_row(definition=None), _row(name="e", definition="   ")])
    assert [(r.definition, r.unreadable) for r in routines] == [(None, "hidden"), (None, "empty")]


def test_oracle_lines_are_joined_per_routine_and_wrapped_source_is_unreadable():
    rows = [
        {"routine_schema": "HR", "routine_name": "RAISE_SAL", "routine_type": "PROCEDURE", "definition": "PROCEDURE raise_sal IS\n", "line_no": 1},
        {"routine_schema": "HR", "routine_name": "RAISE_SAL", "routine_type": "PROCEDURE", "definition": "BEGIN NULL; END;\n", "line_no": 2},
        {"routine_schema": "HR", "routine_name": "SECRET", "routine_type": "FUNCTION", "definition": "FUNCTION secret wrapped\n", "line_no": 1},
        {"routine_schema": "HR", "routine_name": "SECRET", "routine_type": "FUNCTION", "definition": "a000000 abcd\n", "line_no": 2},
    ]
    routines, _ = normalize_rows("oracle", rows)
    by = {r.name: r for r in routines}
    assert by["RAISE_SAL"].definition == "PROCEDURE raise_sal IS\nBEGIN NULL; END;\n"
    assert by["SECRET"].definition is None and by["SECRET"].unreadable == "wrapped"


def test_an_unknown_kind_is_treated_as_a_function_and_package_bodies_are_kept():
    routines, _ = normalize_rows("oracle", [
        {"routine_schema": "HR", "routine_name": "PKG", "routine_type": "PACKAGE BODY", "definition": "PACKAGE BODY pkg IS END;", "line_no": 1},
    ])
    assert routines[0].kind == "PACKAGE BODY"
    assert normalize_rows("mysql", [_row(kind="weird")])[0][0].kind == "FUNCTION"


def test_limits_truncate_one_definition_cap_the_total_and_cap_the_count(monkeypatch):
    monkeypatch.setattr(rt, "MAX_DEFINITION_CHARS", 10)
    monkeypatch.setattr(rt, "MAX_TOTAL_CHARS", 15)
    monkeypatch.setattr(rt, "MAX_ROUTINES", 3)
    routines, limited = normalize_rows("mssql", [_row(name=f"p{i}", definition="x" * 50) for i in range(5)])
    assert limited is True and len(routines) == 3
    assert (routines[0].truncated, len(routines[0].definition)) == (True, 10)
    assert routines[1].definition and len(routines[1].definition) == 10  # 10 spent < 15 when it started
    assert (routines[2].definition, routines[2].unreadable) == (None, "limit")  # 20 spent >= 15


# ============================================================== report and manifest


def test_full_mode_needs_at_least_one_readable_definition():
    full = build_report("mssql", normalize_rows("mssql", [_row(), _row(name="h", definition=None)])[0])
    assert (full["mode"], full["found"], full["readable"], full["unreadable"]) == ("full", 2, 1, {"hidden": 1})
    basic = build_report("mssql", [])
    assert (basic["mode"], basic["found"]) == ("basic", 0)


def test_routines_that_exist_but_are_hidden_say_how_to_fix_it():
    report = build_report("mssql", normalize_rows("mssql", [_row(definition=None)])[0])
    assert report["mode"] == "basic" and any("VIEW DEFINITION" in w for w in report["warnings"])


def test_partly_hidden_routines_and_invisible_routines_are_both_called_out():
    partly = build_report("mysql", normalize_rows("mysql", [_row(), _row(name="h", definition=None)])[0])
    assert partly["mode"] == "full" and any("1 of 2 routines are hidden" in w and "mysql.proc" in w for w in partly["warnings"])
    none_seen = build_report("mssql", [])
    assert none_seen["mode"] == "basic" and any("none" not in w and "no stored procedures were visible" in w for w in none_seen["warnings"])
    assert build_report("sqlite", [], warnings=["sqlite has no stored procedures"])["warnings"] == ["sqlite has no stored procedures"]


def test_the_manifest_holds_facts_never_code():
    routines, _ = normalize_rows("mssql", [_row(definition=f"CREATE PROCEDURE p AS {SECRET}")])
    report = build_report("mssql", routines)
    doc = manifest_document("qts_ais", "mssql", routines, report)
    text = json.dumps(doc)
    assert "secret_column" not in text and "payroll" not in text
    assert doc["generated_by"] == "auto_procedures" and doc["mode"] == "full" and doc["routines"][0]["chars"] > 0
    assert "definition" not in doc["routines"][0] and "routine" not in json.dumps(report).lower().replace("routines", "")


def test_the_manifest_is_written_only_into_an_existing_folder_and_never_over_a_foreign_file(dbs: Path):
    doc = {"generated_by": "auto_procedures", "mode": "basic"}
    assert write_manifest("pg_live", doc, dbs)["written"] == ["schema/procedures.json"]
    assert json.loads((dbs / "pg_live/schema/procedures.json").read_text())["mode"] == "basic"
    assert write_manifest("pg_live", {**doc, "mode": "full"}, dbs)["written"] == ["schema/procedures.json"]  # ours: replaced

    (dbs / "pg_live/schema/procedures.json").write_text('{"mine": true}', encoding="utf-8")
    skipped = write_manifest("pg_live", doc, dbs)
    assert skipped["written"] == [] and "left untouched" in skipped["skipped"]["schema/procedures.json"]
    assert json.loads((dbs / "pg_live/schema/procedures.json").read_text()) == {"mine": True}

    assert "does not exist" in write_manifest("nope_db", doc, dbs)["skipped"]["all"]
    assert "pinned" in write_manifest("erp_main", doc, dbs)["skipped"]["all"] and not (dbs / "erp_main").exists()


# ============================================================== the phase, with a faked engine


class _Row:
    def __init__(self, mapping: dict[str, Any]):
        self._mapping = mapping


class _Conn:
    def __init__(self, rows, fail, log):
        self.rows, self.fail, self.log = rows, fail, log

    async def execute(self, stmt):
        self.log.append(str(stmt))
        if self.fail:
            raise PermissionError("permission denied for pg_proc")
        return [_Row(r) for r in self.rows]


class _Engine:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail, self.log, self.disposed = list(rows), fail, [], False

    def connect(self):
        return self

    async def __aenter__(self):
        return _Conn(self.rows, self.fail, self.log)

    async def __aexit__(self, *exc):
        return False

    async def dispose(self):
        self.disposed = True


def _fake(monkeypatch, engine_key: str, eng: _Engine):
    monkeypatch.setattr(phase, "resolve_connection", lambda db_id: (engine_key, {}))
    monkeypatch.setattr(phase, "create_sa_engine", lambda db_id: eng)


@pytest.mark.asyncio
async def test_the_phase_reads_runs_only_the_catalog_query_and_records_full_mode(dbs: Path, monkeypatch):
    eng = _Engine([_row(), _row(name="calc", kind="FUNCTION")])
    _fake(monkeypatch, "mssql", eng)
    report, routines = await run_procedures_phase(db_id="pg_live", databases_dir=dbs)
    assert eng.log == [ROUTINE_SQL["mssql"]] and eng.disposed is True
    assert (report["mode"], report["found"], report["by_kind"]) == ("full", 2, {"FUNCTION": 1, "PROCEDURE": 1})
    assert [r.name for r in routines] == ["p", "calc"] and routines[0].definition.startswith("CREATE PROCEDURE")
    assert "definition" not in json.dumps(report)  # the report that reaches the API carries no code
    assert report["files"]["written"] == ["schema/procedures.json"]


@pytest.mark.asyncio
async def test_a_database_with_no_routines_is_basic_mode(dbs: Path, monkeypatch):
    _fake(monkeypatch, "postgresql", _Engine([]))
    report, routines = await run_procedures_phase(db_id="pg_live", databases_dir=dbs)
    assert (report["mode"], report["found"], routines) == ("basic", 0, [])
    assert json.loads((dbs / "pg_live/schema/procedures.json").read_text())["mode"] == "basic"


@pytest.mark.asyncio
async def test_a_failing_query_degrades_to_basic_mode_with_a_warning(dbs: Path, monkeypatch):
    eng = _Engine(fail=True)
    _fake(monkeypatch, "postgresql", eng)
    report, routines = await run_procedures_phase(db_id="pg_live", databases_dir=dbs)
    assert report["ok"] is True and report["mode"] == "basic" and routines == [] and eng.disposed is True
    assert any("PermissionError" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_sqlite_has_no_routines_and_opens_no_connection(dbs: Path, monkeypatch):
    opened = []
    monkeypatch.setattr(phase, "resolve_connection", lambda db_id: ("sqlite", {}))
    monkeypatch.setattr(phase, "create_sa_engine", lambda db_id: opened.append(db_id))
    report, _ = await run_procedures_phase(db_id="pg_live", databases_dir=dbs)
    assert opened == [] and report["mode"] == "basic" and any("no stored procedures" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_erp_main_is_never_read_by_this_phase(monkeypatch):
    monkeypatch.setattr(phase, "resolve_connection", lambda db_id: pytest.fail("erp_main must not be resolved"))
    report, routines = await run_procedures_phase(db_id="erp_main")
    assert report["skipped"] == "pinned database" and routines == []


# ============================================================== inside the real sync


@pytest.mark.asyncio
async def test_sync_without_the_flag_is_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: False)
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert set(result) == {"status", "db_id", "document_id", "tables_synced", "table_names"}
    assert not (dbs / "pg_live/schema/procedures.json").exists()


@pytest.mark.asyncio
async def test_sync_with_the_flag_reports_the_mode_and_writes_the_manifest(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_procedures")
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and "catalog" not in result  # only the flagged phase ran
    assert result["procedures"]["mode"] == "basic" and result["procedures"]["found"] == 0
    assert json.loads((dbs / "pg_live/schema/procedures.json").read_text())["engine"] == "sqlite"


@pytest.mark.asyncio
async def test_a_crash_in_the_procedure_phase_never_fails_the_sync(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_procedures")
    monkeypatch.setattr(phase, "run_procedures_phase", AsyncMock(side_effect=RuntimeError("boom")))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["procedures"]["ok"] is False and "boom" in result["procedures"]["error"]


def test_the_flag_is_known_and_erp_main_is_pinned_off():
    from src.utils.feature_flags import DB_FEATURE_FLAGS, is_db_feature_enabled

    assert "auto_procedures" in DB_FEATURE_FLAGS and is_db_feature_enabled("auto_procedures", "erp_main") is False


def test_routine_dataclass_is_immutable_and_hashes_only_text():
    r = Routine("s", "n", "PROCEDURE", "database", None, "hidden")
    assert r.sha256 == ""
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass
        r.name = "x"  # type: ignore[misc]
