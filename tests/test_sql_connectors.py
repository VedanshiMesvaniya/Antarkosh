"""Tests for src/sql/connectors package."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.sql.connectors.base import Connector
from src.sql.connectors.sqlite import SQLiteConnector, DB_PATH
from src.sql.connectors import get_connector


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


def test_get_connector_unsupported():
    with pytest.raises(ValueError, match="Unsupported db_engine"):
        get_connector("cockroachdb")
