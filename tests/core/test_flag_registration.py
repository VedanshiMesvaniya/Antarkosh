"""Automatic per-database feature flags: new databases, backfill of flags added later, odd file layouts, isolation."""

# ruff: noqa: F811

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import yaml

from src.utils import feature_flags as ff
from tests.sql.test_auto_procedures import _sync_args, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_catalog_extraction import ROOT, dbs  # noqa: F401  (shared fixture)

ALL = ff.DB_FEATURE_FLAGS
OLD_FLAGS = tuple(f for f in ALL if f != "auto_join_paths")  # a database registered before auto_join_paths existed


def local() -> Path:
    return ff.CONFIG_DIR / "feature_flags.yaml"  # the per-test config folder from conftest


def entries() -> dict:
    return yaml.safe_load(local().read_text(encoding="utf-8")).get("db_features") or {}


def write_entry(db_id: str, flags: dict[str, bool], extra: str = "") -> None:
    body = "".join(f"    {k}: {'true' if v else 'false'}\n" for k, v in flags.items())
    local().write_text(f"{extra}db_features:\n  {db_id}:\n{body}", encoding="utf-8")


# ================================================================= the template and the rules


def test_the_new_database_template_is_derived_from_the_flag_list_so_a_new_flag_is_never_forgotten():
    assert tuple(ff.DEFAULT_NEW_DB_FLAGS) == ALL and all(ff.DEFAULT_NEW_DB_FLAGS.values()) and len(ALL) == 13


def test_a_new_database_gets_every_flag_on_and_nothing_else_in_the_file_changes():
    local().write_text("# mine\nfeatures:\n  a: true\n\ndb_features:\n  other_db:\n    auto_catalog: false  # on purpose\n", encoding="utf-8")
    assert ff.register_db_feature_flags("brand_new_db") is True
    text = local().read_text(encoding="utf-8")
    assert "# mine" in text and "# on purpose" in text and entries()["other_db"] == {"auto_catalog": False}
    assert entries()["brand_new_db"] == dict.fromkeys(ALL, True)
    assert all(ff.is_db_feature_enabled(f, "brand_new_db") for f in ALL)


def test_pinned_blank_and_template_ids_are_never_registered():
    for bad in ("erp_main", "", "   ", None, "_example_db", "*"):
        assert ff.register_db_feature_flags(bad) is False
    assert not local().exists() or "erp_main" not in entries()


def test_the_local_file_is_created_from_the_tracked_example_and_carries_no_real_database_names():
    assert not local().exists()
    assert ff.register_db_feature_flags("first_db") is True
    names = set(entries())
    assert names == {"_example_db", "first_db"}
    example = yaml.safe_load((ROOT / "config" / "feature_flags.example.yaml").read_text(encoding="utf-8"))
    assert set(example["db_features"]) == {"_example_db"}  # nothing of the owner's is committed


# ================================================================= the user's failure: a flag added after registration


def test_a_flag_added_later_is_backfilled_as_true_and_the_owners_values_are_kept():
    write_entry("qts_ais", {**dict.fromkeys(OLD_FLAGS, True), "auto_column_meaning": False})
    assert ff.is_db_feature_enabled("auto_join_paths", "qts_ais") is False or ff.is_db_feature_enabled("auto_join_paths", "qts_ais") is True
    before = entries()["qts_ais"]
    assert "auto_join_paths" not in before
    assert ff.register_db_feature_flags("qts_ais") is True
    after = entries()["qts_ais"]
    assert after["auto_join_paths"] is True and after["auto_column_meaning"] is False  # added true; the owner's false stays false
    assert {k: v for k, v in after.items() if k != "auto_join_paths"} == before
    assert ff.register_db_feature_flags("qts_ais") is False  # nothing left to add; no second write


