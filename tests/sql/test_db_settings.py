"""Admin database-connection settings: validation, config-file persistence, live apply."""

from __future__ import annotations

import json
import os
import stat
import sys
import types

import pytest

import src.core.db_settings as dbs
from src.core.config import settings


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    path = tmp_path / "db_connection.json"
    monkeypatch.setenv("DB_CONFIG_FILE", str(path))
    for name, val in [("db_engine", "sqlite"), ("db_host", ""), ("db_port", 3306),
                      ("db_name", ""), ("db_readonly_user", ""), ("db_readonly_password", "")]:
        monkeypatch.setattr(settings, name, val)

    calls: list[str] = []

    class _Retriever:
        clear_schema_cache = classmethod(lambda c: calls.append("schema"))
        clear_result_cache = classmethod(lambda c: calls.append("result"))

    class _Cache:
        reset = classmethod(lambda c: calls.append("semantic"))

    monkeypatch.setitem(sys.modules, "src.stages.s12b_sql_retrieval",
                        types.SimpleNamespace(SQLRetriever=_Retriever))
    monkeypatch.setitem(sys.modules, "src.utils.semantic_cache",
                        types.SimpleNamespace(SemanticCache=_Cache))

    async def _ok(cfg):  # no real server in unit tests
        return None

    monkeypatch.setattr(dbs, "_test_connection", _ok)
    return types.SimpleNamespace(path=path, calls=calls)


MYSQL = {"engine": "mysql", "host": "localhost", "port": 3306, "database": "gm",
         "username": "root", "password": "secret"}
PG = {"engine": "postgresql", "host": "pg.local", "port": 5432, "database": "gm",
      "username": "ro", "password": "secret"}


def _stored(env) -> dict:
    return json.loads(env.path.read_text())


@pytest.mark.parametrize("bad", [
    {"engine": "nope"},
    {**MYSQL, "host": ""}, {**MYSQL, "port": "abc"}, {**MYSQL, "port": 70000},
    {**MYSQL, "host": "a\nb"}, {**PG, "database": ""},
])
async def test_rejects_invalid(bad, env):
    with pytest.raises(dbs.DBSettingsError):
        await dbs.save(bad)
    assert not env.path.exists() and settings.db_engine == "sqlite" and env.calls == []


@pytest.mark.parametrize("payload", [MYSQL, PG])
async def test_save_writes_config_file_and_applies_live(payload, env):
    out = await dbs.save({**payload, "password": "p@ss#w'rd\\${X}"})   # ${X} is fine in JSON
    stored = _stored(env)
    assert stored["db_engine"] == payload["engine"] and stored["db_host"] == payload["host"]
    assert stored["db_readonly_password"] == "p@ss#w'rd\\${X}"
    assert (settings.db_engine, settings.db_host, settings.db_name) == (
        payload["engine"], payload["host"], "gm")
    assert settings.db_readonly_password == "p@ss#w'rd\\${X}"
    assert "password" not in out and out["password_set"] is True
    assert env.calls == ["schema", "result", "semantic"]


async def test_postgres_default_port_when_blank(env):
    await dbs.save({**PG, "port": ""})
    assert _stored(env)["db_port"] == 5432 and settings.db_port == 5432


async def test_failed_connection_changes_nothing(env, monkeypatch):
    async def _fail(cfg):
        raise dbs.DBSettingsError("Could not connect")
    monkeypatch.setattr(dbs, "_test_connection", _fail)
    with pytest.raises(dbs.DBSettingsError):
        await dbs.save(MYSQL)
    assert not env.path.exists() and settings.db_engine == "sqlite" and env.calls == []


async def test_omitted_password_kept_only_for_same_target(env):
    await dbs.save(MYSQL)
    p = {k: v for k, v in MYSQL.items() if k != "password"}
    await dbs.save({**p, "database": "other_db"})            # same engine/host/port/user: keeps it
    assert settings.db_readonly_password == "secret"
    for changed in ({"host": "evil.example.com"}, {"port": 3307}, {"username": "root2"},
                    {"engine": "postgresql", "port": 3306}):
        with pytest.raises(dbs.DBSettingsError, match="Re-enter the password"):
            await dbs.save({**p, **changed})
    assert settings.db_host == "localhost" and settings.db_engine == "mysql"


async def test_switch_to_sqlite_keeps_server_values(env):
    await dbs.save(MYSQL)
    await dbs.save({"engine": "sqlite"})
    stored = _stored(env)
    assert stored["db_engine"] == "sqlite" and stored["db_host"] == "localhost"
    assert stored["db_readonly_password"] == "secret"
    assert settings.db_engine == "sqlite" and settings.db_host == "localhost"


async def test_unwritable_config_is_a_clean_error(env, monkeypatch):
    def _boom(updates):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(dbs.db_config_file, "write", _boom)
    with pytest.raises(dbs.DBSettingsError, match="Could not save"):
        await dbs.save(MYSQL)
    assert settings.db_engine == "sqlite" and env.calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_saved_file_is_private(env):
    await dbs.save(MYSQL)
    assert stat.S_IMODE(env.path.stat().st_mode) == 0o600


def test_catalogue_never_exposes_password():
    cfg = dbs.current_config()
    assert "password" not in cfg and "password_set" in cfg
    assert {e["key"] for e in cfg["engines"]} == {"mysql", "postgresql", "sqlite", "mssql", "oracle"}
    assert [e["key"] for e in cfg["engines"] if e["ready"]] == ["mysql", "postgresql", "sqlite", "mssql", "oracle"]
