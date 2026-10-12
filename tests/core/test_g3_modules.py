"""Unit tests verifying Step G3 canonical modules."""

from __future__ import annotations

import src.rag.metadata_store as new_ms
import src.sql.learning.failure_capture as new_fc
import src.sql.schema_budget as new_sb
import src.sql.schema_compactor as new_sc
import src.sql.schema_token_estimator as new_ste


def test_g3_canonical_modules_smoke():
    assert callable(new_sc.compact_ddl)
    assert callable(new_sc.extract_join_hints)
    assert new_sc.AUDIT_COLUMNS is not None

    assert callable(new_sb.select_schema_within_budget)
    assert new_sb.DEFAULT_SCHEMA_TOKEN_BUDGET is not None

    assert callable(new_ste.estimate_schema_tokens)

    assert callable(new_fc.capture_sql_failure)
    assert new_fc.DEFAULT_FAILURE_LOG_FILE is not None

    assert callable(new_ms.create_metadata_backend)
    assert callable(new_ms.migrate_registry)
    assert new_ms.JsonMetadataBackend is not None
    assert new_ms.QdrantMetadataBackend is not None


def test_g3_schema_compactor_smoke():
    raw_ddl = "CREATE TABLE orders (id INT PRIMARY KEY, order_num VARCHAR(50), created_at TIMESTAMP);"
    compacted = new_sc.compact_ddl(raw_ddl, dialect="sqlite")
    assert "orders:" in compacted
    assert "id(int, PK)" in compacted
    assert "order_num(varchar)" in compacted
    assert "created_at" not in compacted  # stripped audit column


def test_g3_schema_token_estimator_smoke():
    ddl = "CREATE TABLE test (id INT);"
    tokens = new_ste.estimate_schema_tokens(ddl)
    assert tokens > 0


def test_g3_schema_budget_smoke():
    candidates = [
        {"table_name": "t1", "ddl": "CREATE TABLE t1 (id int);"},
        {"table_name": "t2", "ddl": "CREATE TABLE t2 (id int);"},
    ]
    selected, dropped = new_sb.select_schema_within_budget(candidates, token_budget=1000)
    assert len(selected) == 2
    assert len(dropped) == 0


def test_g3_failure_capture_smoke(tmp_path):
    log_file = tmp_path / "failed.jsonl"
    rec = new_fc.capture_sql_failure(
        query_id="qid_1",
        stage="sql_execution",
        failed_sql="SELECT bad FROM tbl",
        raw_error="no such column: bad",
        file_path=log_file,
    )
    assert rec["query_id"] == "qid_1"
    assert rec["failed_sql"] == "SELECT bad FROM tbl"
    assert log_file.exists()


def test_g3_metadata_store_smoke(tmp_path):
    backend = new_ms.JsonMetadataBackend(tmp_path / "reg.json")
    assert backend.load_all() == {}
    migrated, changed = new_ms.migrate_registry({})
    assert migrated == {}
    assert not changed
