"""Auto-schema Phase 4 (procedures folder): developers drop .sql files into databases/<id>/procedures/.

The files are read at sync time together with the database's own procedures. They are checked, never
executed, and a skipped file is reported with its reason. erp_main never uses the folder.
"""

# ruff: noqa: F811

from __future__ import annotations

import codecs
import json
import os
import shutil
from pathlib import Path

import pytest

from src.sql import procedure_files as pf
from src.sql.introspection.scaffold import ensure_db_folder
from src.sql.sa import procedures_phase as phase
from tests.sql.test_auto_procedures import (  # noqa: F401  (shared fixtures)
    _sync_args,
    dbs,
    sqlite_db,
)

TSQL = "CREATE PROCEDURE [dbo].[usp_ClosePO] @id INT AS BEGIN UPDATE po SET status = 'C' WHERE id = @id END\nGO\n"
SECRET = "payroll_ssn_column"


def _no_folder(dbs: Path, db: str = "pg_live") -> None:
    """The template already ships an empty procedures/ folder; remove it to test the 'no folder yet' case."""
    shutil.rmtree(dbs / db / "procedures", ignore_errors=True)


def _drop(dbs: Path, name: str, content: str | bytes, db: str = "pg_live") -> Path:
    folder = dbs / db / "procedures"
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


# ================================================================= decoding and checking


def test_decode_handles_utf8_bom_utf16_and_windows_1252_and_normalises_newlines():
    assert pf.decode_script("CREATE PROCEDURE a AS SELECT 'é'\r\n".encode()) == "CREATE PROCEDURE a AS SELECT 'é'\n"
    assert pf.decode_script(codecs.BOM_UTF8 + b"CREATE PROCEDURE a AS SELECT 1").startswith("CREATE")
    assert pf.decode_script("CREATE PROCEDURE ä AS SELECT 1".encode("utf-16")).startswith("CREATE PROCEDURE ä")  # SSMS default
    assert "é" in pf.decode_script("SELECT 'é'".encode("cp1252"))


def test_a_binary_file_is_refused():
    with pytest.raises(ValueError, match="not a text file"):
        pf.decode_script(b"PK\x03\x04\x00\x00\x00binary")


@pytest.mark.parametrize(
    "script,expected",
    [
        (TSQL, ["dbo].[usp_ClosePO"]),
        ("CREATE OR ALTER PROC dbo.usp_x AS SELECT 1", ["dbo.usp_x"]),
        ("CREATE OR REPLACE FUNCTION public.total(o int) RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql;", ["public.total"]),
        ("CREATE DEFINER=`root`@`%` PROCEDURE `close_order`(IN o INT) BEGIN END", ["close_order"]),
        ("CREATE OR REPLACE EDITIONABLE PACKAGE BODY hr.emp_pkg IS END;", ["hr.emp_pkg"]),
        ("-- CREATE PROCEDURE old_one\n/* CREATE FUNCTION gone */\nCREATE FUNCTION kept() RETURNS int RETURN 1", ["kept"]),
    ],
)
def test_routine_names_are_found_across_dialects_and_comments_are_ignored(script, expected):
    assert pf.routine_names(script) == expected


def test_what_is_accepted_and_what_is_refused():
    assert pf.script_problem(TSQL) is None
    assert "empty" in pf.script_problem("  \n")
    assert "no CREATE" in pf.script_problem("SELECT * FROM customers;")
    assert "data export" in pf.script_problem("INSERT INTO t VALUES (1);\n" * 201 + TSQL)
    assert "data export" in pf.script_problem("COPY customers (id) FROM stdin;\n1\n\\.\n" + TSQL)
    inside_body = "CREATE PROCEDURE p AS BEGIN\n" + "    INSERT INTO log VALUES (1);\n" * 300 + "END"
    assert pf.script_problem(inside_body) is None  # indented INSERTs inside a routine are normal


# ================================================================= the folder


def test_no_folder_or_an_empty_one_means_the_database_is_the_only_source(dbs: Path):
    _no_folder(dbs)
    assert pf.load_folder_routines("pg_live", dbs) == ([], [])
    (dbs / "pg_live" / "procedures").mkdir()
    assert pf.load_folder_routines("pg_live", dbs) == ([], [])
    assert pf.load_folder_routines("not_a_db", dbs) == ([], [])


def test_sql_files_are_loaded_including_subfolders_and_other_files_are_ignored(dbs: Path):
    _drop(dbs, "purchasing.sql", TSQL)
    _drop(dbs, "stock/usp_Stock.sql", "CREATE PROC dbo.usp_a AS SELECT 1\nGO\nCREATE PROC dbo.usp_b AS SELECT 2")
    _drop(dbs, "README.txt", "notes for developers")
    scripts, skipped = pf.load_folder_routines("pg_live", dbs)
    assert skipped == []
    assert [(s.name, s.kind, s.source, s.schema) for s in scripts] == [
        ("purchasing.sql", "SCRIPT", "folder", ""), ("stock/usp_Stock.sql", "SCRIPT", "folder", ""),
    ]
    assert scripts[1].members == ("dbo.usp_a", "dbo.usp_b") and scripts[0].definition == TSQL


