"""Phase 13: drift, the sync record, dependency-aware skipping, kept versions with rollback, stale chunks, the orchestrator hook.

Pieces are tested on a temporary databases folder with synthetic files; the real-sync tests live in test_resync_e2e.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from src.sql import registry
from src.sql.sync import drift, modes, orchestrate, resync, versions
from src.sql.sync.guard import GENERATED_FILES, SyncGuard
from src.sql.sync.stale import cleanup_stale_chunks

DB = "p13_db"


# ----------------------------------------------------------------------------------------------- helpers


def _doc(by: str, **extra: Any) -> bytes:
    return json.dumps({"generated_by": by, **extra}).encode("utf-8")


def _put(root: Path, rel: str, data: bytes | str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    (base / DB / "schema").mkdir(parents=True)
    (base / DB / "semantics").mkdir()
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    return base / DB


def _flags(monkeypatch: pytest.MonkeyPatch, *on: str) -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in on)


def _catalog_files(root: Path, marker: str = "antarkosh-auto-catalog", extra: str = "") -> None:
    _put(root, "schema/schema.json", _doc(marker, tables=[{"name": "a"}], x=extra))
    _put(root, "schema/table_inventory.json", _doc(marker, tables=[{"name": "a"}], x=extra))


# ----------------------------------------------------------------------------------------------- drift


def test_fingerprints_follow_the_columns_and_the_declared_keys_not_the_order() -> None:
    a = drift.table_fingerprints({"t": "TABLE t (id int)", "u": "TABLE u (id int)"}, [])
    assert a == drift.table_fingerprints({"u": "TABLE u (id int)", "t": "TABLE t (id int)"}, [])
    assert a["t"] != drift.table_fingerprints({"t": "TABLE t (id bigint)"}, [])["t"]  # a type changed
    fk = {"table_name": "t", "column_name": "u_id", "referenced_table_name": "u", "referenced_column_name": "id"}
    assert drift.table_fingerprints({"t": "TABLE t (id int)"}, [fk])["t"] != a["t"]  # a declared key was added
    upper = {k.upper(): v for k, v in fk.items()}
    assert drift.table_fingerprints({"t": "TABLE t (id int)"}, [upper]) == drift.table_fingerprints({"t": "TABLE t (id int)"}, [fk])


def test_structure_hash_changes_when_any_table_does() -> None:
    base = {"a": "1", "b": "2"}
    assert drift.structure_hash(base) == drift.structure_hash({"B": "2", "A": "1"})
    assert drift.structure_hash(base) != drift.structure_hash({"a": "1", "b": "3"})
    assert drift.structure_hash(base) != drift.structure_hash({"a": "1"})


def test_diff_names_added_removed_and_changed_tables_in_their_original_spelling() -> None:
    old = {"Keep": "1", "Gone": "2", "Edit": "3"}
    new = {"keep": "1", "edit": "9", "Fresh": "4"}
    d = drift.diff_tables(old, new)
    assert (d.added, d.removed, d.changed, d.unchanged) == (("Fresh",), ("Gone",), ("edit",), 1)
    assert d.has_changes and d.as_dict()["counts"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}
    first = drift.diff_tables(None, new)
    assert first.first_sync and not first.has_changes and first.unchanged == 3
    assert not drift.diff_tables(new, new).has_changes


def test_long_lists_are_capped_in_the_report() -> None:
    d = drift.diff_tables({}, {f"t{i}": "x" for i in range(30)}).as_dict()
    assert len(d["added"]) == drift.MAX_LISTED + 1 and d["added"][-1].endswith("more") and d["counts"]["added"] == 30


# ----------------------------------------------------------------------------------------------- the record


def _state(**over: Any) -> dict[str, Any]:
    base = {"schema_hash": "h", "structure_hash": "s", "tables": {"a": "1"}, "files": {}, "phases": {"catalog": "ok"}, "enabled": ["catalog"], "phase_versions": {}, "clean": True}
    return {**base, **over}


def test_the_record_round_trips_and_stamps_its_time(root: Path) -> None:
    assert drift.save_state(DB, _state()) is True
    loaded = drift.load_state(DB)
    assert loaded["tables"] == {"a": "1"} and loaded["version"] == drift.STATE_VERSION and loaded["committed_at"].endswith("Z")


def test_a_broken_or_foreign_record_is_no_record(root: Path) -> None:
    assert drift.load_state(DB) is None
    for text in ("{broken", "[]", json.dumps({"version": 99}), json.dumps({"version": 1, "tables": {}})):
        _put(root, ".sync/state.json", text)
        assert drift.load_state(DB) is None


def test_the_pinned_database_and_a_missing_folder_are_never_recorded(root: Path, tmp_path: Path) -> None:
    (root.parent / "erp_main").mkdir()
    assert drift.save_state("erp_main", _state()) is False and drift.load_state("erp_main") is None
    assert drift.save_state("nofolder", _state()) is False


def test_invalidating_keeps_the_record_but_makes_it_untrusted(root: Path) -> None:
    drift.save_state(DB, _state())
    first = drift.load_state(DB)["committed_at"]
    assert drift.invalidate_state(DB, "rolled back to v0001") is True
    after = drift.load_state(DB)
    assert after["clean"] is False and after["invalid_reason"] == "rolled back to v0001" and after["committed_at"] == first
    assert drift.invalidate_state("nofolder", "x") is False


# ----------------------------------------------------------------------------------------------- the switches


@pytest.mark.parametrize(
    ("flags", "resync", "stale"),
    [
        ((), "", ""),
        (("auto_resync_apply", "auto_stale_chunk_cleanup"), "", ""),  # the apply flags alone do nothing
        (("auto_sync_orchestration",), "shadow", "shadow"),
        (("auto_sync_orchestration", "auto_resync_apply"), "apply", "shadow"),
        (("auto_sync_orchestration", "auto_stale_chunk_cleanup"), "shadow", "apply"),
        (("auto_sync_orchestration", "auto_resync_apply", "auto_stale_chunk_cleanup"), "apply", "apply"),
    ],
)
def test_the_two_phase_13_switches_are_shadow_until_their_own_flag_is_on(monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...], resync: str, stale: str) -> None:
    _flags(monkeypatch, *flags)
    assert modes.resync_mode(DB) == resync and modes.stale_chunks_mode(DB) == stale


def test_the_apply_flags_start_off_so_turning_orchestration_on_never_changes_a_sync() -> None:
    from src.utils.feature_flags import DEFAULT_NEW_DB_FLAGS, OFF_BY_DEFAULT_FLAGS

    assert {"auto_resync_apply", "auto_stale_chunk_cleanup"} <= OFF_BY_DEFAULT_FLAGS
    assert DEFAULT_NEW_DB_FLAGS["auto_resync_apply"] is False and DEFAULT_NEW_DB_FLAGS["auto_stale_chunk_cleanup"] is False
    assert DEFAULT_NEW_DB_FLAGS["auto_sync_orchestration"] is True


def test_the_pinned_database_is_never_on() -> None:
    assert modes.resync_mode("erp_main") == "" and modes.stale_chunks_mode("erp_main") == ""  # the real flag lookup: pinned off


# ----------------------------------------------------------------------------------------------- content digests


def _stamped(stamp: str, **fields: Any) -> bytes:
    return json.dumps({"generated_by": "x", "generated_at": stamp, **fields}).encode("utf-8")


def test_content_digest_ignores_the_generation_stamp_and_key_order_but_nothing_else() -> None:
    a = _stamped("2026-01-01T00:00:00Z", tables=[1])
    b = json.dumps({"tables": [1], "generated_at": "2030-12-31T23:59:59Z", "generated_by": "x"}).encode("utf-8")
    assert drift.content_digest(a) == drift.content_digest(b)  # same findings, different clock and key order
    assert drift.content_digest(a) != drift.content_digest(_stamped("2026-01-01T00:00:00Z", tables=[2]))  # a real change
    nested = lambda stamp: json.dumps({"generated_by": "x", "tables": [{"generated_at": stamp}]}).encode("utf-8")
    assert drift.content_digest(nested("t1")) != drift.content_digest(nested("t2"))  # only the top-level stamp is ignored


def test_content_digest_handles_files_that_are_not_json_objects() -> None:
    import hashlib

    assert drift.content_digest(None) is None
    raw = b"not json \xff\xfe"
    assert drift.content_digest(raw) == hashlib.sha256(raw).hexdigest()
    assert drift.content_digest(b"plain text") != drift.content_digest(b"other text")
    assert drift.content_digest(b"[1, 2]") == drift.content_digest(b"[1,2]")  # a JSON list is digested whole, spacing aside
    assert drift.content_digest(b"[1, 2]") != drift.content_digest(b"[1, 3]")


def test_the_tracked_file_digests_do_not_move_when_only_a_stamp_does(root: Path) -> None:
    _put(root, "schema/schema.json", _stamped("2026-01-01T00:00:00Z", tables=["a"]))
    before = resync.file_digests(DB)["schema/schema.json"]
    _put(root, "schema/schema.json", _stamped("2026-06-30T10:11:12Z", tables=["a"]))
    assert resync.file_digests(DB)["schema/schema.json"] == before
    _put(root, "schema/schema.json", _stamped("2026-06-30T10:11:12Z", tables=["a", "b"]))
    assert resync.file_digests(DB)["schema/schema.json"] != before


# ----------------------------------------------------------------------------------------------- the planner


def _write_generation(root: Path) -> None:
    """A generation the planner can compare with: every skippable phase's outputs and inputs exist."""
    _catalog_files(root)
    _put(root, "schema/inferred_relationships.json", _doc("auto_relationships", edges=[]))
    _put(root, "schema/table_profiles.json", _doc("auto_table_profiles", tables={}))
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", pairs=[]))
    _put(root, "schema/sp_evidence.json", _doc("auto_sp_mining", facts=[]))


