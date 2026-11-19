"""Sync validation reports a soft-delete setting that names a column the catalog does not have (a warning, never an error)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.sql import soft_delete_spec as ss
from src.sql.sync import validate as v
from src.sql.sync.guard import GENERATED_FILES

DB = "shop"


def table(name: str, *cols: tuple[str, str]) -> dict[str, Any]:
    return {"name": name, "columns": [{"name": n, "type": t} for n, t in cols]}


CATALOG = {"tables": [table("sqts_main_lear", ("qn_number", "varchar(20)"), ("record_deleted", "char(1)")), table("plain", ("id", "int"))]}
PLAIN = {"tables": [table("plain", ("id", "int"), ("name", "varchar(20)"))]}  # no soft-delete column anywhere


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ss, "_declared_column", lambda db_id: None)
    monkeypatch.setattr(v, "_template_default", lambda: "deleted_at")
    return tmp_path


def declare(monkeypatch: pytest.MonkeyPatch, column: str | None) -> None:
    monkeypatch.setattr(ss, "_declared_column", lambda db_id: column)


def curate(root: Path, tables: dict[str, Any]) -> None:
    (root / "semantics").mkdir(exist_ok=True)
    (root / ss.CURATED_REL).write_text(json.dumps({"tables": tables}), encoding="utf-8")


def warn(root: Path, catalog: dict[str, Any] = CATALOG) -> list[str]:
    return v._soft_delete_warnings(DB, catalog, root)


def test_the_stale_template_default_is_reported_when_the_catalog_has_other_markers(root: Path, monkeypatch: pytest.MonkeyPatch):
    declare(monkeypatch, "deleted_at")  # the qts_ais case: the template's value, while the real marker is record_deleted CHAR(1)
    out = warn(root)
    assert len(out) == 1 and "'deleted_at'" in out[0] and "filters nothing" in out[0]
    assert "sqts_main_lear.record_deleted (char_flag)" in out[0]  # the real marker is named, so the fix is visible


def test_the_template_default_on_a_database_with_no_soft_delete_at_all_stays_silent(root: Path, monkeypatch: pytest.MonkeyPatch):
    declare(monkeypatch, "deleted_at")
    assert warn(root, PLAIN) == []  # otherwise every sync of such a database would warn forever


def test_a_deliberately_declared_column_that_does_not_exist_is_always_reported(root: Path, monkeypatch: pytest.MonkeyPatch):
    declare(monkeypatch, "is_delted")  # a typo: deleted rows would silently come back
    for catalog in (CATALOG, PLAIN):
        out = warn(root, catalog)
        assert len(out) == 1 and "'is_delted'" in out[0]
    assert "markers detected in the catalog: none" in warn(root, PLAIN)[0]


@pytest.mark.parametrize("declared", ["deleted_at", "record_deleted", "Record_Deleted", None])
def test_a_declared_column_that_exists_or_no_declaration_gives_no_warning(root: Path, monkeypatch: pytest.MonkeyPatch, declared):
    declare(monkeypatch, declared)
    present = {"tables": [table("t", ("deleted_at", "datetime"), ("record_deleted", "char(1)"))]}
    assert warn(root, present) == []


def test_a_curated_entry_naming_a_missing_column_or_table_is_reported(root: Path):
    curate(root, {"sqts_main_lear": {"column": "record_deleted", "kind": "char_flag"}, "plain": {"column": "gone", "kind": "char_flag"}, "ghost": {"column": "x", "kind": "boolean"}})
    out = warn(root)
    assert any("plain.gone is not in the catalog" in w and "fail with an invalid column" in w for w in out)
    assert any("table 'ghost' is not in the catalog" in w for w in out)
    assert not any("sqts_main_lear" in w for w in out)  # the valid entry is not mentioned


def test_unreadable_or_odd_curated_files_and_catalogs_never_raise(root: Path):
    (root / "semantics").mkdir()
    for text in ("{broken", "[]", '{"tables": 5}', '{"tables": {"t": 7}}'):
        (root / ss.CURATED_REL).write_text(text, encoding="utf-8")
        assert warn(root) == []
    assert v._soft_delete_warnings(DB, {"tables": "x"}, root) == [] and v._soft_delete_warnings(DB, {}, root) == []


def test_the_findings_are_warnings_so_they_never_roll_a_sync_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / DB
    (root / "schema").mkdir(parents=True)
    marker = next(iter(GENERATED_FILES["schema/schema.json"]))
    (root / "schema" / "schema.json").write_text(json.dumps({"generated_by": marker, **CATALOG}), encoding="utf-8")
    monkeypatch.setattr(v, "_template_default", lambda: "deleted_at")
    declare(monkeypatch, "deleted_at")
    curate(root, {"plain": {"column": "gone", "kind": "char_flag"}})
    res = v.validate_generation(DB, databases_dir=tmp_path)
    assert res.ok and res.errors == []
    assert any(w.startswith("soft-delete: db.yaml soft_delete.column 'deleted_at'") for w in res.warnings)
    assert any("plain.gone" in w for w in res.warnings)


def test_the_template_default_is_read_from_the_template_file(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.undo()
    assert v._template_default() == "deleted_at"  # the value databases/_template/db.yaml gives every new database
