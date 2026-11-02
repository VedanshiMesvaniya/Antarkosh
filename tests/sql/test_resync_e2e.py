# ruff: noqa: F811, F401
"""Phase 13 through the real sync (real SQLite): drift, the kept record, skipping unchanged phases, stale chunks, rollback."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.sql.sync import drift, versions
from tests.sql.test_catalog_extraction import _sync_args, dbs, sqlite_db

DB = "pg_live"
ON = ("auto_catalog", "auto_relationships", "auto_sync_orchestration")
APPLY = "auto_resync_apply"
STALE = "auto_stale_chunk_cleanup"


def _flags(monkeypatch: pytest.MonkeyPatch, *on: str) -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in on)
    monkeypatch.setattr("src.sql.introspection.catalog._now", lambda: "2026-11-01T12:00:00Z")


async def _sync(args: dict[str, Any] | None = None) -> dict[str, Any]:
    from src.sql.schema_retrieval import sync_live_schema

    return await sync_live_schema(db_id=DB, **(args or _sync_args()))


def _sql(path: Path, *statements: str) -> None:
    con = sqlite3.connect(path)
    for s in statements:
        con.execute(s)
    con.commit()
    con.close()


@pytest.fixture()
def shop(sqlite_db: Path) -> Path:
    """The fixture database without its view: the catalog phase reports a view SQLite cannot embed as a problem, which would
    (correctly) make every sync of it "not clean" and hide what these tests are about."""
    _sql(sqlite_db, "DROP VIEW v_orders")
    return sqlite_db


@pytest.mark.asyncio
async def test_with_the_flag_off_no_record_and_no_version_is_kept(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_catalog", "auto_relationships")
    result = await _sync()
    assert "orchestration" not in result and not (dbs / DB / ".sync").exists()


@pytest.mark.asyncio
async def test_the_first_sync_records_itself_and_keeps_a_version(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, *ON)
    rep = (await _sync())["orchestration"]
    assert rep["drift"]["first_sync"] is True and rep["version"]["id"] == "v0001"
    assert rep["resync"]["mode"] == "shadow" and rep["resync"]["skipped"] == [] and "no earlier sync" in rep["resync"]["ran"]["catalog"]
    state = drift.load_state(DB)
    assert state["clean"] is True and set(state["tables"]) == {"customer", "orders", "order_items"} and state["phases"]["catalog"] == "ok"
    assert state["enabled"] == ["catalog", "relationships"]
    last = yaml.safe_load((dbs / DB / "db.yaml").read_text())["schema_sync"]["last_sync"]
    assert last["version"] == "v0001" and last["drift"]["unchanged"] == 3


@pytest.mark.asyncio
async def test_in_shadow_mode_every_phase_still_runs_and_the_plan_is_only_reported(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, *ON)
    await _sync()
    second = await _sync()
    rep = second["orchestration"]
    assert second["catalog"]["tables"] == 3 and "skipped" not in second["catalog"]  # the catalog really ran
    assert rep["resync"]["mode"] == "shadow" and rep["resync"]["skipped"] == [] and "catalog" in rep["resync"]["would_skip"]
    assert rep["drift"]["counts"] == {"added": 0, "removed": 0, "changed": 0, "unchanged": 3} and rep["version"]["unchanged"] is True


@pytest.mark.asyncio
async def test_in_apply_mode_unchanged_phases_are_skipped_and_the_files_stay_exactly_as_they_were(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, *ON, APPLY)
    await _sync()
    before = {p.name: p.read_bytes() for p in (dbs / DB / "schema").glob("*.json")}
    second = await _sync()
    rep = second["orchestration"]
    assert second["catalog"]["skipped"] == "unchanged since the last sync" and second["relationships"]["unchanged"] is True
    assert {"catalog", "relationships"} <= set(rep["resync"]["skipped"]) and rep["resync"]["mode"] == "apply"
    assert {p.name: p.read_bytes() for p in (dbs / DB / "schema").glob("*.json")} == before
    assert rep["status"] == "ok" and rep["validation"]["ok"] is True
    assert not any("catalog" in u or "relationships" in u for u in rep["unknowns"])  # a deliberate skip is not an unknown
    assert drift.load_state(DB)["phases"]["catalog"] == "ok" and drift.load_state(DB)["clean"] is True
    third = await _sync()  # and it stays that way
    assert {"catalog", "relationships"} <= set(third["orchestration"]["resync"]["skipped"])


@pytest.mark.asyncio
async def test_a_schema_change_runs_the_phases_and_shows_what_drifted(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, *ON, APPLY)
    await _sync()
    _sql(shop, "CREATE TABLE payment(id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id))", "ALTER TABLE customer ADD COLUMN phone TEXT")
    second = await _sync()
    rep = second["orchestration"]
    assert rep["drift"]["added"] == ["payment"] and rep["drift"]["changed"] == ["customer"] and rep["drift"]["removed"] == []
    assert rep["resync"]["skipped"] == [] and "structure changed" in rep["resync"]["ran"]["catalog"]
    assert second["catalog"]["tables"] == 4 and "skipped" not in second["catalog"]
    assert "payment" in (dbs / DB / "schema/schema.json").read_text() and rep["version"]["id"] == "v0002"
    assert drift.load_state(DB)["structure_hash"] and set(drift.load_state(DB)["tables"]) >= {"payment"}


@pytest.mark.asyncio
async def test_an_edited_generated_file_makes_its_phase_run_again(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, *ON, APPLY)
    await _sync()
    target = dbs / DB / "schema/inferred_relationships.json"
    target.write_text(target.read_text() + " ", encoding="utf-8")  # same content, different bytes: still "edited"
    rep = (await _sync())["orchestration"]
    assert "catalog" in rep["resync"]["skipped"] and "relationships" not in rep["resync"]["skipped"]
    assert "inferred_relationships.json" in rep["resync"]["ran"]["relationships"]


@pytest.mark.asyncio
async def test_switching_a_phase_on_runs_everything_once(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_catalog", "auto_sync_orchestration", APPLY)
    await _sync()
    _flags(monkeypatch, *ON, APPLY)  # auto_relationships is new
    rep = (await _sync())["orchestration"]
    assert rep["resync"]["skipped"] == [] and "switched-on phases" in rep["resync"]["ran"]["catalog"]
    assert rep["phases"]["relationships"]["status"] == "ok"


@pytest.mark.asyncio
async def test_a_dropped_table_is_reported_in_shadow_and_its_chunk_removed_only_when_listed(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, *ON)
    await _sync()
    _sql(shop, "DROP TABLE order_items")
    args = _sync_args()
    rep = (await _sync(args))["orchestration"]
    assert rep["stale_chunks"]["mode"] == "shadow" and rep["stale_chunks"]["would_remove"] == ["order_items"]
    args["vector_store"].delete_chunks.assert_not_called()
    assert rep["drift"]["removed"] == ["order_items"]

    _sql(shop, "CREATE TABLE order_items(order_id INTEGER, sku TEXT)")
    await _sync()  # recorded again
    _sql(shop, "DROP TABLE order_items")
    _flags(monkeypatch, *ON, STALE)
    args = _sync_args()
    rep = (await _sync(args))["orchestration"]
    assert rep["stale_chunks"]["removed"] == ["order_items"]
    args["vector_store"].delete_chunks.assert_awaited_once_with(f"schema:{DB}", [f"{DB}_schema_order_items"])


@pytest.mark.asyncio
async def test_rolling_back_restores_the_old_files_and_the_next_sync_runs_everything(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, *ON, APPLY)
    await _sync()
    old_catalog = (dbs / DB / "schema/schema.json").read_bytes()
    _sql(shop, "CREATE TABLE payment(id INTEGER PRIMARY KEY)")
    await _sync()
    assert (dbs / DB / "schema/schema.json").read_bytes() != old_catalog
    assert [v["id"] for v in versions.list_versions(DB)] == ["v0002", "v0001"]

    result = versions.rollback_to_version(DB, "v0001")
    assert "schema/schema.json" in result["restored"] and (dbs / DB / "schema/schema.json").read_bytes() == old_catalog
    assert yaml.safe_load((dbs / DB / "db.yaml").read_text())["schema_sync"]["rolled_back_to"]["version"] == "v0001"

    rep = (await _sync())["orchestration"]  # the schema still has "payment": the record is untrusted, so everything runs
    assert rep["resync"]["skipped"] == [] and "rolled back to v0001" in rep["resync"]["ran"]["catalog"]
    assert "payment" in (dbs / DB / "schema/schema.json").read_text()


@pytest.mark.asyncio
async def test_a_sync_that_is_rolled_back_by_validation_keeps_the_old_record(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.sa import relationships_phase

    _flags(monkeypatch, *ON)
    await _sync()
    good = drift.load_state(DB)

    async def corrupt(**_kw: Any) -> dict[str, Any]:
        (dbs / DB / "schema/inferred_relationships.json").write_text("{half a file", encoding="utf-8")
        return {"ok": True}

    _sql(shop, "CREATE TABLE payment(id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(relationships_phase, "run_relationships_phase", corrupt)
    rep = (await _sync())["orchestration"]
    assert rep["status"] == "rolled_back" and "version" not in rep
    assert drift.load_state(DB) == good  # the record still describes the files that are on disk
