"""Integration tests for the Query Pipeline."""

import pytest
from unittest.mock import AsyncMock

from src.models.schemas import QueryResult, Chunk, RetrievedChunk, ChunkType, DocumentType, TokenUsage
from src.pipeline.query import QueryPipeline, _is_document_listing_query
from src.rag.stages.s10_embeddings import SparseVector


@pytest.mark.parametrize(
    "question",
    [
        "what files do you have",
        "list all documents",
        "how many documents do you have",
        "what's in your knowledge base",
        "list docs",
    ],
)
def test_document_listing_true(question):
    assert _is_document_listing_query(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # Business-entity scoped → live-data question, NOT a corpus listing.
        "how many files did customer Acme upload",
        "list all orders by revenue",
        "how many invoices does each customer have",
        "show documents attached to order 42",
        # No listing phrase at all.
        "what is the total revenue in 2025",
    ],
)
def test_document_listing_false_for_data_questions(question):
    assert _is_document_listing_query(question) is False


@pytest.fixture
def mock_router():
    router = AsyncMock()
    
    async def mock_chat(*args, **kwargs):
        return "This is a mock answer based on the context."

    async def mock_chat_stream(*args, **kwargs):
        yield "This "
        yield "is a mock "
        yield "stream answer."

    router.chat = mock_chat
    router.chat_stream = mock_chat_stream
    # The generator reads router.last_used for QueryResult.model_used; the real
    # router sets this to a "provider/model" string once a call completes.
    router.last_used = "mock/model"
    # The generator also reads router.usage (a real TokenUsage the router
    # accumulates per request) and copies it onto the QueryResult.
    router.usage = TokenUsage(input_tokens=120, output_tokens=45, calls=1, provider="mock", model="model")
    return router


@pytest.fixture
def mock_store():
    store = AsyncMock()
    return store


@pytest.fixture
def mock_embeddings():
    service = AsyncMock()
    
    async def mock_embed_queries(queries):
        return [[0.1] * 384 for _ in queries]
        
    async def mock_embed_query(query):
        return ([0.1] * 384, SparseVector())
        
    service.embed_queries = mock_embed_queries
    service.embed_query = mock_embed_query
    return service


@pytest.mark.asyncio
async def test_query_pipeline_empty_retrieval(mock_router, mock_store, mock_embeddings):
    # Setup mock to return no chunks
    mock_store.search_hybrid = AsyncMock(return_value=[])
    
    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings
    )
    
    result = await pipeline.query("What is the capital of France?")
    
    assert isinstance(result, QueryResult)
    assert result.query == "What is the capital of France?"
    assert "No relevant documents found" in result.answer
    assert result.chunks_retrieved == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["", "   ", "\n\t"])
async def test_query_pipeline_empty_or_whitespace_input(
    question, mock_router, mock_store, mock_embeddings
):
    """Empty / whitespace-only input must not crash — no source matches, so the
    pipeline returns the graceful no-results message."""
    mock_store.search_hybrid = AsyncMock(return_value=[])
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    # SQL stage abstains (no DB in this unit test).
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])

    result = await pipeline.query(question)

    assert isinstance(result, QueryResult)
    assert "No relevant documents found" in result.answer


@pytest.mark.asyncio
async def test_sql_provider_outage_reports_unavailable_not_missing(
    mock_router, mock_store, mock_embeddings
):
    """When the SQL path found nothing only because its models were unreachable
    (not because the data is missing), the answer must say so — not the
    misleading 'no documents' message."""
    from src.pipeline.query import _SQL_UNAVAILABLE_MSG

    mock_store.search_hybrid = AsyncMock(return_value=[])  # no documents either
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    pipeline._sql_retriever = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])
    pipeline._sql_retriever.last_infra_error = "All providers exhausted for task 'reasoning'"

    result = await pipeline.query("total revenue in 2025")

    assert result.answer == _SQL_UNAVAILABLE_MSG
    assert result.reasoning_task == "sql_unavailable"


