"""Per-DB feature flags: default off, strict parsing, erp_main pinned off."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.utils import feature_flags as ff


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ff, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ff, "_DB_FLAGS_CACHE", None)
    monkeypatch.setattr(ff, "_DB_CACHE_MTIME", 0.0)
    monkeypatch.setattr(ff, "_FLAGS_CACHE", None)
    monkeypatch.setattr(ff, "_CACHE_MTIME", 0.0)
    return tmp_path / "feature_flags.yaml"


def test_missing_file_everything_off(cfg: Path):
    assert ff.is_db_feature_enabled("auto_catalog", "sakila") is False


def test_enabled_only_for_listed_db_and_flag(cfg: Path):
    cfg.write_text(
        "features:\n  fast_path_enabled: true\n"
        "db_features:\n  sakila:\n    auto_catalog: true\n    auto_procedures: false\n",
        encoding="utf-8",
    )
    assert ff.is_db_feature_enabled("auto_catalog", "sakila") is True
    assert ff.is_db_feature_enabled("auto_procedures", "sakila") is False
    assert ff.is_db_feature_enabled("auto_catalog", "pagila") is False


def test_erp_main_pinned_off_even_if_yaml_says_true(cfg: Path):
    cfg.write_text("db_features:\n  erp_main:\n    auto_catalog: true\n", encoding="utf-8")
    assert ff.is_db_feature_enabled("auto_catalog", "erp_main") is False


def test_unknown_flag_blank_or_none_db_off(cfg: Path):
    cfg.write_text("db_features:\n  sakila:\n    nope: true\n    auto_catalog: true\n", encoding="utf-8")
    assert ff.is_db_feature_enabled("nope", "sakila") is False
    assert ff.is_db_feature_enabled("auto_catalog", "") is False
    assert ff.is_db_feature_enabled("auto_catalog", None) is False


def test_strings_and_numbers_never_enable(cfg: Path):
    cfg.write_text(
        'db_features:\n  sakila:\n    auto_catalog: "false"\n    auto_procedures: 1\n    auto_learning: "true"\n',
        encoding="utf-8",
    )
    for flag in ("auto_catalog", "auto_procedures", "auto_learning"):
        assert ff.is_db_feature_enabled(flag, "sakila") is False


def test_malformed_yaml_everything_off(cfg: Path):
    cfg.write_text("db_features: [unclosed\n", encoding="utf-8")
    assert ff.is_db_feature_enabled("auto_catalog", "sakila") is False


def test_global_flags_unchanged_by_db_block(cfg: Path):
    cfg.write_text(
        "features:\n  fast_path_enabled: true\n  delta_repair_enabled: false\n"
        "db_features:\n  sakila:\n    auto_catalog: true\n",
        encoding="utf-8",
    )
    assert ff.is_feature_enabled("fast_path_enabled") is True
    assert ff.is_feature_enabled("delta_repair_enabled") is False
    assert ff.is_feature_enabled("auto_catalog") is False  # per-DB flags never leak into global lookup