def test_a_flag_that_does_not_exist_yet_is_added_to_every_registered_database_when_it_appears(monkeypatch):
    monkeypatch.setattr(ff, "DB_FEATURE_FLAGS", (*ALL, "auto_future_phase"))
    monkeypatch.setattr(ff, "DEFAULT_NEW_DB_FLAGS", dict.fromkeys((*ALL, "auto_future_phase"), True))
    write_entry("qts_ais", dict.fromkeys(ALL, True))
    assert ff.register_db_feature_flags("qts_ais") is True and entries()["qts_ais"]["auto_future_phase"] is True


def test_the_complete_case_is_cheap_nothing_is_read_or_written(monkeypatch):
    write_entry("qts_ais", dict.fromkeys(ALL, True))
    ff._load_db_flags_from_yaml()  # warm the cache the way the application does
    before = local().stat().st_mtime_ns

    def boom(*a, **k):
        raise AssertionError("the file must not be opened on the fast path")

    with monkeypatch.context() as scoped:  # only this patch is undone, not the isolated config folder
        scoped.setattr(Path, "read_text", boom)
        assert ff.register_db_feature_flags("qts_ais") is False
    assert local().stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_a_sync_creates_join_paths_for_a_database_whose_entry_predates_the_flag(sqlite_db: Path, dbs: Path):
    """The reported failure: every other flag on, auto_join_paths missing from the entry, so join_paths.json never appeared."""
    from src.sql.schema_retrieval import sync_live_schema

    write_entry("pg_live", dict.fromkeys(OLD_FLAGS, True))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["join_paths"]["ok"] is True
    assert (dbs / "pg_live" / "schema" / "join_paths.json").exists()
    assert entries()["pg_live"]["auto_join_paths"] is True


# ================================================================= layouts that used to fail silently


LAYOUTS = {
    "a comment that mentions db_features first": "# per-database flags live under db_features: below\nfeatures:\n  a: true\n\ndb_features:\n  x:\n    auto_catalog: true\n",
    "db_features is an empty inline map": "features:\n  a: true\ndb_features: {}\n",
    "db_features is flow style": "db_features: {x: {auto_catalog: true}}\n",
    "db_features is null": "features:\n  a: true\ndb_features:\n",
    "no db_features at all": "features:\n  a: true\n",
    "four-space indentation": "db_features:\n    x:\n        auto_catalog: true\n",
    "db_features is not the last section": "db_features:\n  x:\n    auto_catalog: true\ndb_defaults:\n  auto_catalog: true\n",
    "an empty file": "",
    "entries with a trailing comment on the header": "db_features:   # per database\n  x:   # first\n    auto_catalog: true\n",
}


@pytest.mark.parametrize("label", sorted(LAYOUTS))
def test_registering_works_for_every_layout_and_changes_nothing_else(label):
    local().write_text(LAYOUTS[label], encoding="utf-8")
    old = yaml.safe_load(LAYOUTS[label]) or {}
    assert ff.register_db_feature_flags("new_one") is True, label
    new = yaml.safe_load(local().read_text(encoding="utf-8"))
    assert new["db_features"]["new_one"] == dict.fromkeys(ALL, True), label
    expected = dict(old)
    expected["db_features"] = {**(old.get("db_features") or {}), "new_one": dict.fromkeys(ALL, True)}
    if "x" in expected["db_features"]:  # an existing sibling entry is kept exactly (registering another id does not complete it)
        assert new["db_features"]["x"] == old["db_features"]["x"], label
    assert {k: v for k, v in new.items() if k != "db_features"} == {k: v for k, v in old.items() if k != "db_features"}, label
    assert ff.is_db_feature_enabled("auto_join_paths", "new_one") is True


def test_text_edits_keep_comments_but_a_structured_rewrite_keeps_a_backup_copy():
    local().write_text(LAYOUTS["a comment that mentions db_features first"], encoding="utf-8")
    ff.register_db_feature_flags("one")
    assert "# per-database flags live under db_features: below" in local().read_text(encoding="utf-8")
    assert not local().with_name("feature_flags.yaml.bak").exists()  # a text edit needs no backup
    local().write_text("db_features: {x: {auto_catalog: true}}\n", encoding="utf-8")
    ff.register_db_feature_flags("two")
    bak = local().with_name("feature_flags.yaml.bak")
    assert bak.read_text(encoding="utf-8") == "db_features: {x: {auto_catalog: true}}\n"
    assert set(entries()) == {"x", "two"}