@pytest.mark.asyncio
async def test_no_infra_error_keeps_normal_no_results_message(
    mock_router, mock_store, mock_embeddings
):
    """A clean 'nothing found' (SQL abstained, no docs) keeps the ordinary
    message — the outage wording only appears on a real provider outage."""
    mock_store.search_hybrid = AsyncMock(return_value=[])
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    pipeline._sql_retriever = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])
    pipeline._sql_retriever.last_infra_error = None

    result = await pipeline.query("something with no answer anywhere")

    assert "No relevant documents found" in result.answer
    assert result.reasoning_task == "no_results"


@pytest.mark.asyncio
async def test_sql_outage_streaming_reports_unavailable(
    mock_router, mock_store, mock_embeddings
):
    from src.pipeline.query import _SQL_UNAVAILABLE_MSG

    mock_store.search_hybrid = AsyncMock(return_value=[])
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    pipeline._sql_retriever = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])
    pipeline._sql_retriever.last_infra_error = "All providers exhausted for task 'reasoning'"

    chunks = [c async for c in pipeline.query_stream("total revenue in 2025")]
    final = chunks[-1]
    assert final.reasoning_task == "sql_unavailable"
    assert final.answer == _SQL_UNAVAILABLE_MSG


@pytest.mark.asyncio
async def test_query_pipeline_success(mock_router, mock_store, mock_embeddings):
    # Setup mock to return some chunks
    mock_chunk = Chunk(
        chunk_id="chunk-1",
        document_id="doc-1",
        content="Paris is the capital of France.",
        chunk_type=ChunkType.PROSE,
        page_number=1,
        document_type=DocumentType.GENERAL,
        source_file="test.txt"
    )
    retrieved_chunk = RetrievedChunk(chunk=mock_chunk, score=0.9)
    mock_store.search_hybrid = AsyncMock(return_value=[retrieved_chunk])
    
    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings
    )
    
    result = await pipeline.query("What is the capital of France?")
    
    assert isinstance(result, QueryResult)
    assert result.query == "What is the capital of France?"
    assert "This is a mock answer" in result.answer
    assert result.chunks_retrieved == 1
    assert result.chunks_after_rerank == 1


@pytest.mark.asyncio
async def test_query_pipeline_stream_success(mock_router, mock_store, mock_embeddings):
    # Setup mock to return some chunks
    mock_chunk = Chunk(
        chunk_id="chunk-1",
        document_id="doc-1",
        content="Paris is the capital of France.",
        chunk_type=ChunkType.PROSE,
        page_number=1,
        document_type=DocumentType.GENERAL,
        source_file="test.txt"
    )
    retrieved_chunk = RetrievedChunk(chunk=mock_chunk, score=0.9)
    mock_store.search_hybrid = AsyncMock(return_value=[retrieved_chunk])
    
    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings
    )
    
    chunks = []
    async for chunk in pipeline.query_stream("What is the capital of France?"):
        chunks.append(chunk)
        
    assert len(chunks) > 0
    final_result = chunks[-1]
    
    assert isinstance(final_result, QueryResult)
    assert final_result.query == "What is the capital of France?"
    assert "stream answer" in final_result.answer
    assert final_result.chunks_retrieved == 1
    # Token usage from the router is copied onto the result and serializes with
    # a computed total, so the UI can show a breakdown per answer.
    assert final_result.usage.total_tokens == 165
    assert final_result.usage.model_dump()["total_tokens"] == 165


def _sql_table_md() -> str:
    return (
        "SQL Query Executed: `SELECT model, units FROM gpu_sales`\n\n"
        "| model | units |\n"
        "| --- | --- |\n"
        "| A100 | 12 |"
    )


def _sql_retrieved() -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id="live_sql_001",
            document_id="live_db",
            content=_sql_table_md(),
            chunk_type=ChunkType.SQL_RESULT,
            page_number=0,
            document_type=DocumentType.GENERAL,
            source_file="live_database (gpu_sales table)",
        ),
        score=1.0,
        retrieval_method="text-to-sql",
    )


