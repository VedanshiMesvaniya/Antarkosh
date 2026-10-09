"""Unit tests for Engine enum and dialect mapping."""

from __future__ import annotations

import pytest

from src.sql.engine import Engine


def test_engine_enum_values():
    assert Engine.SQLITE == "sqlite"
    assert Engine.MYSQL == "mysql"
    assert Engine.POSTGRESQL == "postgresql"
    assert Engine.MSSQL == "mssql"
    assert Engine.ORACLE == "oracle"


def test_engine_key_property():
    assert Engine.SQLITE.key == "sqlite"
    assert Engine.MYSQL.key == "mysql"
    assert Engine.POSTGRESQL.key == "postgresql"
    assert Engine.MSSQL.key == "mssql"
    assert Engine.ORACLE.key == "oracle"


def test_engine_sqlglot_property():
    assert Engine.SQLITE.sqlglot == "sqlite"
    assert Engine.MYSQL.sqlglot == "mysql"
    assert Engine.POSTGRESQL.sqlglot == "postgres"
    assert Engine.MSSQL.sqlglot == "tsql"
    assert Engine.ORACLE.sqlglot == "oracle"


def test_engine_from_value_canonical():
    assert Engine.from_value("sqlite") is Engine.SQLITE
    assert Engine.from_value("mysql") is Engine.MYSQL
    assert Engine.from_value("postgresql") is Engine.POSTGRESQL
    assert Engine.from_value("mssql") is Engine.MSSQL
    assert Engine.from_value("oracle") is Engine.ORACLE


def test_engine_from_value_aliases():
    assert Engine.from_value("postgres") is Engine.POSTGRESQL
    assert Engine.from_value("tsql") is Engine.MSSQL
    assert Engine.from_value("  POSTGRES  ") is Engine.POSTGRESQL
    assert Engine.from_value("TSQL") is Engine.MSSQL


def test_engine_from_value_instance():
    assert Engine.from_value(Engine.MYSQL) is Engine.MYSQL
    assert Engine.from_value(Engine.POSTGRESQL) is Engine.POSTGRESQL


def test_engine_from_value_invalid():
    for invalid in ["", "mariadb", "db2", "invalid_engine", 123, None]:
        with pytest.raises(ValueError):
            Engine.from_value(invalid)