@pytest.mark.parametrize("odd", ["yes", "no", "null", "on", "2024"])
def test_ids_that_yaml_would_read_as_a_boolean_or_a_number_are_written_quoted_and_match(odd):
    write_entry("existing_db", {"auto_catalog": True})
    assert ff.register_db_feature_flags(odd) is True
    assert odd in entries() and isinstance(next(k for k in entries() if str(k) == odd), str)
    assert ff.is_db_feature_enabled("auto_catalog", odd) is True


def test_an_unreadable_file_is_left_alone_and_never_raises():
    local().write_text("db_features: [unterminated\n  - : :", encoding="utf-8")
    before = local().read_text(encoding="utf-8")
    assert ff.register_db_feature_flags("whatever") is False and local().read_text(encoding="utf-8") == before


def test_an_entry_that_is_not_a_mapping_is_left_alone():
    local().write_text("db_features:\n  odd_db: true\n", encoding="utf-8")
    assert ff.register_db_feature_flags("odd_db") is False and entries() == {"odd_db": True}


def test_concurrent_registrations_all_land_and_the_file_stays_valid():
    local().write_text("db_features:\n  keep_me:\n    auto_catalog: false\n", encoding="utf-8")
    ids = [f"db_{i}" for i in range(12)]
    threads = [threading.Thread(target=ff.register_db_feature_flags, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = entries()
    assert set(ids) <= set(got) and got["keep_me"] == {"auto_catalog": False} and all(got[i] == dict.fromkeys(ALL, True) for i in ids)


# ================================================================= the connection flow


def test_a_second_connection_to_a_same_named_database_gets_a_numbered_id_not_an_error(tmp_path: Path):
    from src.sql import connect

    conn = tmp_path / "connections.json"
    conn.write_text(json.dumps({"qts_ais": {"engine": "mssql", "host": "h1", "port": 1433, "database": "QTS_AIS", "username": "u"}}), encoding="utf-8")
    (tmp_path / "databases").mkdir()
    cfg = {"engine": "mssql", "host": "OTHER", "port": 1433, "database": "QTS_AIS", "username": "u2"}
    first, existing = connect.choose_db_id("admin", cfg, None, databases_dir=tmp_path / "databases", conn_path=conn)
    assert (first, existing) == ("qts_ais_2", False)
    conn.write_text(json.dumps({**json.loads(conn.read_text()), "qts_ais_2": {**cfg}}), encoding="utf-8")
    third, _ = connect.choose_db_id("admin", {**cfg, "host": "THIRD"}, None, databases_dir=tmp_path / "databases", conn_path=conn)
    assert third == "qts_ais_3" and ff.is_db_feature_enabled("auto_catalog", "qts_ais_3") is True


def test_saving_a_connection_registers_its_flags_and_loading_connections_completes_older_entries(tmp_path: Path):
    from src.sql import registry

    conn = tmp_path / "connections.json"
    registry.save_connection("fresh_db", {"engine": "sqlite", "database": "x.db"}, conn_path=conn)
    assert entries()["fresh_db"] == dict.fromkeys(ALL, True)
    write_entry("old_db", dict.fromkeys(OLD_FLAGS, True))
    conn.write_text(json.dumps({"old_db": {"engine": "sqlite", "database": "o.db"}}), encoding="utf-8")
    registry.get_connection("old_db", conn_path=conn)
    assert entries()["old_db"]["auto_join_paths"] is True


def test_a_failure_while_deriving_a_name_cannot_break_loading_connections(tmp_path: Path, monkeypatch):
    from src.sql import registry

    conn = tmp_path / "connections.json"
    conn.write_text(json.dumps({"some_db": {"engine": "sqlite", "database": "weird"}}), encoding="utf-8")

    def boom(cfg):
        raise ValueError("cannot derive")

    monkeypatch.setattr("src.sql.connect.derive_db_id", boom)
    assert "some_db" in registry._load_connections(conn_path=conn)


# ================================================================= isolation and the diagnostic command


def test_tests_never_touch_the_real_config_file():
    real = ROOT / "config" / "feature_flags.yaml"
    before = real.read_bytes() if real.exists() else None
    ff.register_db_feature_flags("must_stay_in_the_temp_folder")
    assert ff.CONFIG_DIR != ROOT / "config" and local().exists()
    assert (real.read_bytes() if real.exists() else None) == before


def test_the_diagnostic_command_reports_every_flag_for_the_database_after_completing_it(tmp_path: Path, capsys, monkeypatch):
    import importlib.util

    from src.sql import registry

    spec = importlib.util.spec_from_file_location("sync_feature_flags", ROOT / "scripts" / "db" / "sync_feature_flags.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = tmp_path / "connections.json"
    conn.write_text(json.dumps({"qts_ais": {"engine": "sqlite", "database": "q.db"}}), encoding="utf-8")
    monkeypatch.setattr(registry, "connections_path", lambda: conn)
    write_entry("qts_ais", dict.fromkeys(OLD_FLAGS, True))
    assert mod.main(["--db", "qts_ais"]) == 0
    out = capsys.readouterr().out
    assert entries()["qts_ais"]["auto_join_paths"] is True and "auto_join_paths" in out and "OFF" not in out


# ================================================================= through the real Connect Database endpoints


from tests.sql.test_add_database_flow import (  # noqa: F401  (shared fixtures)
    ADMIN,
    BODY,
    _client,
    env,
)


@pytest.mark.asyncio
async def test_connecting_a_new_database_gives_it_every_flag_without_naming_an_id(env: Path):
    async with _client() as c:
        res = await c.post("/api/databases/connect", json={**BODY, "database": "QTS_AIS", "engine": "mssql"}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["db_id"] == "qts_ais"
    assert entries()["qts_ais"] == dict.fromkeys(ALL, True)
    assert all(ff.is_db_feature_enabled(f, "qts_ais") for f in ALL)


@pytest.mark.asyncio
async def test_connecting_a_second_database_with_the_same_name_gets_its_own_id_and_its_own_flags(env: Path):
    async with _client() as c:
        first = await c.post("/api/databases/connect", json={**BODY, "database": "QTS_AIS"}, headers=ADMIN)
        second = await c.post("/api/databases/connect", json={**BODY, "database": "QTS_AIS", "host": "another-server"}, headers=ADMIN)
    assert (first.status_code, second.status_code) == (200, 200)
    assert (first.json()["db_id"], second.json()["db_id"]) == ("qts_ais", "qts_ais_2")
    assert {"qts_ais", "qts_ais_2"} <= set(entries()) and entries()["qts_ais_2"] == dict.fromkeys(ALL, True)


@pytest.mark.asyncio
async def test_saving_a_connection_under_a_chosen_id_registers_it_and_keeps_the_owners_edits(env: Path):
    async with _client() as c:
        await c.post("/api/databases/my_tenant/connection", json=BODY, headers=ADMIN)
        text = local().read_text(encoding="utf-8")
        head, tail = text.split("  my_tenant:", 1)  # the owner turns one flag off, in this database's own block
        local().write_text(head + "  my_tenant:" + tail.replace("auto_learning: true", "auto_learning: false", 1), encoding="utf-8")
        await c.post("/api/databases/my_tenant/connection", json={**BODY, "host": "moved"}, headers=ADMIN)  # saved again
    assert entries()["my_tenant"]["auto_learning"] is False and entries()["my_tenant"]["auto_join_paths"] is True
