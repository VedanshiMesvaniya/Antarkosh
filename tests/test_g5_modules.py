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

import src.pipeline.folder_ingestion as shim_folder_ingestion
import src.pipeline.ingestion as shim_ingestion
import src.pipeline.schema_ingestion as shim_schema_ingestion

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
import src.stages.s01_file_detection as shim_s01
import src.stages.s02_classification as shim_s02
import src.stages.s03_parsing as shim_s03
import src.stages.s04_ocr as shim_s04
import src.stages.s05_layout as shim_s05
import src.stages.s06_tables as shim_s06
import src.stages.s07_s08_visuals as shim_s07
import src.stages.s09_chunking as shim_s09
import src.stages.s10_embeddings as shim_s10
import src.stages.s11_vector_store as shim_s11
import src.stages.s12_s13_s14_retrieval as shim_retrieval


def test_stages_s01_to_s11_shims_object_identity() -> None:
    """All public functions and classes in stages shims match src.rag.stages."""
    assert shim_s01.detect_file is new_s01.detect_file

    assert shim_s02.classify_semantic is new_s02.classify_semantic
    assert shim_s02.classify_structure is new_s02.classify_structure

    assert shim_s03.parse_document is new_s03.parse_document

    assert shim_s04.run_ocr is new_s04.run_ocr

    assert shim_s05.analyze_layout is new_s05.analyze_layout

    assert shim_s06.extract_tables is new_s06.extract_tables

    assert shim_s07.analyze_visuals is new_s07.analyze_visuals

    assert shim_s09.chunk_document is new_s09.chunk_document

    assert shim_s10.EmbeddingService is new_s10.EmbeddingService

    assert shim_s11.QdrantStore is new_s11.QdrantStore


def test_retrieval_shim_object_identity() -> None:
    """Symbols in src.stages.s12_s13_s14_retrieval match src.rag.retrieval."""
    assert shim_retrieval.Retriever is new_retrieval.Retriever
    assert shim_retrieval.Generator is new_retrieval.Generator
    assert shim_retrieval.Reranker is new_retrieval.Reranker
    assert shim_retrieval._enforce_document_diversity is new_retrieval._enforce_document_diversity
    assert shim_retrieval._is_exhaustive_query is new_retrieval._is_exhaustive_query
    assert shim_retrieval._source_mode is new_retrieval._source_mode


def test_ingestion_shim_object_identity() -> None:
    """Symbols in src.pipeline.ingestion match src.rag.ingestion."""
    assert shim_ingestion.IngestionPipeline is new_ingestion.IngestionPipeline
    assert shim_ingestion.IngestionResult is new_ingestion.IngestionResult
    assert shim_ingestion._INGEST_LOCKS is new_ingestion._INGEST_LOCKS
    assert shim_ingestion.reconcile_active_flags is new_ingestion.reconcile_active_flags
    assert shim_ingestion._lock_for_hash is new_ingestion._lock_for_hash
    assert shim_ingestion._discard_redundant_upload is new_ingestion._discard_redundant_upload


def test_folder_ingestion_shim_object_identity() -> None:
    """Symbols in src.pipeline.folder_ingestion match src.rag.folder_ingestion."""
    assert shim_folder_ingestion.FolderIngestionResult is new_folder_ingestion.FolderIngestionResult
    assert shim_folder_ingestion.scan_and_ingest is new_folder_ingestion.scan_and_ingest
    assert shim_folder_ingestion.run_periodic_scan is new_folder_ingestion.run_periodic_scan
    assert shim_folder_ingestion._discover_files is new_folder_ingestion._discover_files


def test_schema_ingestion_shim_object_identity() -> None:
    """Symbols in src.pipeline.schema_ingestion match src.sql.schema_retrieval."""
    assert shim_schema_ingestion.sync_live_schema is new_schema_retrieval.sync_live_schema
    assert shim_schema_ingestion.SCHEMA_DOCUMENT_ID == new_schema_retrieval.SCHEMA_DOCUMENT_ID
    assert shim_schema_ingestion.get_schema_document_id is new_schema_retrieval.get_schema_document_id
    assert shim_schema_ingestion._split_schema_by_table is new_schema_retrieval._split_schema_by_table
    assert shim_schema_ingestion._split_mysql_tables is new_schema_retrieval._split_mysql_tables
    assert shim_schema_ingestion._split_sqlite_tables is new_schema_retrieval._split_sqlite_tables
    assert shim_schema_ingestion.run_readonly_query is new_schema_retrieval.run_readonly_query


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
