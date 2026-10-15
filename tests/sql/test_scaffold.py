"""Phase 2: ensure_db_folder / scaffold_if_enabled (no DB, no network, writes only to tmp_path)."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.core.config import settings
from src.sql import registry
from src.sql.introspection import scaffold
from src.sql.introspection.scaffold import (
    ScaffoldResult,
    display_name_from_id,
    ensure_db_folder,
    scaffold_if_enabled,
)

REAL_TEMPLATE = Path(__file__).resolve().parents[2] / "databases" / "_template"


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private databases/ dir holding a copy of the real template."""
    base = tmp_path / "databases"
    shutil.copytree(REAL_TEMPLATE, base / "_template")
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    monkeypatch.setattr(settings, "db_engine", "mysql")
    return base


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _no_temp_dirs(base: Path) -> bool:
    return not [p for p in base.iterdir() if p.name.startswith("_scaffold")]


# ------------------------------------------------------------------ creating a folder


def test_creates_complete_folder_with_identity_fields(dbs: Path):
    result = ensure_db_folder("my_crm", engine="postgres")  # alias must normalise

    assert result == ScaffoldResult("my_crm", dbs / "my_crm", created=True)
    for rel in (
        "db.yaml",
        "schema/schema.json",
        "schema/relationships.json",
        "semantics/glossary.json",
        "semantics/column_glossary.json",
        "semantics/behavioral_atlas.json",
        "semantics/routing_hints.json",
    ):
        assert (dbs / "my_crm" / rel).is_file(), rel

    meta = registry.get_database("my_crm", databases_dir=dbs)  # same validation the app applies
    assert meta["id"] == "my_crm" and meta["display_name"] == "My Crm"
    assert meta["engine"] == "postgresql" and meta["connection"] == "my_crm" and meta["description"] == ""
    # everything else is the template's own defaults
    template_meta = yaml.safe_load((dbs / "_template" / "db.yaml").read_text(encoding="utf-8"))
    for key in ("enabled", "soft_delete", "default_filters", "allowed_users", "schema_sync"):
        assert meta[key] == template_meta[key], key
    assert _no_temp_dirs(dbs)


def test_template_itself_is_not_modified(dbs: Path):
    before = _snapshot(dbs / "_template")
    ensure_db_folder("my_crm", engine="mysql")
    assert _snapshot(dbs / "_template") == before


def test_display_name_override_and_default_derivation(dbs: Path):
    assert display_name_from_id("sales_dw_2") == "Sales Dw 2"
    ensure_db_folder("sales_dw", engine="mysql", display_name="Sales Warehouse")
    assert registry.get_database("sales_dw", databases_dir=dbs)["display_name"] == "Sales Warehouse"


def test_engine_comes_from_the_databases_own_connection_not_the_global_default(dbs: Path, monkeypatch):
    monkeypatch.setattr(registry, "get_connection", lambda db_id, *a, **k: {"engine": "mssql"})
    ensure_db_folder("hr_sql")
    assert registry.get_database("hr_sql", databases_dir=dbs)["engine"] == "mssql"  # global default is mysql


def test_engine_falls_back_to_global_default_without_a_connection(dbs: Path, monkeypatch):
    monkeypatch.setattr(registry, "get_connection", lambda db_id, *a, **k: {})
    ensure_db_folder("fresh_db")
    assert registry.get_database("fresh_db", databases_dir=dbs)["engine"] == "mysql"


def test_standard_subdirs_exist_even_if_the_template_lacks_them(dbs: Path):
    shutil.rmtree(dbs / "_template" / "learned")
    shutil.rmtree(dbs / "_template" / "evals")  # what the Docker image ships
    ensure_db_folder("img_db", engine="sqlite")
    for sub in ("schema", "semantics", "learned", "evals"):
        assert (dbs / "img_db" / sub).is_dir(), sub


# ------------------------------------------------------------------ never touching what exists


