"""An existing database folder that lacks the template's empty semantics seed files gets them, and nothing else changes."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src.core.config import settings
from src.sql import registry
from src.sql.introspection import scaffold
from src.sql.introspection.scaffold import backfill_seed_files, scaffold_if_enabled

REAL_TEMPLATE = Path(__file__).resolve().parents[2] / "databases" / "_template"
SEEDS = {"semantics/behavioral_atlas.json", "semantics/column_glossary.json", "semantics/glossary.json", "semantics/routing_hints.json"}


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    shutil.copytree(REAL_TEMPLATE, base / "_template")
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    monkeypatch.setattr(settings, "db_engine", "mysql")
    return base


def _incomplete(dbs: Path, name: str = "old_db") -> Path:
    """A folder that exists but never got the template's seeds: only db.yaml and a schema file."""
    folder = dbs / name
    (folder / "schema").mkdir(parents=True)
    (folder / "db.yaml").write_text(yaml.safe_dump({"id": name, "engine": "mysql", "enabled": True}), encoding="utf-8")
    (folder / "schema" / "schema.json").write_text('{"tables": []}', encoding="utf-8")
    return folder


def _files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_the_missing_seed_files_are_added_and_nothing_else(dbs: Path) -> None:
    folder = _incomplete(dbs)
    before = _files(folder)
    assert set(backfill_seed_files("old_db")) == SEEDS
    after = _files(folder)
    assert set(after) - set(before) == SEEDS  # exactly the four seeds appeared
    assert {k: v for k, v in after.items() if k in before} == before  # db.yaml and the schema file are byte-for-byte unchanged
    assert after["semantics/behavioral_atlas.json"] == (REAL_TEMPLATE / "semantics/behavioral_atlas.json").read_bytes()
    assert not [p for p in folder.rglob(".*") if p.is_file() and p.name != ".gitkeep"]  # no temp file left behind


def test_an_existing_or_customised_file_is_never_touched(dbs: Path) -> None:
    folder = _incomplete(dbs)
    (folder / "semantics").mkdir()
    custom = b'{"revenue": "hand written"}'
    (folder / "semantics" / "glossary.json").write_bytes(custom)
    created = backfill_seed_files("old_db")
    assert "semantics/glossary.json" not in created and (folder / "semantics" / "glossary.json").read_bytes() == custom
    assert set(created) == SEEDS - {"semantics/glossary.json"}


def test_a_second_call_adds_nothing(dbs: Path) -> None:
    _incomplete(dbs)
    backfill_seed_files("old_db")
    snapshot = _files(dbs / "old_db")
    assert backfill_seed_files("old_db") == () and _files(dbs / "old_db") == snapshot


def test_a_complete_folder_is_left_alone(dbs: Path) -> None:
    shutil.copytree(dbs / "_template", dbs / "full_db")
    assert backfill_seed_files("full_db") == ()


def test_nothing_is_added_for_the_pinned_database_a_missing_folder_or_a_missing_template(dbs: Path) -> None:
    (dbs / "erp_main" / "schema").mkdir(parents=True)
    assert backfill_seed_files("erp_main") == () and not (dbs / "erp_main" / "semantics").exists()
    assert backfill_seed_files("no_such_db") == ()
    _incomplete(dbs)
    shutil.rmtree(dbs / "_template")
    assert backfill_seed_files("old_db") == () and not (dbs / "old_db" / "semantics").exists()


def test_an_invalid_id_and_a_failing_write_never_raise(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert backfill_seed_files("Bad Id!") == ()
    _incomplete(dbs)
    monkeypatch.setattr(scaffold.tempfile, "mkstemp", MagicMock(side_effect=OSError("disk full")))
    assert backfill_seed_files("old_db") == ()
    assert not (dbs / "old_db" / "semantics" / "glossary.json").exists()


def test_the_template_is_not_modified(dbs: Path) -> None:
    before = _files(dbs / "_template")
    _incomplete(dbs)
    backfill_seed_files("old_db")
    assert _files(dbs / "_template") == before


def test_the_wrapper_adds_the_seeds_reports_them_and_clears_caches(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _incomplete(dbs)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_scaffold")
    cleared = MagicMock()
    monkeypatch.setattr("src.sql.pipeline.clear_knowledge_caches", cleared)
    result = scaffold_if_enabled("old_db")
    assert result.created is False and set(result.backfilled) == SEEDS
    cleared.assert_called_once_with("old_db")
    cleared.reset_mock()
    assert scaffold_if_enabled("old_db").backfilled == ()  # nothing left to add: no cache churn
    cleared.assert_not_called()


def test_the_wrapper_does_nothing_when_the_flag_is_off(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _incomplete(dbs)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: False)
    assert scaffold_if_enabled("old_db") is None and not (dbs / "old_db" / "semantics").exists()
