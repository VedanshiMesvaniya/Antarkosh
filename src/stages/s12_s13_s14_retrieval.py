"""Backward-compatibility shim for src.stages.s12_s13_s14_retrieval.

Re-exports all symbols from src.rag.retrieval.
"""

from __future__ import annotations

from src.rag.retrieval import (
    _ACRONYM_MAP_CACHE,
    _ANSWER_RULES,
    _DEDUP_MIN_TOKENS,
    _DEDUP_WORD_RE,
    _EXHAUSTIVE_KEYWORDS,
    _LEXICAL_STOPWORDS,
    _MODE_INSTRUCTIONS,
    _VISUALIZATION_GUIDANCE,
    _VISUALIZATION_KEYWORDS,
    AGGREGATE_QUERY,
    EXPLANATION_QUERY,
    LIST_QUERY,
    EmbeddingService,
    Generator,
    QdrantStore,
    QueryResult,
    QueryType,
    Reranker,
    RetrievedChunk,
    Retriever,
    _build_context,
    _build_system_prompt,
    _classify_query_task,
    _dedup_tokens,
    _enforce_document_diversity,
    _expand_query_acronyms,
    _extract_and_format_citations,
    _extract_sql_payload,
    _extract_sql_table,
    _get_acronym_expansions,
    _history_messages,
    _is_exhaustive_query,
    _is_visualization_query,
    _lexical_rerank,
    _lexical_terms,
    _limit_context_chunks,
    _source_mode,
    _suppress_near_duplicates,
    build_aggregate_micro_prompt,
    classify_query,
    classify_query_intent,
    fast_path_format,
    format_aggregate_fast_path,
    format_list_fast_path,
    is_feature_enabled,
    log_telemetry,
)

__all__ = [
    "AGGREGATE_QUERY",
    "EXPLANATION_QUERY",
    "LIST_QUERY",
    "_ACRONYM_MAP_CACHE",
    "_ANSWER_RULES",
    "_DEDUP_MIN_TOKENS",
    "_DEDUP_WORD_RE",
    "_EXHAUSTIVE_KEYWORDS",
    "_LEXICAL_STOPWORDS",
    "_MODE_INSTRUCTIONS",
    "_VISUALIZATION_GUIDANCE",
    "_VISUALIZATION_KEYWORDS",
    "EmbeddingService",
    "Generator",
    "QdrantStore",
    "QueryResult",
    "QueryType",
    "Reranker",
    "RetrievedChunk",
    "Retriever",
    "_build_context",
    "_build_system_prompt",
    "_classify_query_task",
    "_dedup_tokens",
    "_enforce_document_diversity",
    "_expand_query_acronyms",
    "_extract_and_format_citations",
    "_extract_sql_payload",
    "_extract_sql_table",
    "_get_acronym_expansions",
    "_history_messages",
    "_is_exhaustive_query",
    "_is_visualization_query",
    "_lexical_rerank",
    "_lexical_terms",
    "_limit_context_chunks",
    "_source_mode",
    "_suppress_near_duplicates",
    "build_aggregate_micro_prompt",
    "classify_query",
    "classify_query_intent",
    "fast_path_format",
    "format_aggregate_fast_path",
    "format_list_fast_path",
    "is_feature_enabled",
    "log_telemetry",
]

import sys
import types

import src.rag.retrieval as _target_module


class _ShimModule(types.ModuleType):
    def __getattr__(self, name: str):
        return getattr(_target_module, name)

    def __setattr__(self, name: str, value):
        super().__setattr__(name, value)
        if name != "__class__":
            setattr(_target_module, name, value)

    def __delattr__(self, name: str):
        super().__delattr__(name)
        if hasattr(_target_module, name):
            delattr(_target_module, name)


sys.modules[__name__].__class__ = _ShimModule