def test_existing_folder_is_left_byte_for_byte_untouched(dbs: Path):
    (dbs / "mine" / "schema").mkdir(parents=True)
    (dbs / "mine" / "db.yaml").write_text("id: mine\nengine: oracle\ndisplay_name: Hand Made\n", encoding="utf-8")
    (dbs / "mine" / "schema" / "schema.json").write_text('{"tables": [{"name": "keep_me"}]}', encoding="utf-8")
    (dbs / "mine" / "notes.txt").write_text("my own file", encoding="utf-8")
    before = _snapshot(dbs / "mine")

    result = ensure_db_folder("mine", engine="mysql", display_name="Overwritten?")

    assert result.created is False
    assert _snapshot(dbs / "mine") == before  # content AND mtimes: nothing was even rewritten
    assert "semantics/glossary.json" in result.missing_files  # reported, never filled in
    assert not (dbs / "mine" / "semantics").exists()
    assert _no_temp_dirs(dbs)


def test_existing_complete_folder_reports_nothing_missing(dbs: Path):
    shutil.copytree(dbs / "_template", dbs / "complete")
    result = ensure_db_folder("complete", engine="mysql")
    assert result.created is False and result.missing_files == ()


def test_second_call_is_a_noop(dbs: Path):
    assert ensure_db_folder("twice_db", engine="mysql").created is True
    snap = _snapshot(dbs / "twice_db")
    assert ensure_db_folder("twice_db", engine="postgresql").created is False
    assert _snapshot(dbs / "twice_db") == snap


def test_losing_a_creation_race_leaves_the_winners_folder_alone(dbs: Path, monkeypatch):
    winner = dbs / "raced"

    def rename_after_someone_else_created_it(src: Any, dst: Any) -> None:
        winner.mkdir()
        (winner / "theirs.txt").write_text("winner's file", encoding="utf-8")
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    monkeypatch.setattr(scaffold.os, "rename", rename_after_someone_else_created_it)
    result = ensure_db_folder("raced", engine="mysql")

    assert result.created is False
    assert (winner / "theirs.txt").read_text(encoding="utf-8") == "winner's file"
    assert _no_temp_dirs(dbs)


# ------------------------------------------------------------------ refusing and cleaning up


@pytest.mark.parametrize("bad", ["Bad-Name", "_template", "a", "has space", "../escape", ""])
def test_invalid_ids_are_rejected_and_nothing_is_created(dbs: Path, bad: str):
    before = sorted(p.name for p in dbs.iterdir())
    with pytest.raises(ValueError):
        ensure_db_folder(bad, engine="mysql")
    assert sorted(p.name for p in dbs.iterdir()) == before


def test_pinned_erp_main_is_never_created_but_an_existing_one_is_left_alone(dbs: Path):
    with pytest.raises(ValueError, match="pinned"):
        ensure_db_folder("erp_main", engine="mysql")
    assert not (dbs / "erp_main").exists()

    (dbs / "erp_main").mkdir()
    (dbs / "erp_main" / "db.yaml").write_text("id: erp_main\nengine: mysql\n", encoding="utf-8")
    before = _snapshot(dbs / "erp_main")
    assert ensure_db_folder("erp_main").created is False
    assert _snapshot(dbs / "erp_main") == before


def test_missing_template_fails_cleanly(dbs: Path):
    (dbs / "_template" / "db.yaml").unlink()
    with pytest.raises(FileNotFoundError, match="template"):
        ensure_db_folder("new_db", engine="mysql")
    assert not (dbs / "new_db").exists() and _no_temp_dirs(dbs)


def test_unsupported_engine_fails_cleanly(dbs: Path):
    with pytest.raises(ValueError):
        ensure_db_folder("odd_db", engine="cockroachdb")
    assert not (dbs / "odd_db").exists() and _no_temp_dirs(dbs)