def _doc_retrieved() -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            content="Paris is the capital of France.",
            chunk_type=ChunkType.PROSE,
            page_number=1,
            document_type=DocumentType.GENERAL,
            source_file="test.txt",
        ),
        score=0.9,
    )


@pytest.mark.asyncio
async def test_sql_plus_document_blends_prose_and_appends_exact_table(
    mock_router, mock_store, mock_embeddings
):
    """When BOTH a SQL result and a document chunk are retrieved, the answer is
    synthesized by the LLM (document context) AND the exact SQL table is appended
    verbatim — the SQL result must never silently drop to a document-only answer,
    nor short-circuit the documents. (Regression: the pipeline previously
    returned the SQL table alone via a dead intent classifier.)"""
    sql_table = _sql_table_md()
    sql_retrieved = _sql_retrieved()
    vector_retrieved = _doc_retrieved()

    mock_router.chat = AsyncMock(return_value="This is a mock answer based on the context.")
    mock_store.search_hybrid = AsyncMock(return_value=[vector_retrieved])

    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings,
    )
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[sql_retrieved])
    pipeline._reranker.rerank = AsyncMock(return_value=[vector_retrieved])

    result = await pipeline.query("List all warehouses and explain the return policy document")

    assert isinstance(result, QueryResult)
    # Both sources present: LLM prose from the document, exact SQL table appended.
    assert "This is a mock answer" in result.answer
    assert sql_table in result.answer
    # The blend path runs the LLM (not the raw sql/direct short-circuit).
    assert result.model_used == "mock/model"
    assert mock_router.chat.await_count >= 1

    # Ensure live database results are strictly kept private and NEVER passed to the LLM prompt
    call_kwargs = mock_router.chat.call_args.kwargs
    messages = call_kwargs.get("messages", [])
    prompt_content = " ".join(m.get("content", "") for m in messages)
    assert sql_table not in prompt_content
    assert "Live Database Results:" not in prompt_content
    assert "Alice" not in prompt_content
    assert "Bob" not in prompt_content


@pytest.mark.asyncio
async def test_sql_plus_document_blends_streaming_keeps_sql_private(
    mock_router, mock_store, mock_embeddings
):
    sql_table = _sql_table_md()
    sql_retrieved = _sql_retrieved()
    vector_retrieved = _doc_retrieved()

    async def _mock_stream(*args, **kwargs):
        messages = kwargs.get("messages", [])
        prompt_content = " ".join(m.get("content", "") for m in messages)
        assert sql_table not in prompt_content
        assert "Alice" not in prompt_content
        yield "Streaming answer for document context."

    mock_router.chat_stream = _mock_stream
    mock_store.search_hybrid = AsyncMock(return_value=[vector_retrieved])

    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings,
    )
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[sql_retrieved])
    pipeline._reranker.rerank = AsyncMock(return_value=[vector_retrieved])

    chunks = []
    async for chunk in pipeline.query_stream("List all warehouses and explain the return policy document"):
        chunks.append(chunk)

    final_result = [c for c in chunks if isinstance(c, QueryResult)][0]
    assert "Streaming answer for document context." in final_result.answer
    assert sql_table in final_result.answer
    assert final_result.sql_payload is not None


# ---- one authoritative database result -------------------------------------------------------------------------------------------------


_FAKE_TABLE = "| model | units |\n| --- | --- |\n| H100 | 999 |"  # what a model could invent from a document


def _pipeline(mock_router, mock_store, mock_embeddings, *, sql=True, docs=True) -> QueryPipeline:
    mock_store.search_hybrid = AsyncMock(return_value=[_doc_retrieved()] if docs else [])
    pipeline = QueryPipeline(router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings)
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[_sql_retrieved()] if sql else [])
    pipeline._reranker.rerank = AsyncMock(return_value=[_doc_retrieved()])
    return pipeline


