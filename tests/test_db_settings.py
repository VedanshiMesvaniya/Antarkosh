"""Admin database-connection settings: validation, safe .env write, live apply."""

from __future__ import annotations

import sys
import types

import pytest

import src.core.db_settings as dbs
from src.core.config import settings


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# keep me\nGEMINI_API_KEY=abc\nDB_ENGINE=sqlite\nOTHER=1\n")
    monkeypatch.setattr(dbs, "ENV_PATH", path)
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


@pytest.mark.parametrize("bad", [
    {"engine": "oracle"}, {"engine": "nope"},
    {**MYSQL, "host": ""}, {**MYSQL, "port": "abc"}, {**MYSQL, "port": 70000},
    {**MYSQL, "host": "a\nb"},
])
async def test_rejects_invalid(bad, env):
    before = env.path.read_text()
    with pytest.raises(dbs.DBSettingsError):
        await dbs.save(bad)
    assert env.path.read_text() == before and settings.db_engine == "sqlite"


async def test_save_writes_only_db_keys_and_applies_live(env):
    out = await dbs.save({**MYSQL, "password": "p@ss#w'rd\\"})
    text = env.path.read_text()
    assert "# keep me" in text and "GEMINI_API_KEY=abc" in text and "OTHER=1" in text
    assert (settings.db_engine, settings.db_host, settings.db_name) == ("mysql", "localhost", "gm")
    assert settings.db_readonly_password == "p@ss#w'rd\\"
    assert "password" not in out and out["password_set"] is True
    assert env.calls == ["schema", "result", "semantic"]


async def test_dollar_brace_password_rolls_back(env):
    before = env.path.read_text()
    with pytest.raises(dbs.DBSettingsError):
        await dbs.save({**MYSQL, "password": "a${X}b"})
    assert env.path.read_text() == before and settings.db_host == ""


async def test_failed_connection_changes_nothing(env, monkeypatch):
    async def _fail(cfg):
        raise dbs.DBSettingsError("Could not connect")
    monkeypatch.setattr(dbs, "_test_connection", _fail)
    before = env.path.read_text()
    with pytest.raises(dbs.DBSettingsError):
        await dbs.save(MYSQL)
    assert env.path.read_text() == before and settings.db_engine == "sqlite" and env.calls == []


async def test_omitted_password_kept_only_for_same_target(env):
    await dbs.save(MYSQL)
    p = {k: v for k, v in MYSQL.items() if k != "password"}
    await dbs.save({**p, "database": "other_db"})            # same host/port/user: keeps password
    assert settings.db_readonly_password == "secret"
    with pytest.raises(dbs.DBSettingsError, match="Re-enter the password"):
        await dbs.save({**p, "host": "evil.example.com"})    # new host: must retype it
    assert settings.db_host == "localhost"


async def test_switch_to_sqlite_keeps_mysql_values(env, monkeypatch):
    await dbs.save(MYSQL)
    await dbs.save({"engine": "sqlite"})
    text = env.path.read_text()
    assert "DB_ENGINE='sqlite'" in text and "DB_HOST='localhost'" in text
    assert settings.db_engine == "sqlite" and settings.db_host == "localhost"


async def test_unwritable_env_is_a_clean_error(env, monkeypatch):
    def _boom(updates):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(dbs, "_write_env", _boom)
    with pytest.raises(dbs.DBSettingsError, match="Could not write"):
        await dbs.save(MYSQL)
    assert settings.db_engine == "sqlite"


def test_catalogue_never_exposes_password():
    cfg = dbs.current_config()
    assert "password" not in cfg and "password_set" in cfg
    assert {e["key"] for e in cfg["engines"]} == {"mysql", "sqlite", "mssql", "postgresql", "oracle"}
    assert [e["key"] for e in cfg["engines"] if e["ready"]] == ["mysql", "sqlite"]
