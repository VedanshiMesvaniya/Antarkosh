"""Any database an admin connects gets its own folder and owner; a user's questions are answered only from
their own current database; one admin can never touch or read another admin's database."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.api import auth
from src.api.db_resolution import resolve_target_db
from src.core import active_db, db_settings
from src.sql import registry
from src.sql.connect import choose_db_id, connection_fingerprint, derive_db_id
from src.sql.introspection.scaffold import activate_after_sync, ensure_db_folder

ROOT = Path(__file__).resolve().parents[2]
SUPER = {"X-User-Id": "admin"}
VED = {"X-User-Id": "vedanshi"}  # an administrator
PM = {"X-User-Id": "pm"}  # another administrator
RAHUL = {"X-User-Id": "rahul"}  # an ordinary user
PRIYA = {"X-User-Id": "priya"}  # another ordinary user

BODY = {"engine": "mssql", "host": "10.0.0.5", "port": 1433, "database": "QTS_AIS", "username": "u", "password": "pw"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    shutil.copytree(ROOT / "databases" / "_template", base / "_template")
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    monkeypatch.setenv("CONNECTIONS_FILE", str(tmp_path / "connections.json"))
    monkeypatch.delenv("DB_CONNECTIONS_FILE", raising=False)
    monkeypatch.setenv("ACTIVE_DB_FILE", str(tmp_path / "active.json"))
    monkeypatch.setenv("ALLOW_HEADER_AUTH", "1")
    monkeypatch.setenv("ADMIN_USERS", "vedanshi,pm")
    monkeypatch.delenv("STRICT_DB_OWNERSHIP", raising=False)

    class _Connector:
        def __init__(self, cfg):
            self.cfg = cfg

        async def test(self):
            return None

    monkeypatch.setattr("src.sql.connectors.get_connector", lambda engine, cfg=None: _Connector(cfg or {}))
    monkeypatch.setattr(db_settings, "_apply_runtime", MagicMock())
    return base


def _client():
    from src.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _meta(base: Path, db_id: str) -> dict[str, Any]:
    return yaml.safe_load((base / db_id / "db.yaml").read_text(encoding="utf-8"))


# ================================================================== who is an admin


def test_database_admins_come_from_the_environment_and_always_include_the_super_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "vedanshi, pm")
    assert auth.database_admins() == {"admin", "vedanshi", "pm"}
    monkeypatch.setenv("ADMIN_USERS", "")
    assert "admin" in auth.database_admins() and "vedanshi" not in auth.database_admins()


@pytest.mark.asyncio
async def test_admins_access_settings_and_ordinary_users_are_forbidden(env: Path):
    async with _client() as c:
        main_settings = await c.get("/api/settings/database", headers=VED)  # admin access
        as_super = await c.get("/api/settings/database", headers=SUPER)
        admin_dbs = await c.get("/api/admin/databases", headers=VED)
        ordinary_settings = await c.get("/api/settings/database", headers=RAHUL)
        ordinary_dbs = await c.get("/api/admin/databases", headers=RAHUL)
    assert main_settings.status_code == 200 and as_super.status_code == 200 and admin_dbs.status_code == 200
    assert ordinary_settings.status_code == 403 and ordinary_dbs.status_code == 403


# ================================================================== access rules


def test_database_access_rules(env: Path):
    ensure_db_folder("qts_ais", engine="mssql", owner="vedanshi")
    # All administrators have access
    assert registry.can_access_database("qts_ais", "vedanshi") is True
    assert registry.can_access_database("qts_ais", "pm") is True
    assert registry.can_access_database("qts_ais", "admin") is True
    # Regular users have access to newly scaffolded db (wildcard allowed_users)
    assert registry.can_access_database("qts_ais", "rahul") is True
    # Explicit restriction in db.yaml restricts regular users
    registry.update_database_access("qts_ais", ["rahul"])
    assert registry.can_access_database("qts_ais", "rahul") is True
    assert registry.can_access_database("qts_ais", "priya") is False


def test_admins_always_keep_access(env: Path, monkeypatch):
    ensure_db_folder("qts_ais", engine="mssql", owner="vedanshi")
    assert registry.can_access_database("qts_ais", "admin") is True
    assert registry.can_access_database("qts_ais", "vedanshi") is True
    assert registry.can_access_database("qts_ais", "pm") is True


def test_databases_without_an_owner_keep_the_original_rules(env: Path):
    (env / "old_db").mkdir()
    (env / "old_db" / "db.yaml").write_text("id: old_db\nengine: mysql\nallowed_users: [rahul]\n", encoding="utf-8")
    assert registry.can_access_database("old_db", "admin") is True
    assert registry.can_access_database("old_db", "vedanshi") is True
    assert registry.can_access_database("old_db", "rahul") is True
    assert registry.can_access_database("old_db", "priya") is False


def test_who_may_manage_a_database(env: Path, monkeypatch):
    ensure_db_folder("qts_ais", engine="mssql", owner="vedanshi")
    can = registry.can_manage_database
    # Any admin may manage any database
    assert can("qts_ais", "admin") and can("qts_ais", "vedanshi") and can("qts_ais", "pm")
    assert can("brand_new_db", "pm")
    assert can("erp_main", "vedanshi") and can("erp_main", "admin")
    # Non-admin or empty cannot manage
    assert not can("qts_ais", "rahul")
    assert not can("x", "") and not can("x", None)


# ================================================================== folder owner and who synced


def test_a_folder_records_creation_and_allows_chat_access(env: Path):
    ensure_db_folder("qts_ais", engine="mssql", owner="vedanshi")
    meta = _meta(env, "qts_ais")
    assert meta["created_at"] and meta["enabled"] is False
    assert meta["allowed_users"] == ["*"]


def test_a_sync_records_which_admin_ran_it(env: Path):
    ensure_db_folder("qts_ais", engine="mssql", owner="vedanshi")
    assert activate_after_sync("qts_ais", "vedanshi") is True
    sync = _meta(env, "qts_ais")["schema_sync"]
    assert sync["synced_by"] == "vedanshi" and sync["last_synced_at"] and _meta(env, "qts_ais")["enabled"] is True
    activate_after_sync("qts_ais", "admin")  # a later sync by another admin updates who ran it
    assert _meta(env, "qts_ais")["schema_sync"]["synced_by"] == "admin"


# ================================================================== choosing the database id


def test_id_comes_from_the_database_name():
    assert derive_db_id({"database": "QTS_AIS"}) == "qts_ais"
    assert derive_db_id({"service_name": "XEPDB1"}) == "xepdb1"
    assert derive_db_id({"database": "123"}) == "db_123" and derive_db_id({"database": "erp_main"}) == "erp_main_db"


def test_the_fingerprint_ignores_credentials_and_case():
    a = connection_fingerprint({**BODY, "username": "x", "password": "y"})
    b = connection_fingerprint({**BODY, "host": "10.0.0.5 ", "database": "qts_ais", "password": "different"})
    assert a == b and a != connection_fingerprint({**BODY, "database": "other"})


def test_reconnecting_same_database_updates_in_place_across_admins(env: Path):
    registry.save_connection("qts_ais", {**BODY})
    ensure_db_folder("qts_ais", engine="mssql", owner="vedanshi")
    assert choose_db_id("vedanshi", dict(BODY)) == ("qts_ais", True)
    assert choose_db_id("pm", dict(BODY)) == ("qts_ais", True)  # same server: updates in place


def test_a_connection_that_matches_erp_main_is_just_another_database(env: Path):
    registry.save_connection("erp_main", {**BODY})  # e.g. erp_main was pointed at this server earlier
    assert choose_db_id("vedanshi", dict(BODY)) == ("qts_ais", False)  # gets its own id and folder
    with pytest.raises(ValueError, match="main database"):
        choose_db_id("vedanshi", dict(BODY), requested="erp_main")  # erp_main is never overwritten from this form


def test_requested_db_id_validation(env: Path):
    ensure_db_folder("qts_ais", engine="mssql", owner="vedanshi")
    assert choose_db_id("pm", dict(BODY), requested="qts_ais") == ("qts_ais", True)
    with pytest.raises(PermissionError):
        choose_db_id("rahul", dict(BODY), requested="qts_ais")


# ================================================================== the connect endpoints


@pytest.mark.asyncio
async def test_connecting_any_database_creates_its_folder_owned_by_the_admin(env: Path):
    registry.save_connection("erp_main", {"engine": "mysql", "host": "main-host", "database": "erp", "username": "main", "password": "mainpw"})
    before = registry.get_connection("erp_main")
    async with _client() as c:
        res = await c.post("/api/databases/connect", json=BODY, headers=VED)
    assert res.status_code == 200
    assert res.json()["ok"] is True and res.json()["status"] == "saved" and res.json()["db_id"] == "qts_ais"
    assert res.json()["folder_created"] is True
    assert registry.get_connection("qts_ais")["host"] == "10.0.0.5"
    assert registry.get_connection("erp_main") == before and not (env / "erp_main").exists()  # the main database is untouched


@pytest.mark.asyncio
async def test_reconnecting_updates_in_place_across_admins(env: Path):
    async with _client() as c:
        first = (await c.post("/api/databases/connect", json=BODY, headers=VED)).json()
        again = (await c.post("/api/databases/connect", json={**BODY, "password": ""}, headers=VED)).json()
        theirs = (await c.post("/api/databases/connect", json=BODY, headers=PM)).json()
    assert first["db_id"] == again["db_id"] == theirs["db_id"] == "qts_ais"
    assert again["existing"] is True and theirs["existing"] is True
    assert registry.get_connection("qts_ais")["password"] == "pw"  # a blank password keeps the stored one


@pytest.mark.asyncio
async def test_admins_manage_databases_and_regular_users_are_forbidden(env: Path):
    async with _client() as c:
        await c.post("/api/databases/connect", json=BODY, headers=VED)
        read = await c.get("/api/databases/qts_ais/connection", headers=PM)
        save = await c.post("/api/databases/qts_ais/connection", json=BODY, headers=PM)
        test = await c.post("/api/databases/qts_ais/test", json=BODY, headers=PM)
        listing = (await c.get("/api/admin/databases", headers=PM)).json()
        mine = (await c.get("/api/admin/databases", headers=VED)).json()
        everything = (await c.get("/api/admin/databases", headers=SUPER)).json()
        assert [r.status_code for r in (read, save, test)] == [200, 200, 200]
        assert "qts_ais" in [d["id"] for d in listing] and "qts_ais" in [d["id"] for d in mine]
        assert "qts_ais" in [d["id"] for d in everything]

        # Regular users are rejected with 403 on all admin endpoints
        r_read = await c.get("/api/databases/qts_ais/connection", headers=RAHUL)
        r_save = await c.post("/api/databases/qts_ais/connection", json=BODY, headers=RAHUL)
        r_test = await c.post("/api/databases/qts_ais/test", json=BODY, headers=RAHUL)
        r_list = await c.get("/api/admin/databases", headers=RAHUL)
        r_conn = await c.post("/api/databases/connect", json=BODY, headers=RAHUL)
        r_del = await c.delete("/api/databases/qts_ais/connection", headers=RAHUL)
        assert [r.status_code for r in (r_read, r_save, r_test, r_list, r_conn, r_del)] == [403] * 6


@pytest.mark.asyncio
async def test_the_connect_test_saves_nothing_and_sqlite_is_refused(env: Path):
    async with _client() as c:
        ok = await c.post("/api/databases/connect/test", json=BODY, headers=VED)
        lite = await c.post("/api/databases/connect", json={"engine": "sqlite"}, headers=VED)
        bad_schema = await c.post("/api/databases/connect", json={**BODY, "schema_name": "x"}, headers=VED)
    assert ok.status_code == 200 and not registry.get_connection("qts_ais") and not (env / "qts_ais").exists()
    assert lite.status_code == 422 and bad_schema.status_code == 422


@pytest.mark.asyncio
async def test_a_failed_connection_test_creates_no_folder(env: Path, monkeypatch):
    class _Bad:
        def __init__(self, cfg):
            pass

        async def test(self):
            raise RuntimeError("Login failed")

    monkeypatch.setattr("src.sql.connectors.get_connector", lambda engine, cfg=None: _Bad(cfg))
    async with _client() as c:
        res = await c.post("/api/databases/connect", json=BODY, headers=VED)
    assert res.status_code == 422 and not (env / "qts_ais").exists() and not registry.get_connection("qts_ais")


@pytest.mark.asyncio
async def test_the_form_gets_its_engine_list_without_any_saved_details(env: Path):
    async with _client() as c:
        res = await c.get("/api/admin/database-engines", headers=VED)
        denied = await c.get("/api/admin/database-engines", headers=RAHUL)
    keys = {e["key"] for e in res.json()["engines"]}
    assert res.status_code == 200 and {"mysql", "postgresql", "mssql", "oracle"} <= keys and "host" not in res.json()
    assert denied.status_code == 403


# ================================================================== sync: who ran it, and the admin's database


@pytest.fixture()
def fake_sync(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def _sync(db_id="erp_main", synced_by=None, **kw):
        calls.append({"db_id": db_id, "synced_by": synced_by})
        return {"status": "ok", "db_id": db_id, "tables_synced": 3}

    monkeypatch.setattr("src.sql.schema_retrieval.sync_live_schema", _sync)
    return calls


@pytest.mark.asyncio
async def test_syncing_your_own_database_records_you_and_makes_it_your_current_one(env: Path, fake_sync):
    async with _client() as c:
        await c.post("/api/databases/connect", json=BODY, headers=VED)
        res = await c.post("/api/settings/sync-schema", params={"db_id": "qts_ais"}, headers=VED)
    assert res.status_code == 200 and fake_sync == [{"db_id": "qts_ais", "synced_by": "vedanshi"}]
    assert active_db.get_active_db("vedanshi") == "qts_ais" and active_db.get_active_db("pm") is None


@pytest.mark.asyncio
async def test_admins_can_sync_and_regular_user_cannot(env: Path, fake_sync):
    async with _client() as c:
        await c.post("/api/databases/connect", json=BODY, headers=VED)
        other = await c.post("/api/settings/sync-schema", params={"db_id": "qts_ais"}, headers=PM)
        main = await c.post("/api/settings/sync-schema", headers=VED)
        super_main = await c.post("/api/settings/sync-schema", headers=SUPER)
        ordinary = await c.post("/api/settings/sync-schema", params={"db_id": "qts_ais"}, headers=RAHUL)
    assert (other.status_code, main.status_code, super_main.status_code) == (200, 200, 200)
    assert ordinary.status_code == 403


# ================================================================== each user's current database


def test_choices_are_per_user_and_survive_a_restart(env: Path):
    active_db.set_active_db("vedanshi", "qts_ais")
    active_db.set_active_db("pm", "other_db")
    assert (active_db.get_active_db("vedanshi"), active_db.get_active_db("pm"), active_db.get_active_db("rahul")) == ("qts_ais", "other_db", None)
    active_db.set_active_db("vedanshi", None)
    assert active_db.get_active_db("vedanshi") is None and active_db.get_active_db("pm") == "other_db"
    assert active_db.clear_database_for_all("other_db") == 1 and active_db.get_active_db("pm") is None


def test_many_users_choosing_at_once_never_lose_each_others_choice(env: Path):
    users = [f"user{i}" for i in range(12)]
    threads = [threading.Thread(target=active_db.set_active_db, args=(u, f"db_{i}")) for i, u in enumerate(users)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert {u: active_db.get_active_db(u) for u in users} == {u: f"db_{i}" for i, u in enumerate(users)}


def _owned_active_db(env: Path, db_id: str, owner: str) -> None:
    ensure_db_folder(db_id, engine="mssql", owner=owner)
    activate_after_sync(db_id, owner)


def test_a_request_uses_its_named_database_then_the_users_own_then_the_default(env: Path):
    _owned_active_db(env, "qts_ais", "vedanshi")
    _owned_active_db(env, "pm_db", "pm")
    active_db.set_active_db("vedanshi", "qts_ais")
    assert resolve_target_db(None, "rahul") == "erp_main"  # nothing chosen: unchanged behaviour
    assert resolve_target_db(None, "vedanshi") == "qts_ais"  # their own current database
    assert resolve_target_db("pm_db", "pm") == "pm_db"  # a named database wins over the current one
    active_db.set_active_db("pm", "pm_db")
    assert resolve_target_db(None, "pm") == "pm_db" and resolve_target_db(None, "vedanshi") == "qts_ais"


def test_a_database_that_is_no_longer_available_is_reported_not_swapped_for_another(env: Path):
    from fastapi import HTTPException

    _owned_active_db(env, "qts_ais", "vedanshi")
    registry.update_database_access("qts_ais", ["rahul"])
    active_db.set_active_db("priya", "qts_ais")  # priya was not given access
    with pytest.raises(HTTPException) as denied:
        resolve_target_db(None, "priya")
    assert denied.value.status_code == 409 and "qts_ais" in denied.value.detail
    with pytest.raises(HTTPException) as explicit:
        resolve_target_db("qts_ais", "priya")
    assert explicit.value.status_code == 403 and explicit.value.detail == "Access denied to this database"

    meta = _meta(env, "qts_ais")
    meta["enabled"] = False
    (env / "qts_ais" / "db.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    active_db.set_active_db("rahul", "qts_ais")
    with pytest.raises(HTTPException) as inactive:
        resolve_target_db(None, "rahul")
    assert inactive.value.status_code == 409 and "not active" in inactive.value.detail


class _Pipe:
    built: ClassVar[list[str]] = []

    def __init__(self, *a: Any, **kw: Any):
        _Pipe.built.append(kw.get("db_id"))

    async def query(self, question, filters=None, db_id=None, **_):
        from src.models.schemas import QueryResult

        await asyncio.sleep(0)
        return QueryResult(query=question, answer=f"from {db_id}", model_used="t")


@pytest.mark.asyncio
async def test_two_users_asking_at_the_same_time_are_each_answered_from_their_own_database(env: Path, monkeypatch):
    _owned_active_db(env, "qts_ais", "vedanshi")
    _owned_active_db(env, "pm_db", "pm")
    active_db.set_active_db("vedanshi", "qts_ais")
    active_db.set_active_db("pm", "pm_db")
    _Pipe.built = []
    monkeypatch.setattr("src.api.query.QueryPipeline", _Pipe)
    async with _client() as c:
        a, b, d = await asyncio.gather(
            c.post("/api/query", json={"question": "hi"}, headers=VED),
            c.post("/api/query", json={"question": "hi"}, headers=PM),
            c.post("/api/query", json={"question": "hi"}, headers=RAHUL),
        )
    assert [r.json()["answer"] for r in (a, b, d)] == ["from qts_ais", "from pm_db", "from erp_main"]


@pytest.mark.asyncio
async def test_my_database_endpoints_only_offer_what_i_may_use(env: Path):
    _owned_active_db(env, "qts_ais", "vedanshi")
    ensure_db_folder("pending_db", engine="mssql", owner="vedanshi")  # connected but not synced yet
    registry.update_database_access("qts_ais", ["rahul"])
    async with _client() as c:
        mine = (await c.get("/api/me/database", headers=RAHUL)).json()
        chosen = await c.put("/api/me/database", json={"db_id": "qts_ais"}, headers=RAHUL)
        denied = await c.put("/api/me/database", json={"db_id": "qts_ais"}, headers=PRIYA)
        inactive = await c.put("/api/me/database", json={"db_id": "pending_db"}, headers=VED)
        junk = await c.put("/api/me/database", json={"db_id": "Bad Name"}, headers=RAHUL)
        after = (await c.get("/api/me/database", headers=RAHUL)).json()
        cleared = (await c.put("/api/me/database", json={"db_id": None}, headers=RAHUL)).json()
        priya_view = (await c.get("/api/me/database", headers=PRIYA)).json()
    assert [o["id"] for o in mine["options"]] == ["qts_ais"] and mine["effective"] == "erp_main"
    assert chosen.status_code == 200 and after["active"] == "qts_ais" and after["effective"] == "qts_ais"
    assert (denied.status_code, inactive.status_code, junk.status_code) == (403, 409, 400)
    assert cleared == {"active": None, "effective": "erp_main"} and priya_view["options"] == []


@pytest.mark.asyncio
async def test_admin_can_delete_saved_connection_and_regular_user_is_forbidden(env: Path):
    async with _client() as c:
        await c.post("/api/databases/connect", json=BODY, headers=VED)
        assert registry.get_connection("qts_ais")["host"] == "10.0.0.5"

        # Regular user cannot delete
        denied = await c.delete("/api/databases/qts_ais/connection", headers=RAHUL)
        assert denied.status_code == 403

        # Admin can delete
        deleted = await c.delete("/api/databases/qts_ais/connection", headers=PM)
        assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
        assert not registry.get_connection("qts_ais")

        # Cannot delete default database
        main_del = await c.delete("/api/databases/erp_main/connection", headers=SUPER)
        assert main_del.status_code == 400


@pytest.mark.asyncio
async def test_regular_user_cannot_call_ingestion_endpoints(env: Path):
    async with _client() as c:
        res_folder = await c.post("/api/ingest/folder", headers=RAHUL)
        assert res_folder.status_code == 403



# ================================================================== flag defaults


@pytest.fixture()
def flag_file(tmp_path: Path, monkeypatch):
    from src.utils import feature_flags as ff

    monkeypatch.setattr(ff, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ff, "_DB_FLAGS_CACHE", None)
    monkeypatch.setattr(ff, "_DB_CACHE_MTIME", 0.0)
    return tmp_path / "feature_flags.yaml"


def test_db_defaults_apply_to_databases_without_their_own_entry(flag_file: Path):
    from src.utils.feature_flags import is_db_feature_enabled as on

    flag_file.write_text(
        "db_defaults:\n  auto_catalog: true\n  auto_sqlalchemy: \"true\"\n"
        "db_features:\n  special_db:\n    auto_catalog: false\n    auto_scaffold: true\n",
        encoding="utf-8",
    )
    assert on("auto_catalog", "any_new_db") is True
    assert on("auto_catalog", "special_db") is False  # its own entry wins, even over a default of true
    assert on("auto_scaffold", "special_db") is True and on("auto_scaffold", "any_new_db") is False
    assert on("auto_sqlalchemy", "any_new_db") is False  # only a literal true counts
    assert on("auto_catalog", "erp_main") is False  # the default database is never switched on


def test_without_db_defaults_everything_is_off(flag_file: Path):
    from src.utils.feature_flags import is_db_feature_enabled as on

    flag_file.write_text("db_features:\n  x_db:\n    auto_catalog: true\n", encoding="utf-8")
    assert on("auto_catalog", "x_db") is True and on("auto_catalog", "y_db") is False


def test_the_admin_list_and_strict_mode_can_also_come_from_the_env_file_settings(monkeypatch):
    from src.core.config import settings

    monkeypatch.delenv("ADMIN_USERS", raising=False)
    monkeypatch.delenv("STRICT_DB_OWNERSHIP", raising=False)
    monkeypatch.setattr(settings, "admin_users", "vedanshi,pm", raising=False)
    monkeypatch.setattr(settings, "strict_db_ownership", True, raising=False)
    assert auth.database_admins() == {"admin", "vedanshi", "pm"}
    assert registry._strict_ownership() is True
    monkeypatch.setenv("STRICT_DB_OWNERSHIP", "0")  # the process environment wins over .env
    assert registry._strict_ownership() is False


# ================================================================== blank fields are never borrowed from the app's own database


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["host", "database", "username"])
async def test_a_half_filled_connection_is_rejected_before_anything_is_saved(env: Path, missing: str):
    # env's connector always "succeeds", so a 422 here proves the form is checked before any connection is tried.
    body = {**BODY, missing: ""}
    async with _client() as c:
        for url in ("/api/databases/connect/test", "/api/databases/connect"):
            res = await c.post(url, json=body, headers=VED)
            assert res.status_code == 422 and "required" in res.json()["detail"], url
    assert registry.get_connection("qts_ais") == {} and not (env / "qts_ais").exists()


@pytest.mark.asyncio
async def test_connecting_with_empty_password_is_allowed(env: Path):
    body = {**BODY, "password": ""}
    async with _client() as c:
        for url in ("/api/databases/connect/test", "/api/databases/connect"):
            res = await c.post(url, json=body, headers=VED)
            assert res.status_code == 200, url
    assert registry.get_connection("qts_ais")["password"] == ""


@pytest.mark.asyncio
async def test_the_add_form_test_reaches_the_add_handler_not_the_per_database_one(env: Path):
    async with _client() as c:
        res = await c.post("/api/databases/connect/test", json=BODY, headers=VED)
    assert res.status_code == 200 and res.json() == {"ok": True, "db_id": "qts_ais", "existing": False}  # not db_id "connect"


@pytest.mark.asyncio
async def test_editing_a_saved_database_may_leave_the_password_blank(env: Path):
    async with _client() as c:
        await c.post("/api/databases/connect", json=BODY, headers=VED)
        res = await c.post("/api/databases/connect", json={**BODY, "password": "", "db_id": "qts_ais"}, headers=VED)
    assert res.status_code == 200 and registry.get_connection("qts_ais")["password"] == "pw"


@pytest.mark.asyncio
async def test_switching_saved_postgres_with_schema_to_mssql_does_not_fail_on_schema(env: Path):
    pg_body = {
        "engine": "postgresql",
        "host": "10.0.0.1",
        "port": 5432,
        "database": "my_pg",
        "username": "u",
        "password": "pw",
        "schema_name": "erp_food",
    }
    async with _client() as c:
        # First save as PostgreSQL with schema
        res = await c.post("/api/databases/connect", json=pg_body, headers=VED)
        assert res.status_code == 200
        assert registry.get_connection("my_pg")["schema_name"] == "erp_food"

        # Now test and switch to MSSQL without specifying schema (omitted or blank)
        mssql_body = {
            "engine": "mssql",
            "host": "10.0.0.2",
            "port": 1433,
            "database": "my_pg",
            "username": "u",
            "password": "pw",
            "schema_name": "",
        }
        test_res = await c.post("/api/databases/my_pg/connection/test", json=mssql_body, headers=VED)
        assert test_res.status_code == 200, test_res.text

        save_res = await c.post("/api/databases/my_pg/connection", json=mssql_body, headers=VED)
        assert save_res.status_code == 200, save_res.text

        # Verify the saved connection is mssql and has dropped the schema
        saved = registry.get_connection("my_pg")
        assert saved["engine"] == "mssql" and saved.get("schema_name") == ""


@pytest.mark.asyncio
async def test_mssql_connect_test_with_blank_schema_succeeds(env: Path):
    async with _client() as c:
        res = await c.post("/api/databases/connect/test", json={**BODY, "schema_name": ""}, headers=VED)
    assert res.status_code == 200 and res.json()["ok"] is True

