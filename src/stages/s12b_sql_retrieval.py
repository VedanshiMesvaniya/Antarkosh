"""Stage 12b — Text-to-SQL Retrieval (Compatibility Shim).

This module is a backwards-compatibility shim that re-exports all symbols from:
- src.sql.pipeline (SQLRetriever, cache lifecycle)
- src.sql.generation (SQL generation, execution retry glue, safety validation, formatting)
- src.sql.prompt_builder (Prompt construction, glossary, relationships, caches)
- src.sql.schema_retrieval (Qdrant schema retrieval, DDL extraction)
- src.sql.table_router (Table routing, routing hints)
- src.sql.intent (Analytical intent extraction)
- src.sql.soft_delete (Soft-delete detection and enforcement)

New code should import directly from the respective src.sql modules.
This shim will be removed in Phase I.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

# Re-exports from src.sql.pipeline (extracted in Step F7)
from src.sql.pipeline import (
    MAX_RESULT_CACHE_ENTRIES,
    SQLRetriever,
    _MAX_RESULT_CACHE_ENTRIES,
    _SQLRetriever,
    _check_feature_enabled,
    _clear_knowledge_caches,
    clear_knowledge_caches,
)

# Re-exports from src.sql.generation (extracted in Step F6)
from src.sql.generation import (
    ABSTAIN_RE,
    DANGEROUS_FUNCTIONS,
    DISPLAY_HIDDEN_COLS,
    FENCE_RE,
    MAX_DISPLAY_ROWS,
    SQL_START_RE,
    UnsafeQueryError,
    _ABSTAIN_RE,
    _DANGEROUS_FUNCTIONS,
    _DISPLAY_HIDDEN_COLS,
    _FENCE_RE,
    _MAX_DISPLAY_ROWS,
    _SQL_START_RE,
    _extract_table_names,
    _filter_display_rows,
    _format_rows_as_markdown,
    _is_aggregate_over_zero_rows,
    _is_all_null,
    _is_safe_read_query,
    _sanitize_cell_value,
    _sanitize_rows,
    _unwrap_sql,
    execute_with_delta_repair,
    execute_with_retry,
    extract_cot_and_sql,
    extract_table_names,
    fetch_sqlite_foreign_keys,
    filter_display_rows,
    format_fk_rows,
    format_rows_as_markdown,
    format_schema_rows,
    generate_sql,
    is_aggregate_over_zero_rows,
    is_all_null,
    is_safe_read_query,
    sanitize_cell_value,
    sanitize_rows,
    unwrap_sql,
)

_fetch_sqlite_foreign_keys = fetch_sqlite_foreign_keys
_generate_sql = generate_sql
_execute_with_retry = execute_with_retry
_execute_with_delta_repair = execute_with_delta_repair
_extract_cot_and_sql = extract_cot_and_sql
_format_schema_rows = format_schema_rows
_format_fk_rows = format_fk_rows
_UnsafeQueryError = UnsafeQueryError

# Re-exports from src.sql.prompt_builder (extracted in Step F5)
from src.sql.prompt_builder import (
    GLOSSARY_STOP_WORDS,
    OUTPUT_READABILITY_RULES,
    _BEHAVIORAL_ATLAS_CACHE,
    _LOAD_GLOSSARY_CACHE,
    _LOAD_RELATIONSHIPS_CACHE,
    _RAW_COLUMN_GLOSSARY_CACHE,
    _RAW_RELATIONSHIPS_CACHE,
    build_behavioral_atlas_for_query,
    build_column_glossary_for_query,
    build_sql_prompt,
    clear_behavioral_atlas_cache,
    clear_glossary_cache,
    clear_prompt_caches,
    clear_raw_column_glossary_cache,
    clear_raw_relationships_cache,
    clear_relationships_cache,
    get_raw_behavioral_atlas,
    get_raw_column_glossary,
    get_raw_relationships,
    get_scoped_readability_rules,
    load_glossary,
    load_relationships,
    matches_glossary_candidate,
    stem_word,
)

RAW_RELATIONSHIPS_CACHE = _RAW_RELATIONSHIPS_CACHE
_RAW_RELATIONSHIPS_CACHE = _RAW_RELATIONSHIPS_CACHE
LOAD_RELATIONSHIPS_CACHE = _LOAD_RELATIONSHIPS_CACHE
_LOAD_RELATIONSHIPS_CACHE = _LOAD_RELATIONSHIPS_CACHE
LOAD_GLOSSARY_CACHE = _LOAD_GLOSSARY_CACHE
_LOAD_GLOSSARY_CACHE = _LOAD_GLOSSARY_CACHE
RAW_COLUMN_GLOSSARY_CACHE = _RAW_COLUMN_GLOSSARY_CACHE
_RAW_COLUMN_GLOSSARY_CACHE = _RAW_COLUMN_GLOSSARY_CACHE
BEHAVIORAL_ATLAS_CACHE = _BEHAVIORAL_ATLAS_CACHE
_BEHAVIORAL_ATLAS_CACHE = _BEHAVIORAL_ATLAS_CACHE

_build_sql_prompt = build_sql_prompt
_get_scoped_readability_rules = get_scoped_readability_rules
_OUTPUT_READABILITY_RULES = OUTPUT_READABILITY_RULES
_build_column_glossary_for_query = build_column_glossary_for_query
_build_behavioral_atlas_for_query = build_behavioral_atlas_for_query
_get_raw_relationships = get_raw_relationships
_load_relationships = load_relationships
_load_glossary = load_glossary
_get_raw_column_glossary = get_raw_column_glossary
_get_raw_behavioral_atlas = get_raw_behavioral_atlas
_stem_word = stem_word
_matches_glossary_candidate = matches_glossary_candidate
_GLOSSARY_STOP_WORDS = GLOSSARY_STOP_WORDS
_clear_prompt_caches = clear_prompt_caches
_clear_raw_relationships_cache = clear_raw_relationships_cache
_clear_relationships_cache = clear_relationships_cache
_clear_glossary_cache = clear_glossary_cache
_clear_raw_column_glossary_cache = clear_raw_column_glossary_cache
_clear_behavioral_atlas_cache = clear_behavioral_atlas_cache

# Re-exports from src.sql.schema_retrieval (extracted in Step F4)
from src.sql.schema_retrieval import (
    build_scoped_schema_fallback,
    extract_schema_table_names,
    extract_table_ddl_map,
    format_scoped_relationships,
    get_1hop_neighbors,
    retrieve_schema_from_qdrant,
)

_extract_schema_table_names = extract_schema_table_names
_extract_table_ddl_map = extract_table_ddl_map
_get_1hop_neighbors = get_1hop_neighbors
_format_scoped_relationships = format_scoped_relationships
_build_scoped_schema_fallback = build_scoped_schema_fallback
_retrieve_schema_from_qdrant = retrieve_schema_from_qdrant

# Re-exports from src.sql.soft_delete (extracted in Step F1)
from src.sql.soft_delete import (
    FALLBACK_SOFT_DELETE_TABLES,
    _SOFT_DELETE_TABLES_CACHE,
    _clear_soft_delete_tables_cache,
    _get_tables_with_soft_delete,
    detect_soft_delete_intent,
    enforce_soft_delete_filter,
)

_FALLBACK_SOFT_DELETE_TABLES = FALLBACK_SOFT_DELETE_TABLES
_detect_soft_delete_intent = detect_soft_delete_intent
_enforce_soft_delete_filter = enforce_soft_delete_filter
SOFT_DELETE_TABLES_CACHE = _SOFT_DELETE_TABLES_CACHE
clear_soft_delete_tables_cache = _clear_soft_delete_tables_cache
get_tables_with_soft_delete = _get_tables_with_soft_delete

# Re-exports from src.sql.intent (extracted in Step F2)
from src.sql.intent import extract_analytical_intent

_extract_analytical_intent = extract_analytical_intent

# Re-exports from src.sql.table_router (extracted in Step F3)
from src.sql.table_router import (
    _ROUTING_HINTS_CACHE,
    _clear_routing_hints_cache,
    load_routing_hints,
    route_anchor_tables,
    route_tables_for_query,
)

ROUTING_HINTS_CACHE = _ROUTING_HINTS_CACHE
clear_routing_hints_cache = _clear_routing_hints_cache
_load_routing_hints = load_routing_hints
_route_anchor_tables = route_anchor_tables
_route_tables_for_query = route_tables_for_query

# Common pipeline & core imports preserved for monkeypatching / test compatibility
from src.core.config import CONFIG_DIR, settings
from src.core.db_client import run_readonly_query
from src.sql.learning.pipeline_metrics import CURRENT_DB_ID, log_event as _log_pipeline_event
from src.core.provider_client import ProviderRouter
from src.sql.safety.column_registry import ColumnRegistry
from src.core.sql_dialects import SQLDialectProfile, get_dialect_profile
from src.sql.safety.result_validator import ResultValidator, ValidationSeverity
from src.sql.learning.pattern_learner import PatternLearner
from src.sql.safety.confidence_scorer import ConfidenceScorer, ConfidenceBreakdown
from src.models.schemas import Chunk, ChunkType, RetrievedChunk, DocumentType
from src.rag.stages.s10_embeddings import EmbeddingService
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID, get_knowledge_path
from src.sql.safety.empty_result_classifier import classify_empty_result
from src.sql.learning.failure_capture import capture_sql_failure
from src.utils.feature_flags import is_feature_enabled
from src.utils.query_budget import QueryBudgetExceededError, get_or_create_budget_controller
from src.sql.schema_budget import DEFAULT_SCHEMA_TOKEN_BUDGET, select_schema_within_budget
from src.sql.schema_compactor import compact_ddl, extract_join_hints
from src.sql.schema_token_estimator import estimate_schema_tokens
from src.utils.stream_token_counter import TokenBudgetExceededError
from src.utils.telemetry import get_or_create_query_id, log_telemetry, timed_stage
from src.guards.schema_guard import evaluate_schema_sufficiency
from src.guards.temporal_guard import evaluate_temporal_filter
from src.models.trace import GuardResult
from src.utils.trace_context import get_current_span
from src.sql.repair.sql_repair import (
    MAX_DELTA_REPAIR_ATTEMPTS,
    attempt_delta_repair,
    extract_schema_context_from_ddl,
)

if TYPE_CHECKING:
    from src.rag.stages.s11_vector_store import QdrantStore

logger = logging.getLogger(__name__)

_is_feature_enabled = is_feature_enabled
_capture_sql_failure = capture_sql_failure