def _planner(root: Path, mode: str = "apply", **over: Any) -> resync.ResyncPlanner:
    state = {
        "schema_hash": "h",
        "structure_hash": "S",
        "tables": {"a": "1"},
        "files": resync.file_digests(DB),
        "phases": {"catalog": "ok", "relationships": "ok", "table_profiles": "ok", "join_paths": "ok"},
        "enabled": ["catalog", "join_paths", "relationships", "table_profiles"],
        "phase_versions": {"relationships": 2},
        "clean": True,
        "committed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": drift.STATE_VERSION,
    }
    kwargs: dict[str, Any] = {"mode": mode, "state": state, "structure": "S", "enabled": ["catalog", "join_paths", "relationships", "table_profiles"], "versions": {"relationships": 2}}
    for key in list(over):
        if key in ("state_patch",):
            state.update(over.pop(key))
    kwargs.update(over)
    return resync.ResyncPlanner(DB, **kwargs)


def test_nothing_changed_means_every_skippable_phase_is_skipped_in_apply_mode(root: Path) -> None:
    _write_generation(root)
    p = _planner(root)
    assert [p.should_run(ph) for ph in resync.SKIPPABLE] == [False] * 4
    assert p.summary()["skipped"] == list(resync.SKIPPABLE) and p.summary()["ran"] == {}
    assert p.skipped_report("catalog") == {"ok": True, "skipped": "unchanged since the last sync", "unchanged": True, "phase": "catalog"}