@pytest.mark.asyncio
async def test_a_database_question_with_incidental_documents_shows_one_database_result(mock_router, mock_store, mock_embeddings):
    """The query succeeded and the question names no document: the live rows are the one result (sql_payload). Document chunks that
    happened to come back are neither reranked nor synthesized, so no second table can appear."""
    mock_router.chat = AsyncMock(return_value=_FAKE_TABLE)
    pipeline = _pipeline(mock_router, mock_store, mock_embeddings)

    result = await pipeline.query("Show me the live database results")

    assert result.answer == "Found 1 matching live record."
    assert "|" not in result.answer and "H100" not in result.answer  # no table in the text, and nothing invented
    assert result.sql_payload is not None and result.sql_payload["rows"] == [{"model": "A100", "units": "12"}]
    assert result.model_used == "sql/direct" and result.citations == []
    assert mock_router.chat.await_count == 0  # no synthesis call at all
    assert pipeline._reranker.rerank.await_count == 0  # the incidental documents are not even ranked


@pytest.mark.asyncio
async def test_a_model_that_would_write_a_table_cannot_create_a_second_table_for_a_database_question(mock_router, mock_store, mock_embeddings):
    mock_router.chat = AsyncMock(return_value=_FAKE_TABLE)

    async def _stream(*args, **kwargs):
        yield _FAKE_TABLE

    mock_router.chat_stream = _stream
    pipeline = _pipeline(mock_router, mock_store, mock_embeddings)

    streamed = [c async for c in pipeline.query_stream("Show me the live database results")]
    final = [c for c in streamed if isinstance(c, QueryResult)][0]
    text = "".join(c for c in streamed if isinstance(c, str))
    assert final.answer == "Found 1 matching live record." and text == final.answer
    assert "|" not in text and final.sql_payload is not None
    assert mock_router.chat.await_count == 0


@pytest.mark.asyncio
async def test_a_zero_row_database_result_still_gets_one_plain_sentence(mock_router, mock_store, mock_embeddings):
    empty = _sql_retrieved()
    empty.chunk.metadata = {"sql_payload": {"query": "SELECT 1", "columns": ["a"], "rows": [], "row_count": 0}}
    mock_router.chat = AsyncMock(return_value=_FAKE_TABLE)
    pipeline = _pipeline(mock_router, mock_store, mock_embeddings)
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[empty])
    result = await pipeline.query("Show me the live database results")
    assert result.answer == "No matching live records were found." and mock_router.chat.await_count == 0


@pytest.mark.asyncio
async def test_a_genuine_two_part_question_keeps_the_database_result_and_the_document_explanation_apart(mock_router, mock_store, mock_embeddings):
    """The question names documents too: both sections stay. The document model still never sees the rows, and a table it writes
    is removed, so the only table is the live one."""
    mock_router.chat = AsyncMock(return_value=f"The return policy allows 30 days.\n\n{_FAKE_TABLE}\n\nSee the policy.")
    pipeline = _pipeline(mock_router, mock_store, mock_embeddings)

    result = await pipeline.query("List all warehouses and explain the return policy document")

    assert "The return policy allows 30 days." in result.answer and "See the policy." in result.answer
    assert "H100" not in result.answer  # the invented table is gone
    assert result.answer.count(_sql_table_md()) == 1 and result.sql_payload is not None  # the live one is attached once
    prompt = " ".join(m.get("content", "") for m in mock_router.chat.call_args.kwargs["messages"])
    assert "A100" not in prompt and "SQL Query Executed" not in prompt
    assert "never say or imply that you have seen the live database rows" in prompt


@pytest.mark.asyncio
async def test_an_explicit_mix_request_keeps_both_sections_even_without_a_document_word(mock_router, mock_store, mock_embeddings):
    mock_router.chat = AsyncMock(return_value="Prose from the document.")
    pipeline = _pipeline(mock_router, mock_store, mock_embeddings)
    result = await pipeline.query("Show me the live database results", mode="mix")
    assert "Prose from the document." in result.answer and _sql_table_md() in result.answer
    assert mock_router.chat.await_count == 1


