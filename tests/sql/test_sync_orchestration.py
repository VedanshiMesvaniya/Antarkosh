"""Phase 11: Sync Schema under a guard - snapshot, validate, roll back, recover, clear caches, report.

The first groups test each piece on a temporary databases folder; the last group runs the real sync (real SQLite) through it.
"""
# ruff: noqa: F811, F401

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from src.sql import registry
from src.sql.sync import orchestrate
from src.sql.sync import report as report_mod
from src.sql.sync import validate as validate_mod
from src.sql.sync.guard import GENERATED_FILES, SyncBusy, SyncGuard, is_generated
from src.sql.sync.validate import validate_generation
from tests.sql.test_catalog_extraction import _sync_args, dbs, sqlite_db

DB = "p11_db"
FIXTURES = Path(__file__).parent / "fixtures" / "relationships"


def _doc(by: str, **extra: Any) -> bytes:
    return json.dumps({"generated_by": by, **extra}).encode("utf-8")


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    (base / DB / "schema").mkdir(parents=True)
    (base / DB / "semantics").mkdir()
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    return base / DB


def _put(root: Path, rel: str, data: bytes | str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


# ===================================================================== the list of generated files


def test_the_generated_file_list_matches_what_the_phases_really_write() -> None:
    from src.sql.introspection import catalog
    from src.sql.mining import evidence
    from src.sql.paths import writer as paths_writer
    from src.sql.profiles import writer as profiles_writer
    from src.sql.relationships import writer as rel_writer
    from src.sql.sa import procedures_phase
    from src.sql.semantics import dictionary, hints

    expected = {
        catalog.SCHEMA_REL: catalog.GENERATED_BY,
        catalog.INVENTORY_REL: catalog.GENERATED_BY,
        procedures_phase.MANIFEST_REL: procedures_phase.GENERATED_BY,
        evidence.EVIDENCE_REL: evidence.GENERATED_BY,
        hints.HINTS_REL: hints.GENERATED_BY,
        rel_writer.INFERRED_REL: rel_writer.GENERATED_BY,
        rel_writer.APPLIED_REL: rel_writer.GENERATED_BY,
        profiles_writer.PROFILES_REL: profiles_writer.GENERATED_BY,
        paths_writer.PATHS_REL: paths_writer.GENERATED_BY,
        dictionary.DICTIONARY_REL: dictionary.GENERATED_BY,
    }
    assert {rel: next(iter(by)) for rel, by in GENERATED_FILES.items()} == expected
    assert "semantics/column_glossary.json" not in GENERATED_FILES  # curated and generated entries are mixed: never rolled back


def test_is_generated_only_trusts_the_marker_for_that_file() -> None:
    assert is_generated("schema/join_paths.json", _doc("auto_join_paths"))
    assert not is_generated("schema/join_paths.json", _doc("auto_relationships"))  # another phase's marker
    assert not is_generated("schema/join_paths.json", b'{"pairs": []}') and not is_generated("schema/join_paths.json", b"{broken")
    assert not is_generated("schema/join_paths.json", None) and not is_generated("elsewhere.json", _doc("auto_join_paths"))


# ===================================================================== the guard


def test_the_guard_does_nothing_for_a_missing_folder_or_the_pinned_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    for db in ("nofolder", "erp_main"):
        (tmp_path / "erp_main").mkdir(exist_ok=True)
        g = SyncGuard(db)
        assert not g.active
        g.acquire(), g.begin()
        assert g.rollback() == {"restored": [], "removed": [], "kept": []} and not (tmp_path / db / ".sync").exists()


def test_a_second_sync_for_the_same_database_is_refused_until_the_first_ends(root: Path) -> None:
    first, second = SyncGuard(DB), SyncGuard(DB)
    first.acquire()
    with pytest.raises(SyncBusy):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_begin_snapshots_generated_files_and_commit_drops_the_snapshot(root: Path) -> None:
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", pairs=[]))
    _put(root, "schema/relationships.json", '{"relationships": []}')  # curated: no marker
    g = SyncGuard(DB)
    g.begin()
    assert g.interrupted() and (root / ".sync/pending/schema/join_paths.json").is_file()
    assert (root / ".sync/pending/schema/relationships.json").is_file()  # every existing file is copied, so a replaced placeholder can be put back
    manifest = json.loads((root / ".sync/pending/manifest.json").read_text())
    assert manifest["files"]["schema/join_paths.json"]["ours"] is True and manifest["files"]["schema/table_profiles.json"]["existed"] is False
    g.commit()
    assert not g.interrupted() and not (root / ".sync/pending").exists()


def test_rollback_restores_changed_files_removes_new_ones_and_never_touches_anyone_elses(root: Path) -> None:
    old_paths = _doc("auto_join_paths", pairs=["old"])
    _put(root, "schema/join_paths.json", old_paths)
    _put(root, "schema/table_profiles.json", _doc("auto_table_profiles", v=1))
    _put(root, "schema/relationships.json", '{"relationships": [{"curated": true}]}')  # a person's file
    g = SyncGuard(DB)
    g.begin()
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", pairs=["new"]))  # rewritten by the sync
    _put(root, "schema/schema.json", _doc("antarkosh-auto-catalog", tables=[]))  # created by the sync
    _put(root, "schema/table_profiles.json", b'{"edited": "by a person during the sync"}')  # replaced by a person
    assert g.changed_files() == {"schema/join_paths.json", "schema/schema.json", "schema/table_profiles.json"}
    out = g.rollback()
    assert out == {"restored": ["schema/join_paths.json"], "removed": ["schema/schema.json"], "kept": ["schema/table_profiles.json"]}
    assert (root / "schema/join_paths.json").read_bytes() == old_paths and not (root / "schema/schema.json").exists()
    assert json.loads((root / "schema/table_profiles.json").read_text()) == {"edited": "by a person during the sync"}
    assert "curated" in (root / "schema/relationships.json").read_text() and not g.interrupted()


def test_a_placeholder_a_phase_replaced_is_put_back_and_a_curated_file_a_person_changed_is_not_touched(root: Path) -> None:
    placeholder = '{"tables": []}'
    _put(root, "schema/schema.json", placeholder)  # what a fresh template folder holds
    _put(root, "schema/relationships.json", '{"relationships": []}')
    g = SyncGuard(DB)
    g.begin()
    _put(root, "schema/schema.json", _doc("antarkosh-auto-catalog", tables=[{"name": "t"}]))  # the catalog phase replaced it
    _put(root, "schema/relationships.json", '{"relationships": [{"edited": true}]}')  # a person edited a curated file
    out = g.rollback()
    assert out["restored"] == ["schema/schema.json"] and out["kept"] == ["schema/relationships.json"]
    assert (root / "schema/schema.json").read_text() == placeholder and "edited" in (root / "schema/relationships.json").read_text()


def test_a_run_that_died_is_recovered_by_the_next_guard(root: Path) -> None:
    good = _doc("auto_join_paths", pairs=["good"])
    _put(root, "schema/join_paths.json", good)
    dead = SyncGuard(DB)
    dead.acquire()
    dead.begin()
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", pairs=["half written generation"]))
    dead.release()  # the process died: no commit, no rollback
    nxt = SyncGuard(DB)
    nxt.acquire()
    assert nxt.interrupted()
    assert nxt.recover()["restored"] == ["schema/join_paths.json"] and (root / "schema/join_paths.json").read_bytes() == good and not nxt.interrupted()
    nxt.release()


def test_changed_files_is_empty_without_a_snapshot(root: Path) -> None:
    assert SyncGuard(DB).changed_files() == set()


# ===================================================================== validation


def _catalog_files(root: Path, name: str = "employees") -> tuple[dict, str]:
    """Write the schema.json of a public fixture catalog (marked as generated) and return it with its inputs hash."""
    from src.sql.relationships.model import load_graph

    schema = {**json.loads((FIXTURES / f"{name}.schema.json").read_text()), "generated_by": "antarkosh-auto-catalog"}
    _put(root, "schema/schema.json", json.dumps(schema))
    graph = load_graph(DB)
    assert graph is not None
    return schema, graph.inputs_hash


def test_a_file_this_sync_wrote_that_is_not_json_is_an_error_but_an_old_one_is_a_warning(root: Path) -> None:
    _put(root, "schema/join_paths.json", "{not json")
    assert [e for e in validate_generation(DB, changed={"schema/join_paths.json"}).errors if "not valid JSON" in e]
    old = validate_generation(DB, changed=set())
    assert old.ok and any("left over" in w for w in old.warnings)


def test_generated_relationships_that_name_a_missing_table_or_column_are_errors_only_when_written_now(root: Path) -> None:
    schema, _ = _catalog_files(root)
    table = schema["tables"][0]
    good = {"from_table": table["name"], "from_column": table["columns"][0]["name"], "to_table": table["name"], "to_column": table["columns"][0]["name"]}
    bad = {"from_table": "ghost", "from_column": "id", "to_table": table["name"], "to_column": "nope"}
    _put(root, "schema/relationships.json", _doc("auto_relationships", relationships=[good, bad]))
    now = validate_generation(DB, changed={"schema/relationships.json"})
    assert not now.ok and any("ghost" in e for e in now.errors) and any("nope" in e for e in now.errors)
    later = validate_generation(DB, changed=set())
    assert later.ok and any("ghost" in w for w in later.warnings)  # not rewritten this sync: reported, never rolled back


def test_join_paths_naming_a_table_the_catalog_lacks_is_an_error(root: Path) -> None:
    schema, _ = _catalog_files(root)
    real = schema["tables"][0]["name"]
    doc = _doc("auto_join_paths", pairs=[{"a": real, "b": "ghost", "routes": [{"tables": [real, "dbo." + real, "ghost"]}]}])
    _put(root, "schema/join_paths.json", doc)
    result = validate_generation(DB, changed={"schema/join_paths.json"})
    assert not result.ok and any("ghost" in e for e in result.errors) and not any(f"'{real}'" in e for e in result.errors)


def test_files_built_from_an_older_catalog_are_stale_warnings_and_matching_ones_are_coherent(root: Path) -> None:
    _, current = _catalog_files(root)
    _put(root, "schema/table_profiles.json", _doc("auto_table_profiles", inputs_hash=current))
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", inputs_hash=current, pairs=[]))
    fresh = validate_generation(DB)
    assert fresh.ok and fresh.coherent is True and fresh.stale == []
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", inputs_hash="older", pairs=[]))
    stale = validate_generation(DB)
    assert stale.ok and stale.coherent is False and stale.stale == ["schema/join_paths.json"] and any("older catalog" in w for w in stale.warnings)


def test_validation_without_a_catalog_skips_cross_checks_and_findings_are_capped(root: Path) -> None:
    _put(root, "schema/relationships.json", _doc("auto_relationships", relationships=[{"from_table": f"t{i}"} for i in range(30)]))
    result = validate_generation(DB)
    assert result.ok and result.coherent is None  # nothing to compare against
    schema, _ = _catalog_files(root)
    many = [{"from_table": f"ghost{i}", "from_column": "x", "to_table": schema["tables"][0]["name"], "to_column": "y"} for i in range(30)]
    _put(root, "schema/relationships.json", _doc("auto_relationships", relationships=many))
    capped = validate_generation(DB, changed={"schema/relationships.json"})
    assert len(capped.errors) <= validate_mod.MAX_LISTED + 1 and any("more" in e for e in capped.errors)


# ===================================================================== report


def test_phase_status_distinguishes_off_ok_skipped_and_failed() -> None:
    ps = report_mod.phase_status
    assert ps(None) == ("off", "") and ps({"ok": True}) == ("ok", "")
    assert ps({"ok": True, "skipped": "no schema.json"}) == ("skipped", "no schema.json")
    assert ps({"ok": False, "error": "boom"}) == ("failed", "boom") and ps({"ok": False, "skipped": "time budget"}) == ("failed", "time budget")


def test_schema_fingerprint_changes_with_the_schema_not_the_order() -> None:
    a = {"t1": "TABLE t1 (a int)", "t2": "TABLE t2 (b int)"}
    assert report_mod.schema_fingerprint(a) == report_mod.schema_fingerprint(dict(reversed(a.items())))
    assert report_mod.schema_fingerprint(a) != report_mod.schema_fingerprint({**a, "t2": "TABLE t2 (b bigint)"})


def test_the_report_names_the_mode_counts_and_unknowns(root: Path) -> None:
    schema, _ = _catalog_files(root)
    names = [t["name"] for t in schema["tables"]]
    _put(root, "schema/relationships.json", _doc("auto_relationships", relationships=[{"from_table": names[0], "from_column": "a", "to_table": names[1], "to_column": "b"}]))
    result = {
        "status": "ok", "tables_synced": len(names),
        "catalog": {"ok": True, "tables": len(names), "views": 0, "foreign_keys": 5, "skipped_schemas": {"hr": "no permission"}, "problems": []},
        "procedures": {"ok": True, "mode": "full", "readable": 7, "unreadable": 2},
        "relationships": {"ok": True, "edges": 9, "applied_edges": 4, "direction_unknown": 3, "held_back": {"below_threshold": 2}},
        "table_profiles": {"ok": True, "tables": len(names), "undecided": 1},
        "join_paths": {"ok": False, "error": "route check failed"},
    }
    rep = report_mod.build_sync_report(result, db_id=DB, duration_s=3.14, validation=None, guard={"rolled_back": False}, sync_info={"schema_hash": "abc"})
    assert rep["mode"] == "full" and rep["status"] == "ok_with_warnings" and rep["duration_s"] == 3.1 and rep["schema_hash"] == "abc"
    assert rep["counts"]["tables"] == len(names) and rep["counts"]["relationships_applied"] == 4 and rep["counts"]["procedures_readable"] == 7
    assert rep["phases"]["join_paths"] == {"status": "failed", "detail": "route check failed"} and rep["phases"]["column_meaning"]["status"] == "off"
    text = " | ".join(rep["unknowns"])
    for needle in ("join_paths: failed", "schema 'hr' was not read", "2 could not be read", "3 edge(s) direction unknown", "1 group(s)", "appear in no applied relationship"):
        assert needle in text
    basic = report_mod.build_sync_report({"status": "ok", "tables_synced": 1, "procedures": {"ok": True, "mode": "basic"}}, db_id=DB, duration_s=0.0, validation=None)
    assert basic["mode"] == "basic" and basic["status"] == "ok"
    rolled = report_mod.build_sync_report({"status": "ok", "tables_synced": 1}, db_id=DB, duration_s=0.0, validation=None, guard={"rolled_back": True})
    assert rolled["status"] == "rolled_back"


def test_the_report_lists_routes_below_the_planner_floor_when_the_phase_10_tool_is_present(root: Path) -> None:
    pytest.importorskip("src.sql.paths.benchmark")
    pair = lambda s: {"a": "a", "b": "z", "routes": [{"score": s, "shape": "chain", "tables": ["a", "m", "z"], "joins": []}]}
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", pairs=[pair(0.6), pair(0.3), pair(0.35)]))
    rep = report_mod.build_sync_report({"status": "ok", "tables_synced": 1}, db_id=DB, duration_s=0.0, validation=None)
    assert rep["route_floor"] == {"routes": 3, "at_or_above_floor": 1, "floor": 0.45} and any("2 of 3 multi-join route(s)" in u for u in rep["unknowns"])


def test_record_sync_report_updates_db_yaml_and_keeps_the_old_hash_after_a_rollback(root: Path) -> None:
    path = root / "db.yaml"
    path.write_text(yaml.safe_dump({"enabled": True, "schema_sync": {"last_synced_at": "2026-01-01", "schema_hash": "old"}}), encoding="utf-8")
    rep = {"status": "ok", "mode": "basic", "duration_s": 1.0, "counts": {"tables": 3}, "phases": {"catalog": {"status": "ok"}}, "unknowns": ["x"], "validation": {"errors": []}, "guard": {}}
    assert report_mod.record_sync_report(DB, rep, committed=True, schema_hash="new")
    meta = yaml.safe_load(path.read_text())
    assert meta["schema_sync"]["schema_hash"] == "new" and meta["schema_sync"]["last_synced_at"] == "2026-01-01" and meta["enabled"] is True
    assert meta["schema_sync"]["mode"] == "basic" and meta["schema_sync"]["last_sync"]["phases"] == {"catalog": "ok"} and meta["schema_sync"]["last_sync"]["unknowns"] == 1
    assert report_mod.record_sync_report(DB, {**rep, "status": "rolled_back", "guard": {"rolled_back": True}}, committed=False, schema_hash="newer")
    meta = yaml.safe_load(path.read_text())
    assert meta["schema_sync"]["schema_hash"] == "new" and meta["schema_sync"]["last_sync"]["rolled_back"] is True  # not "newer"
    path.unlink()
    assert report_mod.record_sync_report(DB, rep, committed=True, schema_hash="x") is False  # no db.yaml: nothing to write, no error


# ===================================================================== orchestrator with a fake body


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(orchestrate, "orchestration_enabled", lambda db_id: True)
    cleared: list[str] = []
    monkeypatch.setattr(orchestrate, "refresh_caches", lambda db_id: cleared.append(db_id) or ["x"])
    return cleared


def _body(root: Path, *, write: dict[str, bytes] | None = None, result: dict | None = None, raises: BaseException | None = None):
    calls: list[int] = []

    async def body() -> dict[str, Any]:
        calls.append(1)
        for rel, data in (write or {}).items():
            _put(root, rel, data)
        if raises:
            raise raises
        return result if result is not None else {"status": "ok", "tables_synced": 2, "_sync": {"schema_hash": "h1"}}

    body.calls = calls  # type: ignore[attr-defined]
    return body


def test_a_good_sync_is_committed_reported_and_clears_caches(root: Path, flag_on: list[str]) -> None:
    (root / "db.yaml").write_text(yaml.safe_dump({"enabled": True}), encoding="utf-8")
    body = _body(root, write={"schema/join_paths.json": _doc("auto_join_paths", pairs=[])})
    result = asyncio.run(orchestrate.run_orchestrated(body, DB))
    assert result["status"] == "ok" and "_sync" not in result and result["orchestration"]["status"] in ("ok", "ok_with_warnings")
    assert result["orchestration"]["guard"]["changed_files"] == ["schema/join_paths.json"] and result["orchestration"]["schema_hash"] == "h1"
    assert (root / "schema/join_paths.json").exists() and not (root / ".sync/in_progress.json").exists() and not (root / ".sync/pending").exists()
    assert flag_on == [DB] and yaml.safe_load((root / "db.yaml").read_text())["schema_sync"]["schema_hash"] == "h1"


def test_a_crash_in_the_body_puts_the_last_good_files_back_and_still_raises(root: Path, flag_on: list[str]) -> None:
    old = _doc("auto_join_paths", pairs=["good"])
    _put(root, "schema/join_paths.json", old)
    body = _body(root, write={"schema/join_paths.json": _doc("auto_join_paths", pairs=["half"]), "schema/schema.json": _doc("antarkosh-auto-catalog", tables=[])}, raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orchestrate.run_orchestrated(body, DB))
    assert (root / "schema/join_paths.json").read_bytes() == old and not (root / "schema/schema.json").exists() and not (root / ".sync/in_progress.json").exists()
    assert flag_on == [DB]
    SyncGuard(DB).acquire()  # the lock was released


def test_a_broken_generation_is_rolled_back_and_the_stored_hash_stays_old(root: Path, flag_on: list[str]) -> None:
    (root / "db.yaml").write_text(yaml.safe_dump({"enabled": True, "schema_sync": {"schema_hash": "old"}}), encoding="utf-8")
    old = _doc("auto_join_paths", pairs=["good"])
    _put(root, "schema/join_paths.json", old)
    body = _body(root, write={"schema/join_paths.json": b"{truncated"})
    result = asyncio.run(orchestrate.run_orchestrated(body, DB))
    rep = result["orchestration"]
    assert result["status"] == "ok" and rep["status"] == "rolled_back" and rep["guard"]["rolled_back"] is True and rep["validation"]["errors"]
    assert (root / "schema/join_paths.json").read_bytes() == old
    assert yaml.safe_load((root / "db.yaml").read_text())["schema_sync"]["schema_hash"] == "old"


def test_a_sync_that_did_not_finish_leaves_the_files_alone(root: Path, flag_on: list[str]) -> None:
    old = _doc("auto_join_paths", pairs=["good"])
    _put(root, "schema/join_paths.json", old)
    result = asyncio.run(orchestrate.run_orchestrated(_body(root, result={"status": "error", "message": "No schema rows"}), DB))
    assert result == {"status": "error", "message": "No schema rows"} and (root / "schema/join_paths.json").read_bytes() == old


def test_a_second_sync_while_one_runs_is_refused_without_running_the_body(root: Path, flag_on: list[str]) -> None:
    holder = SyncGuard(DB)
    holder.acquire()
    body = _body(root)
    result = asyncio.run(orchestrate.run_orchestrated(body, DB))
    assert result["status"] == "error" and "already running" in result["message"] and body.calls == []  # type: ignore[attr-defined]
    holder.release()


def test_an_interrupted_earlier_run_is_recovered_before_the_new_one_starts(root: Path, flag_on: list[str]) -> None:
    good = _doc("auto_join_paths", pairs=["good"])
    _put(root, "schema/join_paths.json", good)
    dead = SyncGuard(DB)
    dead.begin()
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", pairs=["half"]))
    result = asyncio.run(orchestrate.run_orchestrated(_body(root), DB))  # this body writes nothing
    assert result["orchestration"]["guard"]["recovered_previous"] is True and (root / "schema/join_paths.json").read_bytes() == good


def test_a_database_without_a_folder_still_syncs_with_a_report_and_no_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag_on: list[str]) -> None:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    result = asyncio.run(orchestrate.run_orchestrated(_body(tmp_path / "x"), "ghostdb"))
    assert result["status"] == "ok" and result["orchestration"]["mode"] == "basic" and "validation" not in result["orchestration"] and not (tmp_path / "ghostdb").exists()


