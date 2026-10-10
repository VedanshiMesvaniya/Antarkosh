"""Unit tests for src/sql/registry.py: databases, connections, and safety."""

from __future__ import annotations

import json
import logging
import os
import stat
import types
from pathlib import Path

import pytest
import yaml

from src.core.config import settings
from src.sql import registry
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID


@pytest.fixture
def temp_connections(tmp_path, monkeypatch) -> Path:
    """Fixture providing an isolated connections.json path."""
    conn_path = tmp_path / "config" / "connections.json"
    monkeypatch.setenv("CONNECTIONS_FILE", str(conn_path))
    monkeypatch.delenv("DB_CONFIG_FILE", raising=False)
    return conn_path


@pytest.fixture
def temp_db_dir(tmp_path) -> Path:
    """Fixture providing a mock databases directory."""
    db_dir = tmp_path / "databases"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir


def test_list_databases_includes_erp_main():
    """list_databases() should discover existing databases including erp_main and exclude _template."""
    dbs = registry.list_databases()
    assert len(dbs) >= 1
    db_ids = [db["id"] for db in dbs]
    assert "erp_main" in db_ids
    assert "_template" not in db_ids

    erp = next(db for db in dbs if db["id"] == "erp_main")
    assert erp["engine"] == "mysql"
    assert erp["display_name"] == "ERP (Main)"


def test_get_database_erp_main():
    """get_database('erp_main') loads valid configuration with Engine validated."""
    db = registry.get_database("erp_main")
    assert db["id"] == "erp_main"
    assert db["engine"] == Engine.MYSQL.value
    assert "display_name" in db


def test_get_database_invalid_id_raises():
    """Invalid database id regex must raise ValueError."""
    with pytest.raises(ValueError, match="Invalid db_id"):
        registry.get_database("INVALID_NAME!")

    with pytest.raises(ValueError, match="Invalid db_id"):
        registry.get_database("_template")


