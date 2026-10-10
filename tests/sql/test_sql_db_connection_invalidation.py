"""Tests for STEP E5: Per-database connection resolution and targeted cache invalidation."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.db_client import run_readonly_query
from src.core.db_settings import _apply_runtime
from src.core.provider_client import ProviderRouter
from src.core.sql_column_registry import ColumnRegistry
from src.models.schemas import Chunk, ChunkType, QueryResult, RetrievedChunk
from src.sql.registry import save_connection
from src.stages.s12b_sql_retrieval import (
    SQLRetriever,
    clear_knowledge_caches,
)
from src.utils.query_classifier import QueryType
from src.utils.semantic_cache import SemanticCache, get_semantic_cache


@pytest.fixture(autouse=True)
def _clean_test_state():
    """Reset caches and test registries cleanly before and after each test."""
    SQLRetriever.clear_result_cache()
    SQLRetriever.clear_schema_cache()
    clear_knowledge_caches()
    SemanticCache.reset()
    yield
    SQLRetriever.clear_result_cache()
    SQLRetriever.clear_schema_cache()
    clear_knowledge_caches()
    SemanticCache.reset()


@pytest.mark.asyncio
async def test_run_readonly_query_resolves_connection_by_db_id(tmp_path: Path, monkeypatch):
    """run_readonly_query resolves custom connection configuration for db_id via registry."""
    # Create two isolated SQLite test databases
    db_file_a = tmp_path / "custom_a.db"
    db_file_b = tmp_path / "custom_b.db"

    with sqlite3.connect(db_file_a) as conn:
        conn.execute("CREATE TABLE kv (key TEXT, val TEXT);")
        conn.execute("INSERT INTO kv VALUES ('db', 'from_database_a');")
        conn.commit()

    with sqlite3.connect(db_file_b) as conn:
        conn.execute("CREATE TABLE kv (key TEXT, val TEXT);")
        conn.execute("INSERT INTO kv VALUES ('db', 'from_database_b');")
        conn.commit()

    # Configure connection registry via temporary connections file
    conn_json = tmp_path / "connections.json"
    monkeypatch.setenv("CONNECTIONS_FILE", str(conn_json))

    save_connection("custom_a", {"engine": "sqlite", "path": str(db_file_a)})
    save_connection("custom_b", {"engine": "sqlite", "path": str(db_file_b)})

    # Execute against custom_a
    rows_a = await run_readonly_query("SELECT val FROM kv", db_id="custom_a")
    assert len(rows_a) == 1
    assert rows_a[0]["val"] == "from_database_a"

    # Execute against custom_b
    rows_b = await run_readonly_query("SELECT val FROM kv", db_id="custom_b")
    assert len(rows_b) == 1
    assert rows_b[0]["val"] == "from_database_b"


@pytest.mark.asyncio
async def test_sql_retriever_resolves_connection_by_db_id(tmp_path: Path, monkeypatch):
    """SQLRetriever resolves its connection and dialect by db_id via registry."""
    db_file_a = tmp_path / "retriever_test.db"
    with sqlite3.connect(db_file_a) as conn:
        conn.execute("CREATE TABLE metrics (id INT, score INT);")
        conn.execute("INSERT INTO metrics VALUES (1, 99);")
        conn.commit()

    conn_json = tmp_path / "connections.json"
    monkeypatch.setenv("CONNECTIONS_FILE", str(conn_json))
    save_connection("db_test_rt", {"engine": "sqlite", "path": str(db_file_a)})

    router = MagicMock(spec=ProviderRouter)
    retriever = SQLRetriever(router, db_id="db_test_rt")
    assert retriever.db_id == "db_test_rt"
    assert retriever._dialect.key == "sqlite"

    # Fetch full schema resolves via the registered db_id
    schema = await retriever._fetch_full_schema()
    assert "metrics" in schema
    assert retriever.column_registry is not None
    assert "metrics" in retriever.column_registry._tables


def test_change_connection_db_a_caches_survive_db_b(tmp_path: Path, monkeypatch):
    """When connection changes for db A, all caches of db B survive and db A are cleared."""
    db_a = "db_alpha"
    db_b = "db_beta"

    # 1. Populate Schema cache and Column Registry
    SQLRetriever._full_schema_cache[db_a] = "CREATE TABLE alpha (id int);"
    SQLRetriever._full_schema_cache[db_b] = "CREATE TABLE beta (id int);"
    SQLRetriever._column_registry[db_a] = ColumnRegistry("CREATE TABLE alpha (id int);", "sqlite")
    SQLRetriever._column_registry[db_b] = ColumnRegistry("CREATE TABLE beta (id int);", "sqlite")

    # 2. Populate Result cache
    now = time.monotonic()
    chunk_a = RetrievedChunk(
        chunk=Chunk(chunk_id="c_a", document_id="doc_a", content="alpha result", chunk_type=ChunkType.TABLE),
        score=1.0,
    )
    chunk_b = RetrievedChunk(
        chunk=Chunk(chunk_id="c_b", document_id="doc_b", content="beta result", chunk_type=ChunkType.TABLE),
        score=1.0,
    )
    SQLRetriever._result_cache[(db_a, "query_shared")] = (now, [chunk_a])
    SQLRetriever._result_cache[(db_b, "query_shared")] = (now, [chunk_b])

    # 3. Populate Semantic cache
    cache = get_semantic_cache()
    emb = [0.05] * 384
    res_a = QueryResult(query="query_shared", answer="Answer Alpha", model_used="test")
    res_b = QueryResult(query="query_shared", answer="Answer Beta", model_used="test")

    cache.store("query_shared", emb, res_a, scope_key="team", query_type=QueryType.SUM, db_id=db_a)
    cache.store("query_shared", emb, res_b, scope_key="team", query_type=QueryType.SUM, db_id=db_b)

    # 4. Populate Knowledge caches
    from src.stages.s12b_sql_retrieval import (
        _BEHAVIORAL_ATLAS_CACHE,
        _LOAD_GLOSSARY_CACHE,
        _RAW_RELATIONSHIPS_CACHE,
    )

    _RAW_RELATIONSHIPS_CACHE[db_a] = [{"from_table": "alpha", "to_table": "ref_a"}]
    _RAW_RELATIONSHIPS_CACHE[db_b] = [{"from_table": "beta", "to_table": "ref_b"}]
    _LOAD_GLOSSARY_CACHE[db_a] = "- concept: alpha term"
    _LOAD_GLOSSARY_CACHE[db_b] = "- concept: beta term"
    _BEHAVIORAL_ATLAS_CACHE[db_a] = {"table": "alpha"}
    _BEHAVIORAL_ATLAS_CACHE[db_b] = {"table": "beta"}

    # Verify initial population
    assert SQLRetriever._full_schema_cache.get(db_a) == "CREATE TABLE alpha (id int);"
    assert SQLRetriever._full_schema_cache.get(db_b) == "CREATE TABLE beta (id int);"
    assert (db_a, "query_shared") in SQLRetriever._result_cache
    assert (db_b, "query_shared") in SQLRetriever._result_cache
    assert cache.lookup("query_shared", emb, scope_key="team", db_id=db_a) is not None
    assert cache.lookup("query_shared", emb, scope_key="team", db_id=db_b) is not None
    assert db_a in _RAW_RELATIONSHIPS_CACHE and db_b in _RAW_RELATIONSHIPS_CACHE

    # --- ACTION: Connection for db_a changes! ---
    _apply_runtime(
        {"engine": "sqlite", "host": "", "port": 0, "database": "new_alpha", "username": "", "password": ""},
        db_id=db_a,
    )

    # --- ASSERTIONS: db_a caches are CLEARED ---
    assert db_a not in SQLRetriever._full_schema_cache
    assert db_a not in SQLRetriever._column_registry
    assert (db_a, "query_shared") not in SQLRetriever._result_cache
    assert cache.lookup("query_shared", emb, scope_key="team", db_id=db_a) is None
    assert db_a not in _RAW_RELATIONSHIPS_CACHE
    assert db_a not in _LOAD_GLOSSARY_CACHE
    assert db_a not in _BEHAVIORAL_ATLAS_CACHE

    # --- ASSERTIONS: db_b caches SURVIVE INTACT ---
    assert SQLRetriever._full_schema_cache.get(db_b) == "CREATE TABLE beta (id int);"
    assert db_b in SQLRetriever._column_registry
    assert "beta" in SQLRetriever._column_registry[db_b]._tables
    assert (db_b, "query_shared") in SQLRetriever._result_cache
    cached_b = SQLRetriever._result_cache[(db_b, "query_shared")]
    assert cached_b[1][0].chunk.content == "beta result"

    hit_b = cache.lookup("query_shared", emb, scope_key="team", db_id=db_b)
    assert hit_b is not None
    assert hit_b.answer == "Answer Beta"

    assert _RAW_RELATIONSHIPS_CACHE.get(db_b) == [{"from_table": "beta", "to_table": "ref_b"}]
    assert _LOAD_GLOSSARY_CACHE.get(db_b) == "- concept: beta term"
    assert _BEHAVIORAL_ATLAS_CACHE.get(db_b) == {"table": "beta"}
