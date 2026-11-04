"""A real sync of a database folder that lacks the template's seed files adds them, with versioning on or off."""

# ruff: noqa: F811, F401
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.sql.test_catalog_extraction import _sync_args, dbs, sqlite_db
from tests.sql.test_resync_e2e import DB, _sync, shop

SEEDS = {"semantics/behavioral_atlas.json", "semantics/column_glossary.json", "semantics/glossary.json", "semantics/routing_hints.json"}


@pytest.mark.asyncio
@pytest.mark.parametrize("versioning", [False, True])
async def test_a_real_sync_of_an_incomplete_folder_adds_the_seeds_with_or_without_versioning(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch, versioning: bool) -> None:
    on = {"auto_scaffold", "auto_catalog", "auto_relationships"} | ({"auto_sync_orchestration"} if versioning else set())
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in on)
    folder = dbs / DB
    shutil.rmtree(folder / "semantics")  # the folder exists but never got the seeds
    await _sync()
    assert {f"semantics/{p.name}" for p in (folder / "semantics").glob("*.json")} == SEEDS
    assert (folder / "schema" / "schema.json").is_file()  # and the sync itself still did its work


@pytest.mark.asyncio
async def test_a_customised_seed_survives_a_sync(shop: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in {"auto_scaffold", "auto_catalog"})
    glossary = dbs / DB / "semantics" / "glossary.json"
    glossary.write_text('{"revenue": "kept"}', encoding="utf-8")
    (dbs / DB / "semantics" / "routing_hints.json").unlink()
    await _sync()
    assert glossary.read_text(encoding="utf-8") == '{"revenue": "kept"}' and (dbs / DB / "semantics" / "routing_hints.json").is_file()
