"""A connection can pin the schema the app works in (PostgreSQL, Oracle), with no change to the database.

PostgreSQL: the connector sets search_path on every connection, so the catalog queries
(current_schema()) and the generated SQL's bare table names both resolve inside that schema.
Oracle: ALTER SESSION SET CURRENT_SCHEMA after connecting (checked with a fake driver only).
The PostgreSQL path was also verified against a real server with a read-only role.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.core import db_settings
from src.core.config import settings
from src.sql import registry
from src.sql.connectors import oracle as oracle_mod
from src.sql.connectors import postgresql as pg_mod
from src.sql.engine import ENGINES, SCHEMA_ENGINES, validate_schema_name


@pytest.fixture()
def conn_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "connections.json"
    monkeypatch.setenv("CONNECTIONS_FILE", str(path))
    monkeypatch.delenv("DB_CONNECTIONS_FILE", raising=False)
    return path


# ------------------------------------------------------------------ the name itself


@pytest.mark.parametrize("name", ["erp_food", "ErpFood", "_x", "sales$1", "HR", "a" * 63])
def test_plain_identifiers_are_accepted(name):
    assert validate_schema_name(name) == name


@pytest.mark.parametrize("name", ["erp food", 'a"b', "a;drop table x", "a-b", "1abc", "a.b", "a" * 64, "ü", "a\nb"])
def test_anything_else_is_rejected(name):
    with pytest.raises(ValueError):
        validate_schema_name(name)


def test_blank_and_none_mean_no_schema_and_whitespace_is_trimmed():
    assert validate_schema_name(None) == "" and validate_schema_name("   ") == "" and validate_schema_name(" hr ") == "hr"
    with pytest.raises(ValueError):
        validate_schema_name(5)


def test_only_postgresql_and_oracle_offer_the_field():
    assert SCHEMA_ENGINES == {"postgresql", "oracle"}
    for key, spec in ENGINES.items():
        assert ("schema_name" in spec["fields"]) == (key in SCHEMA_ENGINES), key


# ------------------------------------------------------------------ saved connection


def test_schema_is_saved_kept_when_omitted_and_cleared_with_blank(conn_file: Path):
    registry.save_connection("shop_db", {"engine": "postgresql", "host": "h", "schema_name": "erp_food"})
    assert registry.get_connection("shop_db")["schema_name"] == "erp_food"
    registry.save_connection("shop_db", {"host": "h2", "schema_name": None})  # not supplied: keep
    assert registry.get_connection("shop_db")["schema_name"] == "erp_food"
    registry.save_connection("shop_db", {"schema_name": ""})  # explicit blank: clear
    assert registry.get_connection("shop_db")["schema_name"] == ""


def test_an_unsafe_schema_is_never_written(conn_file: Path):
    with pytest.raises(ValueError):
        registry.save_connection("shop_db", {"engine": "postgresql", "schema_name": "x; DROP"})
    assert not conn_file.exists() or "shop_db" not in json.loads(conn_file.read_text())


# ------------------------------------------------------------------ PostgreSQL connector


class _FakePgConn:
    def __init__(self, visible: bool = True):
        self.visible, self.queries, self.closed = visible, [], False

    async def fetchval(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return 1 if ("schemata" not in sql or self.visible) else None

    def transaction(self, readonly: bool = False):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                assert readonly is True
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Tx()

    async def fetch(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return [{"n": 1}]

    async def close(self):
        self.closed = True

    def terminate(self):
        self.closed = True


@pytest.fixture()
def fake_asyncpg(monkeypatch):
    seen: dict[str, Any] = {"kwargs": [], "conn": _FakePgConn()}

    async def connect(**kw: Any):
        seen["kwargs"].append(kw)
        return seen["conn"]

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    return seen


CFG = {"host": "h", "port": 5432, "database": "d", "username": "u", "password": "p"}


def test_search_path_value_is_quoted_so_case_survives():
    assert pg_mod._search_path("erp_food") == '"erp_food"' and pg_mod._search_path("ErpFood") == '"ErpFood"'
    assert pg_mod._search_path('a"b') == '"a""b"'


@pytest.mark.asyncio
async def test_queries_run_with_the_schema_on_the_search_path(fake_asyncpg):
    rows = await pg_mod.PostgreSQLConnector({**CFG, "schema_name": "erp_food"}).run_readonly("SELECT 1")
    ss = fake_asyncpg["kwargs"][0]["server_settings"]
    assert rows == [{"n": 1}] and ss["search_path"] == '"erp_food"'
    assert ss["application_name"] == "antarkosh" and "statement_timeout" in ss  # nothing else was lost


@pytest.mark.asyncio
async def test_without_a_schema_the_connection_is_exactly_as_before(fake_asyncpg):
    await pg_mod.PostgreSQLConnector(dict(CFG)).run_readonly("SELECT 1")
    assert "search_path" not in fake_asyncpg["kwargs"][0]["server_settings"]


@pytest.mark.asyncio
async def test_connector_refuses_an_unsafe_schema_even_if_it_bypassed_validation(fake_asyncpg):
    with pytest.raises(ValueError):
        await pg_mod.PostgreSQLConnector({**CFG, "schema_name": 'x"; DROP'}).run_readonly("SELECT 1")
    assert fake_asyncpg["kwargs"] == []  # never connected


@pytest.mark.asyncio
async def test_connection_test_checks_the_schema_is_visible(fake_asyncpg):
    await pg_mod.PostgreSQLConnector().test({**CFG, "schema_name": "erp_food"})
    assert fake_asyncpg["kwargs"][0]["server_settings"] == {"search_path": '"erp_food"'}
    assert fake_asyncpg["conn"].closed is True

    fake_asyncpg["conn"] = _FakePgConn(visible=False)
    with pytest.raises(RuntimeError, match="not found, or this user has no access"):
        await pg_mod.PostgreSQLConnector().test({**CFG, "schema_name": "locked"})
    assert fake_asyncpg["conn"].closed is True  # closed even on failure


@pytest.mark.asyncio
async def test_connection_test_without_a_schema_does_not_look_for_one(fake_asyncpg):
    await pg_mod.PostgreSQLConnector().test(dict(CFG))
    assert all("schemata" not in sql for sql, _ in fake_asyncpg["conn"].queries)


# ------------------------------------------------------------------ Oracle connector (fake driver)


class _FakeOraConn:
    def __init__(self, log: list[str], fail_alter: bool = False):
        self.log, self.fail_alter = log, fail_alter

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        conn = self

        class _Cur:
            description = [("N",)]

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def execute(s, sql, params=None):
                conn.log.append(sql)
                if conn.fail_alter and sql.startswith("ALTER SESSION"):
                    raise RuntimeError("ORA-01435: user does not exist")

            def fetchone(s):
                return (1,)

            def fetchall(s):
                return [(1,)]

        return _Cur()


@pytest.fixture()
def fake_oracledb(monkeypatch):
    state: dict[str, Any] = {"log": [], "fail": False, "connects": 0}

    def connect(**kw: Any):
        state["connects"] += 1
        return _FakeOraConn(state["log"], state["fail"])

    monkeypatch.setitem(sys.modules, "oracledb", types.SimpleNamespace(connect=connect))
    return state


ORA = {"host": "h", "port": 1521, "service_name": "XEPDB1", "username": "u", "password": "p"}


@pytest.mark.asyncio
async def test_oracle_selects_the_schema_before_running_the_query(fake_oracledb):
    rows = await oracle_mod.OracleConnector({**ORA, "schema_name": "HR"}).run_readonly("SELECT 1 FROM DUAL")
    assert rows == [{"n": 1}]
    assert fake_oracledb["log"] == ["ALTER SESSION SET CURRENT_SCHEMA = HR", "SELECT 1 FROM DUAL"]


@pytest.mark.asyncio
async def test_oracle_without_a_schema_runs_only_the_query(fake_oracledb):
    await oracle_mod.OracleConnector(dict(ORA)).run_readonly("SELECT 1 FROM DUAL")
    assert fake_oracledb["log"] == ["SELECT 1 FROM DUAL"]


@pytest.mark.asyncio
async def test_oracle_refuses_an_unsafe_schema_before_connecting(fake_oracledb):
    with pytest.raises(ValueError):
        await oracle_mod.OracleConnector({**ORA, "schema_name": "HR; DROP USER x"}).run_readonly("SELECT 1 FROM DUAL")
    assert fake_oracledb["connects"] == 0


@pytest.mark.asyncio
async def test_oracle_schema_that_cannot_be_selected_gives_a_clear_error(fake_oracledb):
    fake_oracledb["fail"] = True
    with pytest.raises(RuntimeError, match="Schema 'NOPE' could not be selected"):
        await oracle_mod.OracleConnector().test({**ORA, "schema_name": "NOPE"})


# ------------------------------------------------------------------ the Settings page (default database)


def _form(**over: Any) -> dict[str, Any]:
    return {"engine": "postgresql", "host": "h", "port": 5432, "database": "d", "username": "u", "password": "p", **over}


def test_settings_clean_accepts_a_schema_for_postgres_and_oracle_only():
    assert db_settings._clean(_form(schema_name=" erp_food "))["schema_name"] == "erp_food"
    assert db_settings._clean({"engine": "oracle", "host": "h", "service_name": "S", "username": "u", "password": "p", "schema_name": "HR"})["schema_name"] == "HR"
    assert "schema_name" not in db_settings._clean(_form())  # not sent: left alone
    with pytest.raises(db_settings.DBSettingsError, match="only supported for PostgreSQL and Oracle"):
        db_settings._clean(_form(engine="mysql", schema_name="x"))
    with pytest.raises(db_settings.DBSettingsError, match="Schema may contain only"):
        db_settings._clean(_form(schema_name="a b"))
    assert db_settings._clean(_form(engine="mysql", schema_name=""))["schema_name"] == ""  # blank is fine anywhere


@pytest.mark.asyncio
async def test_settings_save_stores_the_schema_and_clears_it_when_the_engine_changes(conn_file: Path, monkeypatch):
    monkeypatch.setattr(db_settings, "_test_connection", AsyncMock())
    monkeypatch.setattr(db_settings, "_apply_runtime", MagicMock())
    monkeypatch.setattr(settings, "db_readonly_password", "p")

    cfg = await db_settings.save(_form(schema_name="erp_food"))
    assert registry.get_connection("erp_main")["schema_name"] == "erp_food" and cfg["schema_name"] == "erp_food"

    await db_settings.save(_form(host="h2"))  # schema not sent: kept
    assert registry.get_connection("erp_main")["schema_name"] == "erp_food"

    await db_settings.save(_form(engine="mysql", port=3306))  # no schemas on MySQL: old one dropped
    assert registry.get_connection("erp_main")["schema_name"] == ""


# ------------------------------------------------------------------ admin API for any database


def _client():
    from src.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
def api(monkeypatch, conn_file):
    monkeypatch.setenv("ALLOW_HEADER_AUTH", "1")
    tested: list[dict[str, Any]] = []

    class _Connector:
        def __init__(self, cfg):
            self.cfg = cfg

        async def test(self):
            tested.append(dict(self.cfg))

    monkeypatch.setattr("src.sql.connectors.get_connector", lambda engine, cfg=None: _Connector(cfg or {}))
    monkeypatch.setattr(db_settings, "_apply_runtime", MagicMock())
    return tested


BODY = {"engine": "postgresql", "host": "h", "port": 5432, "database": "d", "username": "u", "password": "p"}
ADMIN = {"X-User-Id": "admin"}


@pytest.mark.asyncio
async def test_api_saves_a_valid_schema_and_the_connector_test_sees_it(api):
    async with _client() as c:
        res = await c.post("/api/databases/shop_db/connection", json={**BODY, "schema_name": "erp_food"}, headers=ADMIN)
    assert res.status_code == 200
    assert api[0]["schema_name"] == "erp_food" and registry.get_connection("shop_db")["schema_name"] == "erp_food"


@pytest.mark.asyncio
async def test_api_rejects_bad_names_and_unsupported_engines_before_testing_or_saving(api, conn_file):
    async with _client() as c:
        bad = await c.post("/api/databases/shop_db/connection", json={**BODY, "schema_name": "x y"}, headers=ADMIN)
        mysql = await c.post("/api/databases/shop_db/connection", json={**BODY, "engine": "mysql", "schema_name": "x"}, headers=ADMIN)
        probe = await c.post("/api/databases/shop_db/test", json={**BODY, "engine": "mssql", "schema_name": "x"}, headers=ADMIN)
    assert [bad.status_code, mysql.status_code, probe.status_code] == [422, 422, 422]
    assert "only supported for PostgreSQL and Oracle" in mysql.json()["detail"]
    assert api == []  # no connection was ever attempted
    assert not conn_file.exists()  # and nothing was saved


@pytest.mark.asyncio
async def test_api_resave_without_the_field_keeps_the_schema_and_tests_with_it(api):
    async with _client() as c:
        await c.post("/api/databases/shop_db/connection", json={**BODY, "schema_name": "erp_food"}, headers=ADMIN)
        again = await c.post("/api/databases/shop_db/connection", json=BODY, headers=ADMIN)
    assert again.status_code == 200
    assert api[-1]["schema_name"] == "erp_food"  # the test used the stored schema
    assert registry.get_connection("shop_db")["schema_name"] == "erp_food"


@pytest.mark.asyncio
async def test_api_switching_to_an_engine_without_schemas_clears_it(api):
    async with _client() as c:
        await c.post("/api/databases/shop_db/connection", json={**BODY, "schema_name": "erp_food"}, headers=ADMIN)
        await c.post("/api/databases/shop_db/connection", json={**BODY, "engine": "mysql", "schema_name": ""}, headers=ADMIN)
    assert registry.get_connection("shop_db")["schema_name"] == ""


# ------------------------------------------------------------------ sync hint


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,expect_hint", [("postgresql", True), ("oracle", True), ("mysql", False)])
async def test_empty_sync_points_at_the_schema_setting_only_where_it_exists(engine, expect_hint, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": engine})
    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", AsyncMock(return_value=[]))
    result = await sync_live_schema(embedding_service=AsyncMock(), vector_store=AsyncMock(), db_id="shop_db")
    assert result["status"] == "error" and result["message"].startswith("No schema rows returned from database")
    assert ("Schema field" in result["message"]) is expect_hint
