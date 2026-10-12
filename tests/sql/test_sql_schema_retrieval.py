"""Unit and parity tests for schema retrieval extracted into src.sql.schema_retrieval."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.schemas import Chunk, ChunkType, RetrievedChunk
from src.sql.knowledge.loaders import DEFAULT_DB_ID
from src.sql.schema_retrieval import (
    _build_scoped_schema_fallback,
    _extract_schema_table_names,
    _extract_table_ddl_map,
    _format_scoped_relationships,
    _get_1hop_neighbors,
    _retrieve_schema_from_qdrant,
    build_scoped_schema_fallback,
    extract_schema_table_names,
    extract_table_ddl_map,
    format_scoped_relationships,
    get_1hop_neighbors,
    retrieve_schema_from_qdrant,
)



SAMPLE_DDL = """
CREATE TABLE sales_order (
    id INTEGER PRIMARY KEY,
    party_id INTEGER,
    amount DECIMAL(10, 2),
    status VARCHAR(50),
    created_id INTEGER
);

CREATE TABLE party (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255),
    is_active BOOLEAN,
    created_id INTEGER
);

CREATE TABLE product (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255),
    price DECIMAL(10, 2)
);

CREATE TABLE warehouse (
    id INTEGER PRIMARY KEY,
    location VARCHAR(255)
);

CREATE TABLE stock_entry (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER,
    product_id INTEGER,
    qty INTEGER
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username VARCHAR(100)
);
"""


def test_extract_schema_table_names():
    """Verify table name extraction handles formatting and case variations."""
    names = extract_schema_table_names(SAMPLE_DDL)
    expected = {"sales_order", "party", "product", "warehouse", "stock_entry", "users"}
    assert names == expected

    # Empty and invalid DDL
    assert extract_schema_table_names("") == set()
    assert extract_schema_table_names("SELECT * FROM foo") == set()


def test_extract_table_ddl_map():
    """Verify table DDL map extracts exact per-table DDL blocks."""
    ddls = extract_table_ddl_map(SAMPLE_DDL)
    assert set(ddls.keys()) == {"sales_order", "party", "product", "warehouse", "stock_entry", "users"}
    assert "CREATE TABLE sales_order" in ddls["sales_order"]
    assert "CREATE TABLE party" in ddls["party"]


def test_get_1hop_neighbors():
    """Verify 1-hop graph expansion finds connected tables and filters audit users noise."""
    sample_rels = [
        {"from_table": "sales_order", "from_column": "party_id", "to_table": "party", "to_column": "id"},
        {"from_table": "sales_order", "from_column": "created_id", "to_table": "users", "to_column": "id"},
        {"from_table": "stock_entry", "from_column": "product_id", "to_table": "product", "to_column": "id"},
        {"from_table": "stock_entry", "from_column": "warehouse_id", "to_table": "warehouse", "to_column": "id"},
    ]

    neighbors = get_1hop_neighbors({"sales_order"}, rels=sample_rels)
    assert "party" in neighbors
    assert "users" not in neighbors  # Filtered audit noise

    stock_neighbors = get_1hop_neighbors({"stock_entry"}, rels=sample_rels)
    assert "product" in stock_neighbors
    assert "warehouse" in stock_neighbors


def test_format_scoped_relationships():
    """Verify relationship formatting filters active tables and respects audit query intent."""
    sample_rels = [
        {"from_table": "sales_order", "from_column": "party_id", "to_table": "party", "to_column": "id"},
        {"from_table": "sales_order", "from_column": "created_id", "to_table": "users", "to_column": "id"},
        {"from_table": "stock_entry", "from_column": "product_id", "to_table": "product", "to_column": "id"},
    ]

    # Non-audit query: users edge omitted
    formatted = format_scoped_relationships({"sales_order", "party", "users"}, query="top customer sales", rels=sample_rels)
    assert "sales_order: party_id->party.id" in formatted
    assert "users" not in formatted

    # Explicit audit query: users edge included
    audit_formatted = format_scoped_relationships({"sales_order", "party", "users"}, query="creator of this order", rels=sample_rels)
    assert "sales_order: party_id->party.id, created_id->users.id" in audit_formatted


REPRESENTATIVE_QUERIES = [
    "Show all customer names and orders",
    "List warehouse inventory stock by location",
    "Find total sales by customer this month",
    "What products are currently low on stock?",
    "Show party contact details and balance",
    "Pending invoices and payments for customer",
    "Top selling products by revenue",
    "Active users and permissions",
    "Count of purchases per vendor",
    "Show delivery challan records",
]


def test_golden_fallback_schema_generation():
    """Verify scoped schema fallback successfully builds schemas across representative queries."""
    for query in REPRESENTATIVE_QUERIES:
        direct_out = build_scoped_schema_fallback(SAMPLE_DDL, query, db_id=DEFAULT_DB_ID)
        assert direct_out != "", f"Empty schema generated for query: {query}"


@pytest.mark.asyncio
async def test_retrieve_schema_from_qdrant_fallback():
    """Verify fallback when vector store or embeddings is not provided or fails."""
    # When vector store / embeddings are None
    res = await retrieve_schema_from_qdrant(
        query="List customers",
        full_schema=SAMPLE_DDL,
        vector_store=None,
        embeddings=None,
        db_id=DEFAULT_DB_ID,
    )
    assert res != ""
    assert "party" in res or "party:" in res

    # When search_hybrid returns 0 chunks
    mock_store = MagicMock()
    mock_store.search_hybrid = AsyncMock(return_value=[])
    mock_embed = MagicMock()
    mock_embed.embed_query = AsyncMock(return_value=([0.1, 0.2], [0.3, 0.4]))

    res_zero = await retrieve_schema_from_qdrant(
        query="List customers",
        full_schema=SAMPLE_DDL,
        vector_store=mock_store,
        embeddings=mock_embed,
        db_id=DEFAULT_DB_ID,
    )
    assert res_zero != ""
    assert "party" in res_zero or "party:" in res_zero


@pytest.mark.asyncio
async def test_retrieve_schema_from_qdrant_isolation():
    """Verify db_id isolation filtering: foreign database chunks are excluded."""
    erp_chunk = MagicMock(spec=RetrievedChunk)
    erp_chunk.chunk = Chunk(
        chunk_id="chunk_erp_1",
        content="CREATE TABLE sales_order (id INTEGER PRIMARY KEY, total REAL);",
        document_id="schema:erp_main",
        chunk_type=ChunkType.SQL_SCHEMA,
        metadata={"db_id": "erp_main"},
    )

    crm_chunk = MagicMock(spec=RetrievedChunk)
    crm_chunk.chunk = Chunk(
        chunk_id="chunk_crm_1",
        content="CREATE TABLE crm_leads (id INTEGER PRIMARY KEY, name TEXT);",
        document_id="schema:crm_db",
        chunk_type=ChunkType.SQL_SCHEMA,
        metadata={"db_id": "crm_db"},
    )


    mock_store = MagicMock()
    # Mock vector store returning chunks from both DBs
    mock_store.search_hybrid = AsyncMock(return_value=[erp_chunk, crm_chunk])
    mock_embed = MagicMock()
    mock_embed.embed_query = AsyncMock(return_value=([0.1], [0.2]))

    # When querying erp_main, crm_leads chunk must be filtered out
    schema_erp = await retrieve_schema_from_qdrant(
        query="show orders",
        full_schema=SAMPLE_DDL,
        vector_store=mock_store,
        embeddings=mock_embed,
        db_id="erp_main",
    )
    assert "crm_leads" not in schema_erp
    assert "sales_order" in schema_erp or "sales_order:" in schema_erp