def test_failure_midway_leaves_no_partial_folder(dbs: Path, monkeypatch):
    def boom(*a: Any, **k: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(registry, "atomic_write_yaml", boom)
    with pytest.raises(OSError):
        ensure_db_folder("full_disk", engine="mysql")
    assert not (dbs / "full_disk").exists() and _no_temp_dirs(dbs)


def test_in_progress_temp_folder_is_invisible_to_list_databases(dbs: Path):
    (dbs / "_scaffold_half_x1").mkdir()
    (dbs / "_scaffold_half_x1" / "db.yaml").write_text("id: half\nengine: mysql\n", encoding="utf-8")
    assert registry.list_databases(databases_dir=dbs) == []


# ------------------------------------------------------------------ the sync-time wrapper


def test_auto_scaffold_is_a_known_per_db_flag_and_pinned_off_for_erp_main():
    from src.utils.feature_flags import DB_FEATURE_FLAGS, is_db_feature_enabled

    assert "auto_scaffold" in DB_FEATURE_FLAGS
    assert is_db_feature_enabled("auto_scaffold", "erp_main") is False


def test_wrapper_does_nothing_when_flag_is_off(dbs: Path, monkeypatch):
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: False)
    assert scaffold_if_enabled("new_db") is None
    assert not (dbs / "new_db").exists()


def test_wrapper_scaffolds_and_clears_caches_when_flag_is_on(dbs: Path, monkeypatch):
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_scaffold")
    cleared = MagicMock()
    monkeypatch.setattr("src.sql.pipeline.clear_knowledge_caches", cleared)

    result = scaffold_if_enabled("new_db")

    assert result is not None and result.created is True and (dbs / "new_db" / "db.yaml").is_file()
    cleared.assert_called_once_with("new_db")


def test_wrapper_does_not_clear_caches_for_an_existing_folder(dbs: Path, monkeypatch):
    shutil.copytree(dbs / "_template", dbs / "old_db")
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: True)
    cleared = MagicMock()
    monkeypatch.setattr("src.sql.pipeline.clear_knowledge_caches", cleared)
    assert scaffold_if_enabled("old_db").created is False
    cleared.assert_not_called()


def test_wrapper_never_raises(dbs: Path, monkeypatch):
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: True)
    (dbs / "_template" / "db.yaml").unlink()  # forces a failure inside ensure_db_folder
    assert scaffold_if_enabled("new_db") is None


# ------------------------------------------------------------------ hook inside sync_live_schema


@pytest.mark.asyncio
async def test_sync_scaffolds_before_any_query_and_survives_a_scaffold_failure(monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    order: list[str] = []
    monkeypatch.setattr(
        "src.sql.introspection.scaffold.scaffold_if_enabled", lambda db_id: order.append(f"scaffold:{db_id}")
    )

    async def fake_run(sql: str, *a: Any, **kw: Any):
        order.append("query")
        return [{"name": "orders", "sql": "CREATE TABLE orders (id INT)"}]

    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", fake_run)
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": "sqlite"})
    monkeypatch.setattr("src.sql.generation.fetch_sqlite_foreign_keys", AsyncMock(return_value=[]))
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(return_value=([[0.1] * 8], [None]))

    result = await sync_live_schema(embedding_service=emb, vector_store=AsyncMock(), db_id="shop_db")

    assert result["status"] == "ok"
    assert order[0] == "scaffold:shop_db" and "query" in order[1:]


@pytest.mark.asyncio
async def test_sync_result_is_unchanged_by_the_hook_when_flag_is_off(monkeypatch):
    """Default state: no folder is created and the returned dict has exactly the same keys as before."""
    from src.sql.schema_retrieval import sync_live_schema

    created = []
    monkeypatch.setattr(scaffold, "ensure_db_folder", lambda *a, **k: created.append(a))  # must never run
    monkeypatch.setattr(
        "src.sql.schema_retrieval.run_readonly_query",
        AsyncMock(return_value=[{"name": "orders", "sql": "CREATE TABLE orders (id INT)"}]),
    )
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": "sqlite"})
    monkeypatch.setattr("src.sql.generation.fetch_sqlite_foreign_keys", AsyncMock(return_value=[]))
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(return_value=([[0.1] * 8], [None]))

    result = await sync_live_schema(embedding_service=emb, vector_store=AsyncMock(), db_id="shop_db")

    assert created == []
    assert set(result) == {"status", "db_id", "document_id", "tables_synced", "table_names"}