def test_get_database_nonexistent_raises_file_not_found():
    """Missing db.yaml must raise FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Database configuration not found"):
        registry.get_database("nonexistent_db_xyz")


def test_get_database_validates_engine_with_engine(temp_db_dir):
    """db.yaml engine field must be validated against Engine enum."""
    custom_dir = temp_db_dir / "custom_db"
    custom_dir.mkdir()
    (custom_dir / "db.yaml").write_text(
        yaml.safe_dump({"id": "custom_db", "engine": "postgresql", "display_name": "Custom PG"}),
        encoding="utf-8",
    )
    loaded = registry.get_database("custom_db", databases_dir=temp_db_dir)
    assert loaded["engine"] == "postgresql"

    # Alias case normalization
    alias_dir = temp_db_dir / "alias_db"
    alias_dir.mkdir()
    (alias_dir / "db.yaml").write_text(
        yaml.safe_dump({"id": "alias_db", "engine": "Postgres", "display_name": "Alias PG"}),
        encoding="utf-8",
    )
    loaded_alias = registry.get_database("alias_db", databases_dir=temp_db_dir)
    assert loaded_alias["engine"] == "postgresql"

    # Invalid engine
    bad_dir = temp_db_dir / "bad_db"
    bad_dir.mkdir()
    (bad_dir / "db.yaml").write_text(
        yaml.safe_dump({"id": "bad_db", "engine": "unsupported_dialect", "display_name": "Bad"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unknown database engine"):
        registry.get_database("bad_db", databases_dir=temp_db_dir)

    # Missing engine
    missing_dir = temp_db_dir / "missing_db"
    missing_dir.mkdir()
    (missing_dir / "db.yaml").write_text(
        yaml.safe_dump({"id": "missing_db", "display_name": "No Engine"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Missing required 'engine' field"):
        registry.get_database("missing_db", databases_dir=temp_db_dir)


def test_first_use_imports_legacy_db_connection(tmp_path, monkeypatch):
    """When connections.json is absent, import existing config/db_connection.json once."""
    conn_path = tmp_path / "connections.json"
    legacy_path = tmp_path / "legacy_db_connection.json"
    legacy_path.write_text(
        json.dumps({
            "db_engine": "postgresql",
            "db_host": "remote.pg.internal",
            "db_port": 5432,
            "db_name": "prod_db",
            "db_readonly_user": "reader",
            "db_readonly_password": "legacy_p@ssword!#",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONNECTIONS_FILE", str(conn_path))
    monkeypatch.setenv("DB_CONFIG_FILE", str(legacy_path))

    assert not conn_path.exists()
    conn = registry.get_connection(DEFAULT_DB_ID)
    assert conn_path.exists()

    assert conn["host"] == "remote.pg.internal"
    assert conn["port"] == 5432
    assert conn["database"] == "prod_db"
    assert conn["username"] == "reader"
    assert conn["password"] == "legacy_p@ssword!#"
    assert conn["engine"] == "postgresql"

    # Check that disk file contains the entry
    stored = json.loads(conn_path.read_text(encoding="utf-8"))
    assert DEFAULT_DB_ID in stored
    assert stored[DEFAULT_DB_ID]["host"] == "remote.pg.internal"


def test_first_use_imports_from_settings_if_legacy_absent(tmp_path, monkeypatch):
    """When neither connections.json nor db_connection.json exist, import from settings once."""
    conn_path = tmp_path / "connections.json"
    monkeypatch.setenv("CONNECTIONS_FILE", str(conn_path))
    monkeypatch.delenv("DB_CONFIG_FILE", raising=False)
    monkeypatch.setattr(registry, "CONFIG_DIR", tmp_path / "nonexistent_config")
    monkeypatch.setattr(settings, "db_engine", "sqlite")
    monkeypatch.setattr(settings, "db_host", "")
    monkeypatch.setattr(settings, "db_port", 3306)

    assert not conn_path.exists()
    conn = registry.get_connection(DEFAULT_DB_ID)
    assert conn_path.exists()
    assert conn["engine"] == "sqlite"


def test_save_connection_and_get_connection_roundtrip(temp_connections):
    """save_connection stores credentials and get_connection retrieves them accurately."""
    payload = {
        "engine": "postgresql",
        "host": "postgres.internal",
        "port": 5432,
        "database": "sales_db",
        "username": "sales_ro",
        "password": "complex_P@ss!#${VAR}",
    }
    registry.save_connection("sales_pg", payload)

    retrieved = registry.get_connection("sales_pg")
    assert retrieved == payload
    # Must return a copy, mutating retrieved does not change store
    retrieved["host"] = "mutated"
    assert registry.get_connection("sales_pg")["host"] == "postgres.internal"


def test_save_connection_merges_and_keeps_existing_fields(temp_connections):
    """Updating a subset of fields should preserve unchanged stored fields."""
    registry.save_connection("sales_pg", {"host": "h1", "port": 5432, "database": "d1"})
    registry.save_connection("sales_pg", {"database": "d2", "username": "u1"})

    conn = registry.get_connection("sales_pg")
    assert conn["host"] == "h1"
    assert conn["port"] == 5432
    assert conn["database"] == "d2"
    assert conn["username"] == "u1"


def test_save_connection_normalizes_legacy_keys(temp_connections):
    """save_connection accepts legacy db_* field names and maps them to canonical names."""
    registry.save_connection(
        "erp_main",
        {
            "db_host": "db.corp.net",
            "db_port": 3306,
            "db_name": "erp_prod",
            "db_readonly_user": "agent",
            "db_readonly_password": "supersecretpassword",
        },
    )
    conn = registry.get_connection("erp_main")
    assert conn["host"] == "db.corp.net"
    assert conn["port"] == 3306
    assert conn["database"] == "erp_prod"
    assert conn["username"] == "agent"
    assert conn["password"] == "supersecretpassword"


def test_save_connection_ignores_unknown_keys(temp_connections):
    """Unknown keys such as API keys must never be written to connections.json."""
    registry.save_connection("erp_main", {"host": "h1", "api_key": "secret_leak", "unknown_val": 123})
    conn = registry.get_connection("erp_main")
    assert "api_key" not in conn
    assert "unknown_val" not in conn

    raw = json.loads(temp_connections.read_text(encoding="utf-8"))
    assert "api_key" not in raw["erp_main"]


def test_save_connection_invalid_port_raises(temp_connections):
    """Invalid port numbers must raise ValueError."""
    with pytest.raises(ValueError, match="port"):
        registry.save_connection("erp_main", {"port": "not_a_number"})

    with pytest.raises(ValueError, match="port"):
        registry.save_connection("erp_main", {"port": 999999})


def test_atomic_write_leaves_no_temp_files(temp_connections):
    """Atomic write must clean up any temporary files upon completion."""
    registry.save_connection("erp_main", {"host": "h1", "port": 3306})
    parent_files = [p.name for p in temp_connections.parent.iterdir()]
    assert parent_files == [temp_connections.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_saved_connections_file_is_private(temp_connections):
    """connections.json must have 0o600 permissions."""
    registry.save_connection("erp_main", {"host": "h1"})
    assert stat.S_IMODE(temp_connections.stat().st_mode) == 0o600


def test_passwords_never_appear_in_log_output(temp_connections, caplog):
    """Assert passwords never appear in log records across all registry operations."""
    caplog.set_level(logging.DEBUG)
    secret_password = "P@ssw0rd_Strictly_Never_Logged_!#99"

    registry.save_connection("erp_main", {"password": secret_password, "host": "db.local"})
    conn = registry.get_connection("erp_main")
    assert conn["password"] == secret_password

    registry.list_databases()
    registry.get_database("erp_main")

    log_content = caplog.text
    assert secret_password not in log_content


def test_db_config_file_shim_integration(temp_connections):
    """src.core.db_config_file should delegate read/write/apply_to to registry."""
    from src.core import db_config_file

    db_config_file.write({"db_host": "shim_host", "db_port": 5432, "db_name": "shim_db"})
    read_vals = db_config_file.read()
    assert read_vals["db_host"] == "shim_host"
    assert read_vals["db_port"] == 5432
    assert read_vals["db_name"] == "shim_db"

    target = types.SimpleNamespace(db_host="", db_port=0, db_name="")
    applied = db_config_file.apply_to(target)
    assert "db_host" in applied
    assert target.db_host == "shim_host"
    assert target.db_port == 5432
