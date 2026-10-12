"""Unit tests for Step G5 module moves and backward-compatibility shims.

Verifies:
1. s01-s11: src/stages/s01-s11 -> src/rag/stages/s01-s11
2. retrieval: src/stages/s12_s13_s14_retrieval.py -> src/rag/retrieval.py
3. ingestion: src/pipeline/ingestion.py -> src/rag/ingestion.py
4. folder_ingestion: src/pipeline/folder_ingestion.py -> src/rag/folder_ingestion.py
5. schema_ingestion: src/pipeline/schema_ingestion.py -> src/sql/schema_retrieval.py
6. Removal of compatibility alias src/pipeline/query_pipeline.py
"""

from __future__ import annotations

import pytest

# 14. Folder Ingestion
import src.rag.folder_ingestion as new_folder_ingestion

# 13. Ingestion Pipeline
import src.rag.ingestion as new_ingestion

# 12. Retrieval
import src.rag.retrieval as new_retrieval

# 1-11. RAG Stages
import src.rag.stages.s01_file_detection as new_s01
import src.rag.stages.s02_classification as new_s02
import src.rag.stages.s03_parsing as new_s03
import src.rag.stages.s04_ocr as new_s04
import src.rag.stages.s05_layout as new_s05
import src.rag.stages.s06_tables as new_s06
import src.rag.stages.s07_s08_visuals as new_s07
import src.rag.stages.s09_chunking as new_s09
import src.rag.stages.s10_embeddings as new_s10
import src.rag.stages.s11_vector_store as new_s11

# 15. Schema Ingestion (merged into sql.schema_retrieval)
import src.sql.schema_retrieval as new_schema_retrieval


def test_query_pipeline_alias_deleted() -> None:
    """src.pipeline.query_pipeline was removed and importing it raises ModuleNotFoundError."""
    with pytest.raises(ModuleNotFoundError):
        import importlib
        importlib.import_module("src.pipeline.query_pipeline")


def test_schema_ingestion_helpers_functional() -> None:
    """Smoke test schema splitting and ID generation helpers."""
    assert new_schema_retrieval.get_schema_document_id("testdb") == "schema:testdb"

    sqlite_rows = [
        {"name": "users", "sql": "CREATE TABLE users (id integer primary key, name text)"},
        {"name": "sqlite_sequence", "sql": "CREATE TABLE sqlite_sequence(name,seq)"},
    ]
    split = new_schema_retrieval._split_sqlite_tables(sqlite_rows)
    assert "users" in split
    assert "sqlite_sequence" not in split
    assert "CREATE TABLE users" in split["users"]

    mysql_rows = [
        {"table_name": "orders", "column_name": "id", "data_type": "int", "column_comment": ""},
        {"table_name": "orders", "column_name": "total", "data_type": "decimal(10,2)", "column_comment": "amount"},
    ]
    mysql_split = new_schema_retrieval._split_mysql_tables(mysql_rows)
    assert "orders" in mysql_split
    assert "TABLE orders (" in mysql_split["orders"]
    assert "total decimal(10,2)  -- amount" in mysql_split["orders"]


def test_ingestion_lock_for_hash() -> None:
    """Lock helper returns the same lock for identical hash."""
    lock1 = new_ingestion._lock_for_hash("hash123")
    lock2 = new_ingestion._lock_for_hash("hash123")
    lock3 = new_ingestion._lock_for_hash("other")
    assert lock1 is lock2
    assert lock1 is not lock3