@pytest.mark.asyncio
async def test_no_sql_result_leaves_the_document_path_untouched(mock_router, mock_store, mock_embeddings):
    mock_router.chat = AsyncMock(return_value="According to the document: Paris.")
    pipeline = _pipeline(mock_router, mock_store, mock_embeddings, sql=False)
    result = await pipeline.query("Show me the live database results")
    assert "According to the document: Paris." in result.answer and result.sql_payload is None


def test_the_one_sentence_and_the_table_stripper_are_plain_deterministic_helpers():
    from src.rag.retrieval import _database_result_sentence, _strip_markdown_tables

    assert _database_result_sentence({"row_count": 500}) == "Found 500 matching live records."
    assert _database_result_sentence({"row_count": 1234}) == "Found 1,234 matching live records."
    assert _database_result_sentence({"row_count": 1}) == "Found 1 matching live record."
    assert _database_result_sentence({"row_count": 0}) == "No matching live records were found."
    assert _database_result_sentence({"rows": [{}, {}]}) == "Found 2 matching live records."  # no count: the rows are counted
    assert _database_result_sentence({"row_count": True, "rows": []}) == "No matching live records were found."
    text = "Intro line.\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\nA | not a table.\n| only one line |\n\nEnd."
    out = _strip_markdown_tables(text)
    assert "| 1 | 2 |" not in out and "Intro line." in out and "End." in out
    assert "A | not a table." in out and "| only one line |" in out  # a single pipe line is not a table
    assert _strip_markdown_tables("no tables here") == "no tables here"


@pytest.mark.asyncio
async def test_sql_only_no_documents_returns_table_direct(
    mock_router, mock_store, mock_embeddings
):
    """SQL result with NO document context short-circuits to the exact table,
    with no LLM synthesis call — the solo-SQL path."""
    sql_retrieved = _sql_retrieved()

    mock_router.chat = AsyncMock(return_value="unused")
    mock_store.search_hybrid = AsyncMock(return_value=[])  # no documents match

    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings,
    )
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[sql_retrieved])

    result = await pipeline.query("Show me the live database results")

    assert _sql_table_md() in result.answer
    assert result.model_used == "sql/direct"
    assert mock_router.chat.await_count == 0


@pytest.mark.asyncio
async def test_mode_sql_bypasses_vector_retrieval(mock_router, mock_store, mock_embeddings):
    """When mode='sql', vector search is completely bypassed even if docs exist in store."""
    sql_retrieved = _sql_retrieved()
    mock_store.search_hybrid = AsyncMock(return_value=[_doc_retrieved()])

    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings,
    )
    pipeline._retriever.retrieve = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[sql_retrieved])

    result = await pipeline.query("List all warehouses", mode="sql")

    assert _sql_table_md() in result.answer
    assert result.model_used in ("sql/direct", "fast_path/list")
    assert pipeline._sql_retriever.retrieve.await_count == 1
    assert pipeline._retriever.retrieve.await_count == 0


@pytest.mark.asyncio
async def test_mode_rag_bypasses_sql_retrieval(mock_router, mock_store, mock_embeddings):
    """When mode='rag', SQL retriever is completely bypassed."""
    doc_retrieved = _doc_retrieved()
    mock_router.chat = AsyncMock(return_value="According to document policy, returns are 30 days.")

    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings,
    )
    pipeline._sql_retriever.retrieve = AsyncMock()
    pipeline._retriever.retrieve = AsyncMock(return_value=[doc_retrieved])
    pipeline._reranker.rerank = AsyncMock(return_value=[doc_retrieved])

    result = await pipeline.query("What is the return policy?", mode="rag")

    assert "30 days" in result.answer
    assert pipeline._sql_retriever.retrieve.await_count == 0
    assert pipeline._retriever.retrieve.await_count == 1
