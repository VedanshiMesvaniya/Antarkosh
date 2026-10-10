"""Unit tests verifying Step G3 module migrations and shim parity."""

from __future__ import annotations

import pytest

import src.utils.schema_compactor as shim_sc
import src.sql.schema_compactor as new_sc

import src.utils.schema_budget as shim_sb
import src.sql.schema_budget as new_sb

import src.utils.schema_token_estimator as shim_ste
import src.sql.schema_token_estimator as new_ste

import src.utils.failure_capture as shim_fc
import src.sql.learning.failure_capture as new_fc

import src.core.metadata_store as shim_ms
import src.rag.metadata_store as new_ms


def test_g3_shims_object_identity():
    # 1. schema_compactor
    assert shim_sc.compact_ddl is new_sc.compact_ddl
    assert shim_sc.extract_join_hints is new_sc.extract_join_hints
    assert shim_sc.AUDIT_COLUMNS == new_sc.AUDIT_COLUMNS

    # 2. schema_budget
    assert shim_sb.select_schema_within_budget is new_sb.select_schema_within_budget
    assert shim_sb.DEFAULT_SCHEMA_TOKEN_BUDGET == new_sb.DEFAULT_SCHEMA_TOKEN_BUDGET

    # 3. schema_token_estimator
    assert shim_ste.estimate_schema_tokens is new_ste.estimate_schema_tokens

    # 4. failure_capture
    assert shim_fc.capture_sql_failure is new_fc.capture_sql_failure
    assert shim_fc.DEFAULT_FAILURE_LOG_FILE == new_fc.DEFAULT_FAILURE_LOG_FILE

    # 5. metadata_store
    assert shim_ms.create_metadata_backend is new_ms.create_metadata_backend
    assert shim_ms.migrate_registry is new_ms.migrate_registry
    assert shim_ms.JsonMetadataBackend is new_ms.JsonMetadataBackend
    assert shim_ms.QdrantMetadataBackend is new_ms.QdrantMetadataBackend


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
    candidates = [{"table_name": "t1", "ddl": "CREATE TABLE t1 (id int);"},
                  {"table_name": "t2", "ddl": "CREATE TABLE t2 (id int);"}]
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
