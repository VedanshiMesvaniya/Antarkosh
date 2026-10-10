"""Unit tests for Step G6: Learned data per database with directory auto-creation and fallback."""

import json
from pathlib import Path

import pytest

import src.core.pattern_learner as shim_pl
import src.core.schema_monitor as shim_sm
import src.sql.learning.failure_capture as learn_fc
import src.sql.learning.pattern_learner as learn_pl
import src.sql.learning.schema_monitor as learn_sm
import src.utils.failure_capture as shim_fc
from src.sql.knowledge.loaders import (
    get_learned_path,
    resolve_learned_read_path,
)
from src.sql.learning.failure_capture import capture_sql_failure
from src.sql.learning.pattern_learner import PatternLearner
from src.sql.learning.schema_monitor import SchemaDrift, SchemaMonitor



def test_g6_shims_and_constants_identity():
    """Verify shims preserve object identity and path constants."""
    assert shim_pl.PatternLearner is learn_pl.PatternLearner
    assert shim_pl.LearnedPattern is learn_pl.LearnedPattern
    assert shim_pl.LEARNED_PATTERNS_PATH == learn_pl.LEARNED_PATTERNS_PATH
    assert shim_pl.LEARNING_METRICS_PATH == learn_pl.LEARNING_METRICS_PATH

    assert shim_sm.SchemaMonitor is learn_sm.SchemaMonitor
    assert shim_sm.SchemaDrift is learn_sm.SchemaDrift
    assert shim_sm.SCHEMA_ATLAS_PATH == learn_sm.SCHEMA_ATLAS_PATH
    assert shim_sm.DRIFT_LOG_PATH == learn_sm.DRIFT_LOG_PATH

    assert shim_fc.capture_sql_failure is learn_fc.capture_sql_failure
    assert shim_fc.DEFAULT_FAILURE_LOG_FILE == learn_fc.DEFAULT_FAILURE_LOG_FILE


def test_get_learned_path_creates_folder_on_write(tmp_path):
    """get_learned_path must create the learned/ folder when create_dir=True."""
    db_id = "test_analytics"
    learned_dir = tmp_path / db_id / "learned"
    assert not learned_dir.exists()

    # create_dir=False does not create directory
    target = get_learned_path("learned_patterns.jsonl", db_id=db_id, create_dir=False, databases_dir=tmp_path)
    assert not learned_dir.exists()
    assert target == learned_dir / "learned_patterns.jsonl"

    # create_dir=True creates the directory
    write_target = get_learned_path("learned_patterns.jsonl", db_id=db_id, create_dir=True, databases_dir=tmp_path)
    assert learned_dir.exists()
    assert write_target == learned_dir / "learned_patterns.jsonl"


def test_get_learned_path_validation(tmp_path):
    """get_learned_path must reject invalid db_id and path traversal."""
    with pytest.raises(ValueError, match="Invalid db_id"):
        get_learned_path("file.json", db_id="123invalid", databases_dir=tmp_path)

    with pytest.raises(ValueError, match="Invalid filename"):
        get_learned_path("../escape.json", db_id="erp_main", databases_dir=tmp_path)


def test_resolve_learned_read_path_fallback(tmp_path):
    """resolve_learned_read_path must fallback to global legacy path if db file absent."""
    db_id = "erp_main"
    legacy_file = tmp_path / "global_legacy.jsonl"
    legacy_file.write_text('{"legacy": true}\n', encoding="utf-8")

    # DB-specific file absent -> returns fallback
    resolved = resolve_learned_read_path(
        "learned_patterns.jsonl",
        fallback_path=legacy_file,
        db_id=db_id,
        databases_dir=tmp_path / "dbs",
    )
    assert resolved == legacy_file

    # Once DB-specific file is written, it takes precedence
    db_learned_file = tmp_path / "dbs" / db_id / "learned" / "learned_patterns.jsonl"
    db_learned_file.parent.mkdir(parents=True, exist_ok=True)
    db_learned_file.write_text('{"db_specific": true}\n', encoding="utf-8")

    resolved_db = resolve_learned_read_path(
        "learned_patterns.jsonl",
        fallback_path=legacy_file,
        db_id=db_id,
        databases_dir=tmp_path / "dbs",
    )
    assert resolved_db == db_learned_file


