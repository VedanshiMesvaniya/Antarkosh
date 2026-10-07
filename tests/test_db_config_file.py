"""The persisted DB-connection file: read/write/apply, safety, and startup precedence."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from src.core import db_config_file as cf

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "db_connection.json"
    monkeypatch.setenv("DB_CONFIG_FILE", str(path))
    return path


def test_missing_file_reads_empty(cfg):
    assert cf.read() == {}


def test_write_then_read_roundtrip_and_no_temp_files(cfg):
    cf.write({"db_engine": "postgresql", "db_host": "h", "db_port": 5432, "db_name": "d",
              "db_readonly_user": "u", "db_readonly_password": "p@ss#w'rd\\${X}"})
    assert cf.read()["db_readonly_password"] == "p@ss#w'rd\\${X}"   # no .env-style escaping limits
    assert cf.read()["db_port"] == 5432
    assert [p.name for p in cfg.parent.iterdir()] == [cfg.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_is_private(cfg):
    cf.write({"db_engine": "mysql"})
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_write_merges_and_keeps_other_keys(cfg):
    cf.write({"db_engine": "mysql", "db_host": "db1", "db_port": 3306})
    cf.write({"db_engine": "sqlite"})
    assert cf.read() == {"db_engine": "sqlite", "db_host": "db1", "db_port": 3306}


def test_only_known_keys_are_read_or_written(cfg):
    cfg.write_text(json.dumps({"db_engine": "mysql", "gemini_api_key": "evil", "upload_dir": "/x"}))
    assert cf.read() == {"db_engine": "mysql"}
    cf.write({"db_host": "h", "gemini_api_key": "evil"})
    assert "gemini_api_key" not in json.loads(cfg.read_text())


@pytest.mark.parametrize("content", ["{not json", "[]", "null", ""])
def test_corrupt_file_is_ignored_not_fatal(cfg, content):
    cfg.write_text(content)
    assert cf.read() == {}


def test_bom_is_tolerated(cfg):
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"db_engine": "mysql"}).encode())
    assert cf.read() == {"db_engine": "mysql"}


def test_one_bad_value_does_not_drop_the_rest(cfg):
    cfg.write_text(json.dumps({"db_engine": "PostgreSQL ", "db_port": "abc", "db_host": "h"}))
    assert cf.read() == {"db_engine": "postgresql", "db_host": "h"}


def test_invalid_write_raises_and_leaves_file_alone(cfg):
    cf.write({"db_engine": "mysql"})
    before = cfg.read_text()
    with pytest.raises(ValueError):
        cf.write({"db_port": 99999})
    assert cfg.read_text() == before


def test_failed_replace_keeps_old_file_and_cleans_up(cfg, monkeypatch):
    cf.write({"db_engine": "mysql"})
    before = cfg.read_text()

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(cf.os, "replace", boom)
    with pytest.raises(PermissionError):
        cf.write({"db_engine": "postgresql"})
    assert cfg.read_text() == before
    assert [p.name for p in cfg.parent.iterdir()] == [cfg.name]


def test_apply_to_sets_attributes(cfg):
    cf.write({"db_engine": "postgresql", "db_port": 5432})
    target = types.SimpleNamespace(db_engine="sqlite", db_port=3306, untouched="x")
    assert sorted(cf.apply_to(target)) == ["db_engine", "db_port"]
    assert (target.db_engine, target.db_port, target.untouched) == ("postgresql", 5432, "x")


def _engine_at_startup(env_engine: str, config_file: Path) -> str:
    """Import the real settings in a fresh interpreter and report db_engine."""
    env = {**os.environ, "DB_ENGINE": env_engine, "DB_CONFIG_FILE": str(config_file)}
    out = subprocess.run(
        [sys.executable, "-c", "from src.core.config import settings; print(settings.db_engine)"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_startup_file_wins_over_env_var(cfg):
    cf.write({"db_engine": "postgresql"})
    assert _engine_at_startup("sqlite", cfg) == "postgresql"   # e.g. render.yaml's DB_ENGINE


def test_startup_without_file_behaves_exactly_as_before(cfg):
    assert _engine_at_startup("sqlite", cfg) == "sqlite"


def test_startup_survives_corrupt_file(cfg):
    cfg.write_text("{definitely not json")
    assert _engine_at_startup("sqlite", cfg) == "sqlite"
