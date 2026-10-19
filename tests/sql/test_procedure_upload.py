"""Auto-schema Phase 4 (uploaded scripts): decode, check, store, permissions, and the merge into the phase.

Uploaded text is stored for mining and NEVER executed; row-data exports are refused.
"""

# ruff: noqa: F811

from __future__ import annotations

import codecs
import json
import os
from pathlib import Path

import pytest

from src.sql import procedure_upload as up
from src.sql.sa import procedures_phase as phase
from tests.sql.test_auto_procedures import (  # noqa: F401  (shared fixtures)
    _sync_args,
    dbs,
    sqlite_db,
)
from tests.sql.test_multi_admin_databases import BODY, PM, SUPER, VED, _client, env  # noqa: F401

TSQL = "CREATE PROCEDURE [dbo].[usp_ClosePO] @id INT AS BEGIN UPDATE po SET status = 'C' WHERE id = @id END\nGO\n"
SECRET = "payroll_ssn_column"


def _files(*items: tuple[str, bytes]):
    return [("files", (name, data, "application/octet-stream")) for name, data in items]


# ================================================================= decoding and checking


def test_decode_handles_utf8_bom_utf16_and_windows_1252_and_normalises_newlines():
    assert up.decode_script("CREATE PROCEDURE a AS SELECT 'é'\r\n".encode()) == "CREATE PROCEDURE a AS SELECT 'é'\n"
    assert up.decode_script(codecs.BOM_UTF8 + b"CREATE PROCEDURE a AS SELECT 1").startswith("CREATE")
    assert up.decode_script("CREATE PROCEDURE ä AS SELECT 1".encode("utf-16")).startswith("CREATE PROCEDURE ä")  # SSMS default
    assert "é" in up.decode_script("SELECT 'é'".encode("cp1252"))


def test_a_binary_file_is_refused():
    with pytest.raises(ValueError, match="not a text file"):
        up.decode_script(b"PK\x03\x04\x00\x00\x00binary")


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
    assert up.routine_names(script) == expected


def test_what_is_accepted_and_what_is_refused():
    assert up.script_problem(TSQL) is None
    assert "empty" in up.script_problem("  \n")
    assert "No CREATE" in up.script_problem("SELECT * FROM customers;")
    dump = "INSERT INTO t VALUES (1);\n" * 201 + TSQL
    assert "data export" in up.script_problem(dump)
    assert "data export" in up.script_problem("COPY customers (id) FROM stdin;\n1\n\\.\n" + TSQL)
    inside_body = "CREATE PROCEDURE p AS BEGIN\n" + "    INSERT INTO log VALUES (1);\n" * 300 + "END"
    assert up.script_problem(inside_body) is None  # indented INSERTs inside a routine are normal


# ================================================================= the endpoints


async def _db(c) -> str:
    res = await c.post("/api/databases/connect", json=BODY, headers=VED)
    assert res.status_code == 200
    return res.json()["db_id"]


@pytest.mark.asyncio
async def test_an_owner_uploads_a_script_and_it_is_stored_under_a_hash_never_its_own_name(env: Path):
    async with _client() as c:
        db = await _db(c)
        res = await c.post(f"/api/databases/{db}/procedures", files=_files(("../../evil.sql", TSQL.encode())), headers=VED)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] and body["rejected"] == [] and body["stored"][0]["name"] == "evil.sql"
    stored = sorted((env / db / "schema/procedures_upload").iterdir())
    assert [p.name for p in stored] == [body["stored"][0]["file"]] and stored[0].suffix == ".sql"
    assert stored[0].read_text(encoding="utf-8") == TSQL
    if os.name != "nt":
        assert oct(stored[0].stat().st_mode & 0o777) == "0o600"
    assert not list(env.rglob("evil*")) and not (env.parent / "evil.sql").exists()


@pytest.mark.asyncio
async def test_the_same_text_twice_is_stored_once(env: Path):
    async with _client() as c:
        db = await _db(c)
        for _ in range(2):
            assert (await c.post(f"/api/databases/{db}/procedures", files=_files(("a.sql", TSQL.encode())), headers=VED)).status_code == 200
    assert len(list((env / db / "schema/procedures_upload").glob("*.sql"))) == 1


@pytest.mark.asyncio
async def test_a_mixed_upload_keeps_the_good_file_and_says_why_the_other_was_refused(env: Path):
    async with _client() as c:
        db = await _db(c)
        res = await c.post(
            f"/api/databases/{db}/procedures",
            files=_files(("good.sql", TSQL.encode()), ("notes.txt", b"just some notes"), ("blob.sql", b"\x00\x01\x02binary")),
            headers=VED,
        )
    assert res.status_code == 200
    body = res.json()
    assert [s["name"] for s in body["stored"]] == ["good.sql"]
    reasons = {r["file"]: r["reason"] for r in body["rejected"]}
    assert "No CREATE" in reasons["notes.txt"] and "not a text file" in reasons["blob.sql"]


@pytest.mark.asyncio
async def test_when_nothing_is_acceptable_the_answer_is_422_with_the_reasons(env: Path):
    async with _client() as c:
        db = await _db(c)
        res = await c.post(f"/api/databases/{db}/procedures", files=_files(("x.sql", b"SELECT 1")), headers=VED)
    assert res.status_code == 422 and "x.sql" in res.json()["detail"] and "No CREATE" in res.json()["detail"]
    assert not (env / db / "schema/procedures_upload").exists()