def test_a_file_that_cannot_be_used_is_skipped_with_its_reason_and_the_others_still_load(dbs: Path):
    _drop(dbs, "good.sql", TSQL)
    _drop(dbs, "empty.sql", "  ")
    _drop(dbs, "notes.sql", "SELECT 1")
    _drop(dbs, "dump.sql", "INSERT INTO t VALUES (1);\n" * 201)
    _drop(dbs, "blob.sql", b"\x00\x01binary")
    scripts, skipped = pf.load_folder_routines("pg_live", dbs)
    assert [s.name for s in scripts] == ["good.sql"]
    reasons = {s["file"]: s["reason"] for s in skipped}
    assert set(reasons) == {"empty.sql", "notes.sql", "dump.sql", "blob.sql"}
    assert "empty" in reasons["empty.sql"] and "no CREATE" in reasons["notes.sql"]
    assert "data export" in reasons["dump.sql"] and "not a text file" in reasons["blob.sql"]


def test_limits_on_size_count_and_total(dbs: Path, monkeypatch):
    monkeypatch.setattr(pf, "MAX_FILE_BYTES", 60)
    monkeypatch.setattr(pf, "MAX_FILES", 3)
    monkeypatch.setattr(pf, "MAX_TOTAL_BYTES", 130)
    _drop(dbs, "a.sql", TSQL)
    _drop(dbs, "b.sql", TSQL.replace("usp_ClosePO", "usp_two"))
    _drop(dbs, "c.sql", TSQL.replace("usp_ClosePO", "usp_three"))
    _drop(dbs, "d.sql", TSQL.replace("usp_ClosePO", "usp_four"))
    _drop(dbs, "big.sql", TSQL * 5)
    scripts, skipped = pf.load_folder_routines("pg_live", dbs)
    reasons = {s["file"]: s["reason"] for s in skipped}
    assert reasons["big.sql"] == "larger than 5 MB" or "larger" in reasons["big.sql"]
    assert "total size limit" in reasons["c.sql"] or "more than 3 files" in reasons.get("d.sql", "")
    assert len(scripts) <= 3


def test_a_symlink_pointing_outside_the_folder_is_never_read(dbs: Path, tmp_path: Path):
    outside = tmp_path / "secret.sql"
    outside.write_text(TSQL.replace("usp_ClosePO", "usp_outside"), encoding="utf-8")
    folder = dbs / "pg_live" / "procedures"
    folder.mkdir(exist_ok=True)
    try:
        os.symlink(outside, folder / "link.sql")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    scripts, skipped = pf.load_folder_routines("pg_live", dbs)
    assert scripts == [] and skipped[0]["file"] == "link.sql" and "regular file" in skipped[0]["reason"]


def test_erp_main_never_uses_the_folder(dbs: Path):
    _drop(dbs, "x.sql", TSQL, db="erp_main")
    assert pf.load_folder_routines("erp_main", dbs) == ([], [])
    assert pf.ensure_procedures_folder("erp_main", dbs) is False


def test_the_folder_is_created_only_inside_an_existing_database_folder(dbs: Path):
    _no_folder(dbs)
    assert pf.ensure_procedures_folder("pg_live", dbs) is True
    assert (dbs / "pg_live" / "procedures").is_dir()
    assert pf.ensure_procedures_folder("pg_live", dbs) is False  # idempotent
    assert pf.ensure_procedures_folder("no_such_db", dbs) is False and not (dbs / "no_such_db").exists()


def test_a_new_database_gets_its_procedures_folder_from_the_template(dbs: Path):
    result = ensure_db_folder("brand_new", engine="mssql", databases_dir=dbs)
    assert result.created and (result.path / "procedures").is_dir()


# ================================================================= merged into the phase and the sync


@pytest.mark.asyncio
async def test_folder_scripts_alone_make_a_database_full_mode_and_the_manifest_holds_no_code(dbs: Path, monkeypatch):
    monkeypatch.setattr(phase, "resolve_connection", lambda db_id: ("sqlite", {}))  # nothing readable from the database
    _drop(dbs, "po.sql", TSQL.replace("status = 'C'", f"{SECRET} = 1"))
    _drop(dbs, "junk.sql", "SELECT 1")
    report, routines = await phase.run_procedures_phase(db_id="pg_live", databases_dir=dbs)
    assert (report["mode"], report["found"], report["folder_scripts"], report["folder_routines"]) == ("full", 0, 1, 1)
    assert report["folder_skipped"] == [{"file": "junk.sql", "reason": "no CREATE PROCEDURE, FUNCTION or PACKAGE BODY definition found"}]
    assert routines[0].source == "folder" and SECRET in routines[0].definition  # in memory for Phase 5 only
    manifest_text = (dbs / "pg_live/schema/procedures.json").read_text()
    assert SECRET not in manifest_text and SECRET not in json.dumps(report)
    entry = json.loads(manifest_text)["routines"][0]
    assert entry["source"] == "folder" and entry["members"] == ["dbo].[usp_ClosePO"]


@pytest.mark.asyncio
async def test_the_phase_creates_the_folder_for_existing_databases_and_no_files_means_database_only(dbs: Path, monkeypatch):
    monkeypatch.setattr(phase, "resolve_connection", lambda db_id: ("sqlite", {}))
    _no_folder(dbs)  # a database made before this feature has no procedures/ folder yet
    report, _ = await phase.run_procedures_phase(db_id="pg_live", databases_dir=dbs)
    assert (dbs / "pg_live" / "procedures").is_dir()
    assert (report["mode"], report["folder_scripts"], report["folder_skipped"]) == ("basic", 0, [])


@pytest.mark.asyncio
async def test_the_real_sync_reports_the_folder_scripts(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_procedures")
    _drop(dbs, "po.sql", TSQL)
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["procedures"]["mode"] == "full" and result["procedures"]["folder_scripts"] == 1