def test_pattern_learner_per_database_and_fallback(tmp_path, monkeypatch):
    """PatternLearner reads fallback if absent and writes to databases/<db_id>/learned/."""
    db_id = "test_crm"
    fake_databases = tmp_path / "databases"
    monkeypatch.setattr(learn_pl, "get_learned_path", lambda f, db_id=db_id, create_dir=True: get_learned_path(f, db_id=db_id, create_dir=create_dir, databases_dir=fake_databases))
    monkeypatch.setattr(learn_pl, "resolve_learned_read_path", lambda f, fb, db_id=db_id: resolve_learned_read_path(f, fb, db_id=db_id, databases_dir=fake_databases))

    # Create legacy fallback file
    legacy_patterns = tmp_path / "legacy_patterns.jsonl"
    legacy_pattern_data = {
        "pattern_id": "legacy_pat_1",
        "business_scenario": "Legacy pattern test",
        "trigger_phrases": ["test"],
        "cot_reasoning_snippet": "Reasoning snippet.",
        "sql_structure_template": "SELECT * FROM {table}",
        "source_question": "What is total?",
        "original_error": "SyntaxError",
        "fixed_sql": "SELECT 1",
        "created_at": "2026-01-01T00:00:00",
        "quality_score": 0.9,
    }
    legacy_patterns.write_text(json.dumps(legacy_pattern_data) + "\n", encoding="utf-8")
    monkeypatch.setattr(learn_pl, "LEARNED_PATTERNS_PATH", str(legacy_patterns))

    # Initialize learner: should load legacy pattern
    learner = PatternLearner(db_id=db_id)
    assert len(learner.learned_patterns) == 1
    assert learner.learned_patterns[0].pattern_id == "legacy_pat_1"

    # Capture a new success: should write to databases/<db_id>/learned/
    target_learned_dir = fake_databases / db_id / "learned"
    assert not target_learned_dir.exists()

    result = learner.capture_success(
        user_question="Show sales orders total",
        original_cot="Need to cast numeric",
        failed_sql="SELECT sales_order, total FROM orders",
        error_message="operator does not exist: numeric = character varying (cast required)",
        fixed_sql="SELECT sales_order, CAST(total AS NUMERIC) FROM orders",
        revised_cot="Because total is varchar, need to cast to numeric.",
    )

    assert result is not None
    # Directory was auto-created on write!
    assert target_learned_dir.exists()
    db_patterns_file = target_learned_dir / "learned_patterns.jsonl"
    db_metrics_file = target_learned_dir / "learning_metrics.json"
    assert db_patterns_file.exists()
    assert db_metrics_file.exists()

    # Verify static pattern library was NOT created
    assert not Path("config/sql_pattern_library.json").exists()


def test_schema_monitor_drift_log_per_database(tmp_path, monkeypatch):
    """SchemaMonitor writes drift log to databases/<db_id>/learned/schema_drift_log.jsonl."""
    db_id = "test_inventory"
    fake_databases = tmp_path / "databases"
    monkeypatch.setattr(learn_sm, "get_learned_path", lambda f, db_id=db_id, create_dir=True: get_learned_path(f, db_id=db_id, create_dir=create_dir, databases_dir=fake_databases))
    monkeypatch.setattr(learn_sm, "resolve_learned_read_path", lambda f, fb, db_id=db_id: resolve_learned_read_path(f, fb, db_id=db_id, databases_dir=fake_databases))

    class DummyClient:
        pass

    target_learned_dir = fake_databases / db_id / "learned"
    assert not target_learned_dir.exists()

    monitor = SchemaMonitor(db_client=DummyClient(), db_id=db_id)
    drift = SchemaDrift(
        drift_id="drift_123",
        drift_type="new_column",
        table_name="products",
        column_name="is_active",
        expected_value=None,
        actual_value={"type": "BOOLEAN"},
        severity="info",
        detected_at="2026-10-10T00:00:00",
    )
    monitor._log_drift(drift)

    # Directory created on write
    assert target_learned_dir.exists()
    drift_file = target_learned_dir / "schema_drift_log.jsonl"
    assert drift_file.exists()
    lines = drift_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    logged_data = json.loads(lines[0])
    assert logged_data["drift_id"] == "drift_123"

    # Schema atlas was NOT created
    assert not Path("config/schema_atlas.json").exists()


def test_failure_capture_per_database_with_folder_creation(tmp_path, monkeypatch):
    """capture_sql_failure writes to databases/<db_id>/learned/failed_queries.jsonl with auto-creation."""
    db_id = "test_billing"
    fake_databases = tmp_path / "databases"
    monkeypatch.setattr(learn_fc, "get_learned_path", lambda f, db_id=db_id, create_dir=True: get_learned_path(f, db_id=db_id, create_dir=create_dir, databases_dir=fake_databases))

    target_learned_dir = fake_databases / db_id / "learned"
    assert not target_learned_dir.exists()

    rec = capture_sql_failure(
        query_id="q-g6-test-01",
        stage="sql_execution",
        failed_sql="SELECT * FROM missing_tbl",
        raw_error="no such table: missing_tbl",
        db_id=db_id,
    )

    assert rec["query_id"] == "q-g6-test-01"
    assert rec["db_id"] == db_id

    # Folder was created on write!
    assert target_learned_dir.exists()
    failure_file = target_learned_dir / "failed_queries.jsonl"
    assert failure_file.exists()
    content = failure_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    disk_rec = json.loads(content[0])
    assert disk_rec["query_id"] == "q-g6-test-01"
    assert disk_rec["db_id"] == db_id


def test_get_learned_path_with_learned_data_dir_override(tmp_path, monkeypatch):
    """get_learned_path respects LEARNED_DATA_DIR environment variable for persistent storage."""
    persistent_root = tmp_path / "persistent_learned"
    monkeypatch.setenv("LEARNED_DATA_DIR", str(persistent_root))

    target = get_learned_path("patterns.jsonl", db_id="erp_main", create_dir=True)
    assert target == persistent_root / "erp_main" / "patterns.jsonl"
    assert (persistent_root / "erp_main").exists()