@pytest.mark.asyncio
async def test_size_and_count_limits(env: Path, monkeypatch):
    monkeypatch.setattr(up, "MAX_FILE_BYTES", 50)
    monkeypatch.setattr(up, "MAX_REQUEST_FILES", 2)
    async with _client() as c:
        db = await _db(c)
        big = await c.post(f"/api/databases/{db}/procedures", files=_files(("big.sql", TSQL.encode() * 5)), headers=VED)
        many = await c.post(f"/api/databases/{db}/procedures", files=_files(*[(f"{i}.sql", TSQL.encode()) for i in range(3)]), headers=VED)
    assert big.status_code == 422 and "too large" in big.json()["detail"]
    assert many.status_code == 422 and "between 1 and 2" in many.json()["detail"]


@pytest.mark.asyncio
async def test_the_per_database_storage_limit(env: Path, monkeypatch):
    monkeypatch.setattr(up, "MAX_STORED_FILES", 1)
    async with _client() as c:
        db = await _db(c)
        ok = await c.post(f"/api/databases/{db}/procedures", files=_files(("a.sql", TSQL.encode())), headers=VED)
        full = await c.post(f"/api/databases/{db}/procedures", files=_files(("b.sql", b"CREATE PROCEDURE other AS SELECT 2")), headers=VED)
    assert ok.status_code == 200 and full.status_code == 422 and "already has 1" in full.json()["detail"]


@pytest.mark.asyncio
async def test_who_may_upload(env: Path):
    async with _client() as c:
        db = await _db(c)
        url = f"/api/databases/{db}/procedures"
        assert (await c.post(url, files=_files(("a.sql", TSQL.encode())), headers={"X-User-Id": "someone"})).status_code in (401, 403)  # not an admin
        assert (await c.post(url, files=_files(("a.sql", TSQL.encode())), headers=PM)).status_code == 403  # another admin's database
        assert (await c.post(url, files=_files(("a.sql", TSQL.encode())), headers=SUPER)).status_code == 200  # the super admin
        main = await c.post("/api/databases/erp_main/procedures", files=_files(("a.sql", TSQL.encode())), headers=SUPER)
        missing = await c.post("/api/databases/no_such_db/procedures", files=_files(("a.sql", TSQL.encode())), headers=SUPER)
        bad = await c.post("/api/databases/Bad%20Id/procedures", files=_files(("a.sql", TSQL.encode())), headers=SUPER)
    assert main.status_code == 422 and "main database" in main.json()["detail"]
    assert missing.status_code == 404 and bad.status_code in (404, 422)
    assert not (env / "erp_main").exists() or not (env / "erp_main/schema/procedures_upload").exists()


@pytest.mark.asyncio
async def test_clearing_removes_only_the_uploads(env: Path):
    async with _client() as c:
        db = await _db(c)
        await c.post(f"/api/databases/{db}/procedures", files=_files(("a.sql", TSQL.encode())), headers=VED)
        denied = await c.delete(f"/api/databases/{db}/procedures", headers=PM)
        done = await c.delete(f"/api/databases/{db}/procedures", headers=VED)
    assert denied.status_code == 403 and done.json() == {"ok": True, "db_id": db, "removed": 1}
    assert not list((env / db / "schema/procedures_upload").glob("*.sql")) and (env / db / "db.yaml").exists()


# ================================================================= merged into the phase and the sync


def test_stored_scripts_load_as_script_routines_with_their_member_names(dbs: Path):
    up.store_script("pg_live", TSQL + "CREATE FUNCTION dbo.f() RETURNS int AS BEGIN RETURN 1 END", dbs)
    [script] = up.load_uploaded_routines("pg_live", dbs)
    assert (script.kind, script.source, script.schema) == ("SCRIPT", "upload", "") and len(script.members) == 2
    assert up.load_uploaded_routines("nope_db", dbs) == []


@pytest.mark.asyncio
async def test_an_upload_alone_makes_a_database_full_mode_and_the_manifest_holds_no_code(dbs: Path, monkeypatch):
    monkeypatch.setattr(phase, "resolve_connection", lambda db_id: ("sqlite", {}))  # no catalog routines at all
    up.store_script("pg_live", TSQL.replace("status = 'C'", f"{SECRET} = 1"), dbs)
    report, routines = await phase.run_procedures_phase(db_id="pg_live", databases_dir=dbs)
    assert (report["mode"], report["found"], report["uploaded_scripts"], report["uploaded_routines"]) == ("full", 0, 1, 1)
    assert routines[0].source == "upload" and SECRET in routines[0].definition  # in memory for Phase 5 only
    manifest_text = (dbs / "pg_live/schema/procedures.json").read_text()
    assert SECRET not in manifest_text and SECRET not in json.dumps(report)
    entry = json.loads(manifest_text)["routines"][0]
    assert entry["source"] == "upload" and entry["members"] == ["dbo].[usp_ClosePO"]


@pytest.mark.asyncio
async def test_the_real_sync_reports_the_upload(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_procedures")
    up.store_script("pg_live", TSQL, dbs)
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["procedures"]["mode"] == "full" and result["procedures"]["uploaded_scripts"] == 1


@pytest.mark.asyncio
async def test_erp_main_ignores_uploads_entirely(monkeypatch):
    with pytest.raises(ValueError, match="curated"):
        up.store_script("erp_main", TSQL)
    report, routines = await phase.run_procedures_phase(db_id="erp_main")
    assert report["skipped"] == "pinned database" and routines == []
