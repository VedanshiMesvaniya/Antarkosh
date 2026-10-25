"""Test-session setup.

The admin-saved DB connection (config/db_connection.json) intentionally wins over
env/.env at runtime. Point it at a file that does not exist for the whole test
session so a connection saved on a developer machine can never leak into tests
(CI sets DB_ENGINE=sqlite and has no such file). Tests that exercise the file
itself set DB_CONFIG_FILE explicitly.
"""

from __future__ import annotations

import os
import tempfile

os.environ["ALLOW_HEADER_AUTH"] = "1"  # tests authenticate with the X-User-Id header
os.environ["SESSION_STORE_FILE"] = os.path.join(tempfile.gettempdir(), "antarkosh-tests-sessions.json")
os.environ["DB_CONFIG_FILE"] = os.path.join(tempfile.gettempdir(), "antarkosh-tests-no-db-config.json")
os.environ["CONNECTIONS_FILE"] = os.path.join(tempfile.gettempdir(), "antarkosh-tests-no-connections.json")
os.environ["ACTIVE_DB_FILE"] = os.path.join(tempfile.gettempdir(), "antarkosh-tests-no-active-db.json")


import shutil

import pytest


@pytest.fixture(autouse=True)
def _isolated_feature_flag_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test its own feature-flag config folder.

    Connecting a database (``save_connection``, ``choose_db_id``, ``ensure_db_folder``, a schema sync) writes that database's
    flags into ``config/feature_flags.yaml``. Without this, a test run would add its throwaway ids (``pg_live``, ``ghost_db`` ...)
    to the developer's real, git-ignored file, and flags written by one test would leak into the next. The folder starts with
    copies of the tracked files only (the example file and ``features.yaml``), which is what a fresh clone sees. A test that
    needs its own folder still sets ``CONFIG_DIR`` itself and wins.
    """
    from src.core.config import CONFIG_DIR as real_config_dir
    from src.utils import feature_flags as ff

    folder = tmp_path_factory.mktemp("flag_config")
    for name in ("feature_flags.example.yaml", "features.yaml"):
        if (real_config_dir / name).exists():
            shutil.copy2(real_config_dir / name, folder / name)
    monkeypatch.setattr(ff, "CONFIG_DIR", folder)
    for cache, value in (("_DB_FLAGS_CACHE", None), ("_DB_CACHE_MTIME", 0.0), ("_FLAGS_CACHE", None), ("_CACHE_MTIME", 0.0)):
        monkeypatch.setattr(ff, cache, value)


@pytest.fixture(autouse=True)
def _no_real_language_model_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The column-meaning phase would call a real model during any test that runs a full schema sync with the flag on.
    Tests that exercise it pass a fake model explicitly (``run_meaning_phase(llm=...)``); nothing else may spend quota."""
    from src.sql.sa import meaning_phase

    def refuse() -> None:
        raise RuntimeError("tests must not call a real language model")

    monkeypatch.setattr(meaning_phase, "_default_llm", refuse)