def test_shadow_mode_only_records_what_it_would_skip(root: Path) -> None:
    _write_generation(root)
    p = _planner(root, mode="shadow")
    assert all(p.should_run(ph) for ph in resync.SKIPPABLE)  # always runs
    assert p.summary()["would_skip"] == list(resync.SKIPPABLE) and p.summary()["skipped"] == []


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"state": None}, "no earlier sync"),
        ({"state_patch": {"clean": False}}, "not clean"),
        ({"state_patch": {"clean": False, "invalid_reason": "rolled back to v0002"}}, "rolled back to v0002"),
        ({"structure": "OTHER"}, "structure changed"),
        ({"enabled": ["catalog"]}, "switched-on phases"),
        ({"versions": {"relationships": 3}}, "rules changed"),
        ({"state_patch": {"committed_at": (datetime.now(UTC) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")}}, "days old"),
        ({"state_patch": {"committed_at": "garbage"}}, "no valid date"),
    ],
)
def test_any_global_doubt_runs_every_phase(root: Path, over: dict[str, Any], why: str) -> None:
    _write_generation(root)
    p = _planner(root, **over)
    assert all(p.should_run(ph) for ph in resync.SKIPPABLE) and p.skipped == []
    assert why in p.summary()["ran"]["catalog"]


def test_a_phase_whose_last_result_was_not_ok_runs(root: Path) -> None:
    _write_generation(root)
    p = _planner(root, state_patch={"phases": {"catalog": "ok", "relationships": "failed", "table_profiles": "ok", "join_paths": "ok"}})
    assert p.should_run("relationships") is True and "failed" in p.ran["relationships"]
    assert p.should_run("table_profiles") is False


def test_an_edited_output_makes_that_phase_run(root: Path) -> None:
    _write_generation(root)
    p = _planner(root)
    _put(root, "schema/table_profiles.json", _doc("auto_table_profiles", tables={"x": {}}))  # someone changed it
    assert p.should_run("table_profiles") is True and "table_profiles.json was changed" in p.ran["table_profiles"]
    assert p.should_run("relationships") is False  # unrelated phases are still skipped


def test_a_changed_input_runs_every_phase_that_reads_it_and_only_those(root: Path) -> None:
    _write_generation(root)
    p = _planner(root)
    _put(root, "schema/sp_evidence.json", _doc("auto_sp_mining", facts=["new"]))  # procedure evidence changed this sync
    assert p.should_run("catalog") is False  # the catalog does not read the evidence
    assert [p.should_run(ph) for ph in ("relationships", "table_profiles", "join_paths")] == [True, True, True]
    assert "sp_evidence.json" in p.ran["relationships"]


def test_a_new_stamp_alone_is_not_a_changed_input(root: Path) -> None:
    _write_generation(root)
    p = _planner(root)
    _put(root, "schema/sp_evidence.json", _doc("auto_sp_mining", facts=[], generated_at="2031-01-01T00:00:00Z"))  # re-stamped, same findings
    _put(root, "schema/schema.json", _doc("antarkosh-auto-catalog", tables=[{"name": "a"}], x="", generated_at="2031-01-01T00:00:00Z"))
    assert [p.should_run(ph) for ph in resync.SKIPPABLE] == [False] * 4
    _put(root, "schema/sp_evidence.json", _doc("auto_sp_mining", facts=["new"], generated_at="2031-01-01T00:00:00Z"))  # a real change
    assert p.should_run("relationships") is True


def test_a_missing_output_is_a_change(root: Path) -> None:
    _write_generation(root)
    p = _planner(root)
    (root / "schema/join_paths.json").unlink()
    assert p.should_run("join_paths") is True


def test_phases_that_are_never_skipped_always_run(root: Path) -> None:
    _write_generation(root)
    p = _planner(root)
    assert p.should_run("column_meaning") is True and p.should_run("procedures") is True and p.skipped == []


def test_a_crash_while_planning_means_run_the_phase(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_generation(root)
    p = _planner(root)
    monkeypatch.setattr(p, "_reason", lambda phase: (_ for _ in ()).throw(RuntimeError("boom")))
    assert p.should_run("catalog") is True and resync.skip_phase(p, "catalog", True) is False


def test_skipped_phases_carry_their_last_result_into_the_next_record(root: Path) -> None:
    _write_generation(root)
    p = _planner(root, state_patch={"phases": {"catalog": "ok", "relationships": "skipped", "table_profiles": "ok", "join_paths": "ok"}})
    for ph in resync.SKIPPABLE:
        p.should_run(ph)
    assert p.carried_statuses()["relationships"] == "skipped" and p.carried_statuses()["catalog"] == "ok"


def test_the_skip_helper_ignores_phases_that_are_off(root: Path) -> None:
    _write_generation(root)
    p = _planner(root)
    assert resync.skip_phase(p, "catalog", enabled=False) is False and p.skipped == []
    assert resync.skip_phase(None, "catalog", enabled=True) is False


def test_start_resync_is_none_when_off_and_distrusts_a_cut_short_schema(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch)
    assert resync.start_resync(DB, {"t": "x"}, [], schema_complete=True) == (None, {})
    _flags(monkeypatch, "auto_sync_orchestration", "auto_catalog", "auto_resync_apply")
    drift.save_state(DB, _state(structure_hash=drift.structure_hash(drift.table_fingerprints({"t": "x"}, []))))
    planner, fps = resync.start_resync(DB, {"t": "x"}, [], schema_complete=False)
    assert planner is not None and fps and planner.should_run("catalog") is True and "cut short" in planner.ran["catalog"]


def test_the_phase_versions_are_the_rule_versions_of_the_derived_phases() -> None:
    assert set(drift.phase_versions()) == {"relationships", "table_profiles", "join_paths"} and all(isinstance(v, int) for v in drift.phase_versions().values())


# ----------------------------------------------------------------------------------------------- versions


def _generation(root: Path, tag: str) -> None:
    _catalog_files(root, extra=tag)
    _put(root, "schema/join_paths.json", _doc("auto_join_paths", pairs=[tag]))


def _keep(tag: str, structure: str = "S") -> dict[str, Any] | None:
    return versions.save_version(DB, schema_hash="h-" + tag, structure=structure, tables=1, note=tag)


def test_a_version_holds_the_generated_files_and_nothing_else(root: Path) -> None:
    _generation(root, "one")
    _put(root, "schema/relationships.json", '{"relationships": []}')  # curated: no marker
    v = _keep("one")
    assert v["id"] == "v0001" and v["files"] == 3
    listed = versions.list_versions(DB)
    assert [x["id"] for x in listed] == ["v0001"] and "schema/relationships.json" not in listed[0]["files"]
    assert not (root / ".sync/versions/v0001/files/schema/relationships.json").exists()


def test_a_second_identical_generation_is_not_stored_again(root: Path) -> None:
    _generation(root, "one")
    _keep("one")
    assert _keep("one") == {"id": "v0001", "unchanged": True}
    assert len(versions.list_versions(DB)) == 1
    _generation(root, "two")
    assert _keep("two")["id"] == "v0002"


def test_a_new_stamp_alone_does_not_create_a_new_version(root: Path) -> None:
    _put(root, "schema/schema.json", _doc("antarkosh-auto-catalog", tables=["a"], generated_at="2026-01-01T00:00:00Z"))
    assert _keep("one")["id"] == "v0001"
    _put(root, "schema/schema.json", _doc("antarkosh-auto-catalog", tables=["a"], generated_at="2026-02-02T02:02:02Z"))
    assert _keep("two") == {"id": "v0001", "unchanged": True}  # same findings, later clock: nothing new to keep
    _put(root, "schema/schema.json", _doc("antarkosh-auto-catalog", tables=["a", "b"], generated_at="2026-02-02T02:02:02Z"))
    assert _keep("three")["id"] == "v0002"


def test_a_version_still_restores_the_exact_bytes_it_was_saved_with(root: Path) -> None:
    original = _doc("antarkosh-auto-catalog", tables=["a"], generated_at="2026-01-01T00:00:00Z")
    _put(root, "schema/schema.json", original)
    _keep("one")
    _put(root, "schema/schema.json", _doc("antarkosh-auto-catalog", tables=["a", "b"], generated_at="2026-03-03T03:03:03Z"))
    versions.rollback_to_version(DB, "v0001")
    assert (root / "schema/schema.json").read_bytes() == original  # the integrity checksum is still over the raw bytes


def test_a_version_saved_before_content_digests_existed_is_replaced_once_not_trusted(root: Path) -> None:
    _generation(root, "one")
    _keep("one")
    manifest_path = root / ".sync/versions/v0001/version.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"].values():
        entry.pop("content")  # what an earlier release wrote
    manifest_path.write_text(json.dumps(manifest))
    assert _keep("again")["id"] == "v0002"  # one extra copy, then the new manifests carry the content digest
    assert _keep("again") == {"id": "v0002", "unchanged": True}


def test_nothing_generated_means_nothing_kept(root: Path) -> None:
    assert _keep("x") is None and versions.list_versions(DB) == []


def test_only_the_newest_versions_are_kept(root: Path) -> None:
    for i in range(versions.KEEP_VERSIONS + 3):
        _generation(root, f"g{i}")
        _keep(f"g{i}")
    ids = [v["id"] for v in versions.list_versions(DB)]
    assert len(ids) == versions.KEEP_VERSIONS and ids[0] == f"v{versions.KEEP_VERSIONS + 3:04d}"
    assert not (root / ".sync/versions/v0001").exists()


def test_an_interrupted_version_is_ignored_and_cleaned_up(root: Path) -> None:
    _generation(root, "one")
    (root / ".sync/versions/v0001/files/schema").mkdir(parents=True)  # a copy that never got its version.json
    assert versions.list_versions(DB) == []
    _generation(root, "two")
    assert _keep("two")["id"] == "v0002"
    assert not (root / ".sync/versions/v0001").exists()


def test_verify_finds_damage(root: Path) -> None:
    _generation(root, "one")
    _keep("one")
    manifest, problems = versions.verify_version(DB, "v0001")
    assert problems == [] and manifest["id"] == "v0001"
    (root / ".sync/versions/v0001/files/schema/join_paths.json").write_bytes(_doc("auto_join_paths", pairs=["tampered"]))
    assert any("checksum" in p for p in versions.verify_version(DB, "v0001")[1])
    (root / ".sync/versions/v0001/files/schema/schema.json").unlink()
    assert any("missing" in p for p in versions.verify_version(DB, "v0001")[1])
    for bad in ("v1", "../x", "", "v0099"):
        with pytest.raises(versions.VersionError):
            versions.verify_version(DB, bad)


def test_rollback_restores_removes_and_keeps_what_it_should(root: Path) -> None:
    _generation(root, "old")
    _keep("old")
    _generation(root, "new")
    _put(root, "schema/table_profiles.json", _doc("auto_table_profiles", tables={}))  # generated only after v0001
    _keep("new")
    drift.save_state(DB, _state(structure_hash="S"))
    (root / "db.yaml").write_text(yaml.safe_dump({"schema_sync": {"schema_hash": "h-new"}}), encoding="utf-8")

    result = versions.rollback_to_version(DB, "v0001")
    assert set(result["restored"]) == {"schema/schema.json", "schema/table_inventory.json", "schema/join_paths.json"}
    assert result["removed"] == ["schema/table_profiles.json"] and result["kept"] == []
    assert json.loads((root / "schema/join_paths.json").read_text())["pairs"] == ["old"] and not (root / "schema/table_profiles.json").exists()
    assert result["kept_current_as"] == "v0002" and "vector store" in result["note"]
    # the record is untrusted now, and db.yaml points at that version's schema
    assert drift.load_state(DB)["clean"] is False and "v0001" in drift.load_state(DB)["invalid_reason"]
    sync_meta = yaml.safe_load((root / "db.yaml").read_text())["schema_sync"]
    assert sync_meta["schema_hash"] == "h-old" and sync_meta["rolled_back_to"]["version"] == "v0001"


def test_the_current_files_are_kept_so_a_rollback_can_be_undone(root: Path) -> None:
    _generation(root, "old")
    _keep("old")
    _generation(root, "new")
    _keep("new")
    versions.rollback_to_version(DB, "v0001")
    assert json.loads((root / "schema/join_paths.json").read_text())["pairs"] == ["old"]
    versions.rollback_to_version(DB, "v0002")  # "new" was still kept
    assert json.loads((root / "schema/join_paths.json").read_text())["pairs"] == ["new"]


def test_a_valid_file_a_person_put_there_is_never_overwritten(root: Path) -> None:
    _generation(root, "old")
    _keep("old")
    _put(root, "schema/join_paths.json", '{"pairs": ["hand written"]}')  # valid JSON without our marker
    result = versions.rollback_to_version(DB, "v0001")
    assert result["kept"] == ["schema/join_paths.json"] and json.loads((root / "schema/join_paths.json").read_text())["pairs"] == ["hand written"]


def test_rollback_is_refused_for_a_damaged_version_a_running_sync_or_an_interrupted_one(root: Path) -> None:
    _generation(root, "one")
    _keep("one")
    before = (root / "schema/join_paths.json").read_bytes()
    guard = SyncGuard(DB)
    guard.acquire()
    with pytest.raises(versions.VersionError, match="already running"):
        versions.rollback_to_version(DB, "v0001")
    guard.release()
    _put(root, ".sync/in_progress.json", "{}")
    with pytest.raises(versions.VersionError, match="interrupted"):
        versions.rollback_to_version(DB, "v0001")
    (root / ".sync/in_progress.json").unlink()
    (root / ".sync/versions/v0001/files/schema/join_paths.json").write_bytes(b"{}")
    with pytest.raises(versions.VersionError, match="damaged"):
        versions.rollback_to_version(DB, "v0001")
    assert (root / "schema/join_paths.json").read_bytes() == before  # nothing was touched


def test_the_pinned_database_has_no_versions(root: Path) -> None:
    with pytest.raises(versions.VersionError):
        versions.rollback_to_version("erp_main", "v0001")
    assert versions.save_version("erp_main", schema_hash=None, structure=None, tables=None) is None


# ----------------------------------------------------------------------------------------------- stale chunks


def _store() -> AsyncMock:
    return AsyncMock()


def _prior(root: Path, names: list[str]) -> None:
    drift.save_state(DB, _state(tables={n: "x" for n in names}))


def _run(**kw: Any) -> dict[str, Any] | None:
    return asyncio.run(cleanup_stale_chunks(DB, kw.pop("store"), "schema:" + DB, kw.pop("fps"), schema_complete=kw.pop("complete", True)))


def test_stale_cleanup_is_off_without_the_flag(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch)
    assert _run(store=_store(), fps={"a": "x"}) is None


def test_shadow_mode_reports_the_chunks_it_would_remove_and_removes_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_sync_orchestration")
    _prior(root, ["a", "b", "Old_Table"])
    store = _store()
    report = _run(store=store, fps={"a": "x", "b": "x"})
    assert report["mode"] == "shadow" and report["would_remove"] == ["Old_Table"] and report["removed"] == []
    store.delete_chunks.assert_not_called()


def test_apply_mode_removes_exactly_the_chunks_of_dropped_tables(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_sync_orchestration", "auto_stale_chunk_cleanup")
    _prior(root, ["a", "b", "Old_Table"])
    store = _store()
    report = _run(store=store, fps={"a": "x", "b": "x"})
    assert report["removed"] == ["Old_Table"]
    store.delete_chunks.assert_awaited_once_with("schema:" + DB, [f"{DB}_schema_Old_Table"])


def test_nothing_is_removed_without_an_earlier_record_or_when_nothing_is_gone(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_sync_orchestration", "auto_stale_chunk_cleanup")
    store = _store()
    assert "no earlier sync" in _run(store=store, fps={"a": "x"})["note"]
    _prior(root, ["a"])
    assert _run(store=store, fps={"a": "x", "new": "y"})["removed"] == []
    store.delete_chunks.assert_not_called()


def test_a_cut_short_read_or_a_mass_disappearance_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_sync_orchestration", "auto_stale_chunk_cleanup")
    store = _store()
    _prior(root, ["a", "b"])
    assert "cut short" in _run(store=store, fps={"a": "x"}, complete=False)["refused"]
    _prior(root, [f"t{i}" for i in range(20)])
    report = _run(store=store, fps={"t0": "x", "t1": "x"})
    assert "failed read" in report["refused"] and report["removed"] == []
    store.delete_chunks.assert_not_called()


def test_a_store_that_cannot_delete_or_a_failing_delete_never_fails_the_sync(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_sync_orchestration", "auto_stale_chunk_cleanup")
    _prior(root, ["a", "gone"])

    class _NoDelete:
        pass

    assert "cannot delete" in _run(store=_NoDelete(), fps={"a": "x"})["refused"]
    store = _store()
    store.delete_chunks.side_effect = RuntimeError("qdrant down")
    assert "qdrant down" in _run(store=store, fps={"a": "x"})["error"]


# ----------------------------------------------------------------------------------------------- the orchestrator hook


def _sync_info(fps: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {"schema_hash": "h", "structure_hash": drift.structure_hash(fps), "table_fingerprints": fps, "schema_complete": True, "enabled": ["catalog"], "phase_versions": {}, **extra}


def test_a_committed_sync_leaves_a_record_a_version_and_its_drift(root: Path) -> None:
    _generation(root, "one")
    out = orchestrate.persist_generation({"catalog": {"ok": True}}, _sync_info({"a": "1"}), None, DB, committed=True)
    assert out["drift"]["first_sync"] is True and out["version"]["id"] == "v0001"
    state = drift.load_state(DB)
    assert state["clean"] is True and state["tables"] == {"a": "1"} and state["phases"]["catalog"] == "ok" and state["files"]["schema/join_paths.json"]

    out = orchestrate.persist_generation({"catalog": {"ok": True}}, _sync_info({"a": "1"}), None, DB, committed=True)
    assert out["drift"]["counts"] == {"added": 0, "removed": 0, "changed": 0, "unchanged": 1}
    assert out["version"] == {"id": "v0001", "unchanged": True}  # nothing moved: no new version

    _generation(root, "two")
    out = orchestrate.persist_generation({"catalog": {"ok": True}}, _sync_info({"a": "2", "b": "1"}), None, DB, committed=True)
    assert out["drift"]["counts"] == {"added": 1, "removed": 0, "changed": 1, "unchanged": 0}
    assert out["version"]["id"] == "v0002" and drift.load_state(DB)["tables"] == {"a": "2", "b": "1"}


def test_a_rolled_back_sync_does_not_replace_the_record(root: Path) -> None:
    _generation(root, "one")
    orchestrate.persist_generation({"catalog": {"ok": True}}, _sync_info({"a": "1"}), None, DB, committed=True)
    out = orchestrate.persist_generation({"catalog": {"ok": True}}, _sync_info({"a": "2"}), None, DB, committed=False)
    assert out["drift"]["counts"]["changed"] == 1 and "version" not in out
    assert drift.load_state(DB)["tables"] == {"a": "1"}


def test_a_failed_phase_or_a_cut_short_read_makes_the_record_untrusted_but_the_files_are_still_kept(root: Path) -> None:
    _generation(root, "one")
    out = orchestrate.persist_generation({"relationships": {"ok": False, "error": "x"}}, _sync_info({"a": "1"}), None, DB, committed=True)
    assert drift.load_state(DB)["clean"] is False  # so the next sync runs every phase
    assert out["version"]["id"] == "v0001"  # the files that are live can still be returned to
    out = orchestrate.persist_generation({}, _sync_info({"a": "1"}, schema_complete=False), None, DB, committed=True)
    assert out["drift"]["partial"] is True and drift.load_state(DB)["clean"] is False


def test_skipped_phases_keep_their_last_result_in_the_record(root: Path) -> None:
    _generation(root, "one")
    info = _sync_info({"a": "1"}, resync={"carried": {"relationships": "ok"}})
    orchestrate.persist_generation({"relationships": {"ok": True, "skipped": "unchanged since the last sync", "unchanged": True}}, info, None, DB, committed=True)
    assert drift.load_state(DB)["phases"]["relationships"] == "ok" and drift.load_state(DB)["clean"] is True


def test_without_fingerprints_nothing_happens(root: Path) -> None:
    assert orchestrate.persist_generation({}, {"schema_hash": "h"}, None, DB, committed=True) == {}
    assert drift.load_state(DB) is None


def test_the_report_carries_the_new_sections_and_an_unchanged_skip_is_not_an_unknown(root: Path) -> None:
    from src.sql.sync import report as report_mod

    result = {"status": "ok", "tables_synced": 2, "catalog": {"ok": True, "skipped": "unchanged since the last sync", "unchanged": True}}
    rep = report_mod.build_sync_report(
        result, db_id=DB, duration_s=1.0, validation=None, sync_info={"schema_hash": "h", "drift": {"counts": {"added": 1}}, "resync": {"mode": "shadow"}, "version": {"id": "v0001"}, "stale_chunks": {"mode": "shadow"}}
    )
    assert rep["drift"] and rep["resync"] and rep["version"] and rep["stale_chunks"]
    assert rep["phases"]["catalog"]["status"] == "skipped" and not any("catalog" in u for u in rep["unknowns"])
    plain = report_mod.build_sync_report(result, db_id=DB, duration_s=1.0, validation=None, sync_info={})
    assert not {"drift", "resync", "version", "stale_chunks"} & set(plain)


def test_the_db_yaml_summary_gets_the_drift_counts_and_version(root: Path) -> None:
    from src.sql.sync import report as report_mod

    (root / "db.yaml").write_text(yaml.safe_dump({"enabled": True}), encoding="utf-8")
    rep = {"status": "ok", "mode": "basic", "counts": {"tables": 2}, "phases": {}, "unknowns": [], "drift": {"counts": {"added": 1, "removed": 0, "changed": 0, "unchanged": 1}}, "version": {"id": "v0003"}}
    assert report_mod.record_sync_report(DB, rep, committed=True, schema_hash="h") is True
    last = yaml.safe_load((root / "db.yaml").read_text())["schema_sync"]["last_sync"]
    assert last["drift"]["added"] == 1 and last["version"] == "v0003"


def test_the_generated_file_list_is_unchanged_by_phase_13() -> None:
    assert len(GENERATED_FILES) == 10 and set(resync.TRACKED_FILES) == set(GENERATED_FILES) | {"semantics/column_glossary.json"}
    assert all(f in GENERATED_FILES for outs in resync.OUTPUTS.values() for f in outs)
