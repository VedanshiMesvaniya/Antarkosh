"""Query API — RAG query endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from src.api.auth import get_current_user
from src.pipeline.query import QueryPipeline
from src.sql.context import DEFAULT_DB_ID
from src.sql.knowledge.loaders import validate_db_id

logger = logging.getLogger(__name__)
router = APIRouter()


class QueryRequest(BaseModel):
    """Request body for the query endpoint."""

    question: str
    top_k: int = 30
    rerank_top_k: int = 6
    filters: dict[str, Any] | None = None
    db_id: str | None = None


@router.post("/query")
async def query_documents(
    request: QueryRequest,
    current_user: str = Depends(get_current_user),
) -> dict:
    """Query ingested documents using the RAG pipeline.

    Supports optional metadata filtering via the ``filters`` field to
    constrain retrieval to specific document types, files, or page ranges.
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    target_db_id = DEFAULT_DB_ID
    if request.db_id is not None:
        try:
            validate_db_id(request.db_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        target_db_id = request.db_id.strip()

    query_filters = dict(request.filters or {})
    # Identity and access scope are decided by the server, never by the client.
    for reserved in ("user_id", "allowed_document_ids", "scope_key", "erp_instance_id"):
        query_filters.pop(reserved, None)
    query_filters["user_id"] = current_user

    try:
        pipeline = QueryPipeline(db_id=target_db_id)
        result = await pipeline.query(request.question, filters=query_filters or None, db_id=target_db_id)
        return {
            "status": "success",
            "query": result.query,
            "answer": result.answer,
            "citations": [c.model_dump() for c in result.citations],
            "model_used": result.model_used,
            "chunks_retrieved": result.chunks_retrieved,
            "chunks_after_rerank": result.chunks_after_rerank,
            "filters_applied": request.filters,
        }
    except HTTPException:
        raise
    except Exception:
        # Log the full error server-side; return a generic message so internal
        # details (paths, provider errors, keys in messages) don't leak.
        logger.exception("Query failed")
        raise HTTPException(status_code=500, detail="Query failed. Please try again.")
