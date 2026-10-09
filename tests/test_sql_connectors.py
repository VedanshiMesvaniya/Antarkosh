"""Tests for src/sql/connectors package."""

from __future__ import annotations

import sqlite3

import pytest

from src.sql.connectors import get_connector
from src.sql.connectors.base import Connector
from src.sql.connectors.sqlite import SQLiteConnector


def test_sqlite_connector_implements_protocol():
    connector = SQLiteConnector()
    assert isinstance(connector, Connector)


@pytest.mark.asyncio
async def test_sqlite_connector_test_and_run_readonly(tmp_path):
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users (name) VALUES ('Alice'), ('Bob')")
    conn.commit()
    conn.close()

    connector = SQLiteConnector(db_path=db_file)
    # 1. test() passes on valid file
    await connector.test()

    # 2. run_readonly() returns rows as dicts
    rows = await connector.run_readonly("SELECT * FROM users ORDER BY id ASC")
    assert rows == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

    # 3. test() raises on missing file
    missing_conn = SQLiteConnector(db_path=tmp_path / "missing.db")
    with pytest.raises(FileNotFoundError):
        await missing_conn.test()


def test_get_connector_sqlite():
    conn = get_connector("sqlite")
    assert isinstance(conn, SQLiteConnector)


def test_mysql_connector_implements_protocol():
    from src.sql.connectors.mysql import MySQLConnector
    connector = MySQLConnector()
    assert isinstance(connector, Connector)


def test_get_connector_mysql():
    from src.sql.connectors.mysql import MySQLConnector
    conn = get_connector("mysql")
    assert isinstance(conn, MySQLConnector)


def test_postgresql_connector_implements_protocol():
    from src.sql.connectors.postgresql import PostgreSQLConnector
    connector = PostgreSQLConnector()
    assert isinstance(connector, Connector)


def test_get_connector_postgresql():
    from src.sql.connectors.postgresql import PostgreSQLConnector
    conn = get_connector("postgresql")
    assert isinstance(conn, PostgreSQLConnector)


@pytest.mark.asyncio
async def test_postgresql_connector_named_params_rejected():
    pytest.importorskip("asyncpg")
    from src.sql.connectors.postgresql import PostgreSQLConnector
    with pytest.raises(ValueError, match="Named query parameters"):
        await PostgreSQLConnector().run_readonly("SELECT 1", {"a": 1})


def test_mssql_connector_implements_protocol():
    from src.sql.connectors.mssql import MSSQLConnector
    connector = MSSQLConnector()
    assert isinstance(connector, Connector)


def test_get_connector_mssql():
    from src.sql.connectors.mssql import MSSQLConnector
    conn = get_connector("mssql")
    assert isinstance(conn, MSSQLConnector)


def test_oracle_connector_implements_protocol():
    from src.sql.connectors.oracle import OracleConnector
    connector = OracleConnector()
    assert isinstance(connector, Connector)


def test_get_connector_oracle():
    from src.sql.connectors.oracle import OracleConnector
    conn = get_connector("oracle")
    assert isinstance(conn, OracleConnector)


def test_get_connector_unsupported():
    with pytest.raises(ValueError, match="Unsupported db_engine"):
        get_connector("cockroachdb")


@pytest.mark.asyncio
async def test_mysql_connector_test_signature():
    from src.sql.connectors.mysql import MySQLConnector
    assert callable(MySQLConnector().test)


@pytest.mark.asyncio
async def test_mssql_connector_build_conn_str():
    from src.sql.connectors.mssql import MSSQLConnector
    c = MSSQLConnector()
    s = c._build_conn_str({"host": "h", "port": 1433, "database": "d", "username": "u", "password": "p"})
    assert "SERVER=h,1433" in s
    assert "DATABASE=d" in s
    assert "UID=u" in s
    assert "PWD=p" in s