def test_refresh_caches_clears_every_cache_and_survives_a_broken_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.pipeline import SQLRetriever
    from src.sql.semantic_cache import get_semantic_cache

    SQLRetriever._full_schema_cache["cachedb"] = "x"
    SQLRetriever._result_cache[("cachedb", "q")] = (0.0, [])
    cleared = orchestrate.refresh_caches("cachedb")
    assert set(cleared) == {"knowledge", "schema", "results", "semantic", "routes"}
    assert "cachedb" not in SQLRetriever._full_schema_cache and ("cachedb", "q") not in SQLRetriever._result_cache
    monkeypatch.setattr(get_semantic_cache(), "clear", lambda db_id=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert "semantic" not in orchestrate.refresh_caches("cachedb") and "schema" in orchestrate.refresh_caches("cachedb")


def test_the_flag_defaults_off_on_any_error_and_for_the_pinned_database(monkeypatch: pytest.MonkeyPatch) -> None:
    assert orchestrate.orchestration_enabled("erp_main") is False
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: (_ for _ in ()).throw(RuntimeError("x")))
    assert orchestrate.orchestration_enabled("anything") is False


# ===================================================================== the relationship text refresh


@pytest.mark.asyncio
async def test_only_tables_whose_relationship_lines_changed_are_re_embedded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.sql.schema_retrieval as sr
    from src.models.schemas import Chunk

    rel_file = tmp_path / "relationships.json"
    rel_file.write_text(json.dumps({"relationships": [{"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id"}]}), encoding="utf-8")
    monkeypatch.setattr(sr, "get_knowledge_path", lambda kind, db_id=None: rel_file)
    schemas = {"orders": "TABLE orders (\n  id int,\n  customer_id int\n)", "customer": "TABLE customer (\n  id int\n)"}
    before = {n: sr._enrich_table_schema(table_name=n, schema_text=t, table_meta=None, fk_lines=None) for n, t in schemas.items()}
    chunks = [Chunk(chunk_id=f"{DB}_schema_{n}", document_id="d", content=c, db_id=DB, source_file=f"database/{DB}/{n}") for n, c in before.items()]
    seen: list[list[str]] = []

    async def fake_reembed(emb: Any, store: Any, chs: list, extras: dict, tables: list[str]) -> int:
        seen.append(sorted(tables))
        return len(tables)

    monkeypatch.setattr(sr, "_reembed_changed_tables", fake_reembed)
    assert await sr._refresh_relationship_text(DB, AsyncMock(), AsyncMock(), chunks, schemas, {}, {}) == 1
    assert seen == [["orders"]] and "FOREIGN KEY (customer_id) REFERENCES customer(id)" in chunks[0].content and chunks[1].content == before["customer"]
    assert await sr._refresh_relationship_text(DB, AsyncMock(), AsyncMock(), chunks, schemas, {}, {}) == 0  # nothing left to change
    assert seen == [["orders"]]


