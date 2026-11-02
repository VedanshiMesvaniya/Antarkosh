"""The sync report must read every phase's result under the key the sync body stores it, or the phase shows as "off"."""

# ruff: noqa: F811, F401
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.sql import registry
from src.sql.sync import drift, orchestrate
from src.sql.sync import report as report_mod
from src.sql.sync.report import PHASES, build_sync_report
from tests.sql.test_catalog_extraction import _sync_args, dbs, sqlite_db
from tests.sql.test_resync_e2e import DB, _flags, _sync, shop


def test_the_report_reads_exactly_the_result_keys_the_sync_body_sets() -> None:
    body = Path("src/sql/schema_retrieval.py").read_text(encoding="utf-8")
    set_keys = set(re.findall(r'result\["(\w+)"\]\s*=', body)) - {"_sync", "status", "tables_synced"}
    assert {key for _, key in PHASES} == set_keys  # a key that matches nothing hides that phase (and its failures)


@pytest.mark.parametrize(("name", "key"), PHASES)
def test_each_phase_result_shows_up_in_the_report_with_its_real_status(name: str, key: str) -> None:
    ok = build_sync_report({key: {"ok": True}}, db_id="x", duration_s=0.1, validation=None)
    assert ok["phases"][name]["status"] == "ok"
    failed = build_sync_report({key: {"ok": False, "error": "boom"}}, db_id="x", duration_s=0.1, validation=None)
    assert failed["phases"][name]["status"] == "failed" and failed["status"] == "ok_with_warnings"
    assert any(u.startswith(f"{name}: failed") for u in failed["unknowns"])
    assert build_sync_report({}, db_id="x", duration_s=0.1, validation=None)["phases"][name]["status"] == "off"


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    (base / "k_db" / "schema").mkdir(parents=True)
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    (base / "k_db" / "schema" / "schema.json").write_text('{"generated_by": "antarkosh-auto-catalog", "tables": []}', encoding="utf-8")
    return base / "k_db"


def _info() -> dict[str, Any]:
    fps = {"a": "1"}
    return {"schema_hash": "h", "structure_hash": drift.structure_hash(fps), "table_fingerprints": fps, "schema_complete": True, "enabled": ["catalog"], "phase_versions": {}}


@pytest.mark.parametrize(("failing", "clean"), [("column_meaning", True), ("sp_evidence", False), ("table_profiles", False), ("join_paths", False)])
def test_only_a_failure_that_later_phases_depend_on_makes_the_record_untrusted(root: Path, failing: str, clean: bool) -> None:
    key = dict(PHASES)[failing]
    orchestrate.persist_generation({key: {"ok": False, "error": "x"}}, _info(), None, "k_db", committed=True)
    state = drift.load_state("k_db")
    assert state["phases"][failing] == "failed" and state["clean"] is clean


@pytest.mark.asyncio
async def test_a_real_sync_records_and_skips_the_profiles_phase_too(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_catalog", "auto_relationships", "auto_table_profiles", "auto_join_paths", "auto_sync_orchestration", "auto_resync_apply")
    first = (await _sync())["orchestration"]
    assert first["phases"]["table_profiles"]["status"] != "off"  # it ran: the report must say so
    assert drift.load_state(DB)["phases"]["table_profiles"] == first["phases"]["table_profiles"]["status"]
    second = (await _sync())["orchestration"]
    assert {"catalog", "relationships", "table_profiles", "join_paths"} <= set(second["resync"]["skipped"])