# ===================================================================== the real sync (real SQLite) through the guard


def _flags(*on: str):
    return lambda flag, db: flag in on


@pytest.mark.asyncio
async def test_with_the_flag_off_the_sync_result_and_folder_are_exactly_as_before(sqlite_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert "orchestration" not in result and "_sync" not in result and not (dbs / "pg_live/.sync").exists()


@pytest.mark.asyncio
async def test_with_the_flag_on_the_real_sync_is_guarded_validated_and_reported(sqlite_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_relationships", "auto_sync_orchestration"))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    rep = result["orchestration"]
    assert result["status"] == "ok" and "_sync" not in result and rep["status"] in ("ok", "ok_with_warnings") and rep["mode"] == "basic"
    assert rep["counts"]["tables"] == 3 and rep["phases"]["relationships"]["status"] == "ok" and rep["validation"]["ok"] is True
    assert {"schema/schema.json", "schema/table_inventory.json"} <= set(rep["guard"]["changed_files"])
    assert not (dbs / "pg_live/.sync/in_progress.json").exists() and not (dbs / "pg_live/.sync/pending").exists()
    sync_meta = yaml.safe_load((dbs / "pg_live/db.yaml").read_text())["schema_sync"]
    assert sync_meta["schema_hash"] == rep["schema_hash"] and sync_meta["mode"] == "basic" and sync_meta["last_sync"]["tables"] == 3 and sync_meta["last_synced_at"]


@pytest.mark.asyncio
async def test_a_phase_that_writes_garbage_is_rolled_back_by_the_real_sync(sqlite_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.sa import relationships_phase
    from src.sql.schema_retrieval import sync_live_schema

    async def corrupt(**_kw: Any) -> dict[str, Any]:
        (dbs / "pg_live/schema/inferred_relationships.json").write_text("{half a file", encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(relationships_phase, "run_relationships_phase", corrupt)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_relationships", "auto_sync_orchestration"))
    placeholder = (dbs / "pg_live/schema/schema.json").read_bytes()  # the fresh template folder's empty catalog
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["orchestration"]["status"] == "rolled_back"
    assert not (dbs / "pg_live/schema/inferred_relationships.json").exists()  # the garbage is gone
    assert (dbs / "pg_live/schema/schema.json").read_bytes() == placeholder  # the catalog this sync wrote is undone: back to "nothing generated yet"
    assert "schema_hash" not in (yaml.safe_load((dbs / "pg_live/db.yaml").read_text())["schema_sync"] or {}) or yaml.safe_load((dbs / "pg_live/db.yaml").read_text())["schema_sync"]["schema_hash"] is None


@pytest.mark.asyncio
async def test_a_process_killed_mid_sync_is_repaired_by_the_next_sync(sqlite_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.sa import relationships_phase
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", _flags("auto_catalog", "auto_relationships", "auto_sync_orchestration"))
    first = await sync_live_schema(db_id="pg_live", **_sync_args())
    good = (dbs / "pg_live/schema/schema.json").read_bytes()
    assert first["orchestration"]["validation"]["ok"]
    monkeypatch.setattr(relationships_phase, "run_relationships_phase", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await sync_live_schema(db_id="pg_live", **_sync_args())
    assert (dbs / "pg_live/schema/schema.json").read_bytes() == good  # rolled back, as if the second sync never ran
