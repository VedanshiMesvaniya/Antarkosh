"""Stage 12b Ã¢â¬â Text-to-SQL Retrieval.

Dynamically translates natural language into SQL against the live database,
executes it, and returns the results formatted as a context chunk.
"""

from __future__ import annotations

from collections import OrderedDict
import datetime
from decimal import Decimal
import functools
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from src.core.config import CONFIG_DIR, settings
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID, get_knowledge_path
from src.core.db_client import run_readonly_query
from src.core.pipeline_metrics import log_event as _log_pipeline_event
from src.core.provider_client import ProviderRouter
from src.core.sql_column_registry import ColumnRegistry
from src.core.sql_dialects import SQLDialectProfile, get_dialect_profile
from src.core.result_validator import ResultValidator, ValidationSeverity
from src.core.pattern_learner import PatternLearner
from src.core.confidence_scorer import ConfidenceScorer, ConfidenceBreakdown
from src.models.schemas import Chunk, ChunkType, RetrievedChunk, DocumentType
from src.stages.s10_embeddings import EmbeddingService

if TYPE_CHECKING:  # annotation only; avoids a runtime import of the vector-store stage
    from src.stages.s11_vector_store import QdrantStore
from src.utils.circuit_breaker import CircuitBreakerOpenError, get_shared_circuit_breaker
from src.utils.empty_result_classifier import classify_empty_result
from src.utils.error_classification import classify_error
from src.utils.failure_capture import capture_sql_failure
from src.utils.feature_flags import is_feature_enabled
from src.utils.query_budget import QueryBudgetExceededError, get_or_create_budget_controller
from src.utils.schema_budget import DEFAULT_SCHEMA_TOKEN_BUDGET, select_schema_within_budget
from src.utils.stream_token_counter import TokenBudgetExceededError
from src.utils.schema_compactor import compact_ddl, extract_join_hints
from src.utils.schema_token_estimator import estimate_schema_tokens
from src.utils.sql_safety import (
    DANGEROUS_FUNCTIONS as _SHARED_DANGEROUS_FUNCTIONS,
    check_dangerous_patterns,
    has_dangerous_qualified_call,
    is_destructive_sql,
    validate_sql_safety,
    validate_tables_and_columns,
)
from src.utils.telemetry import get_or_create_query_id, log_telemetry, timed_stage
from src.guards.schema_guard import evaluate_schema_sufficiency
from src.guards.temporal_guard import evaluate_temporal_filter
from src.models.trace import GuardResult
from src.utils.trace_context import get_current_span
from src.stages.sql_repair import (
    MAX_DELTA_REPAIR_ATTEMPTS,
    attempt_delta_repair,
    extract_schema_context_from_ddl,
)

logger = logging.getLogger(__name__)

# Re-exported from src.sql.generation (extracted in Step F6)
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


# Re-exported from src.sql.prompt_builder (extracted in Step F5)
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

# Re-exports for src.sql.prompt_builder
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
build_sql_prompt = build_sql_prompt
_get_scoped_readability_rules = get_scoped_readability_rules
get_scoped_readability_rules = get_scoped_readability_rules
_OUTPUT_READABILITY_RULES = OUTPUT_READABILITY_RULES
OUTPUT_READABILITY_RULES = OUTPUT_READABILITY_RULES
_build_column_glossary_for_query = build_column_glossary_for_query
build_column_glossary_for_query = build_column_glossary_for_query
_build_behavioral_atlas_for_query = build_behavioral_atlas_for_query
build_behavioral_atlas_for_query = build_behavioral_atlas_for_query
_get_raw_relationships = get_raw_relationships
get_raw_relationships = get_raw_relationships
_load_relationships = load_relationships
load_relationships = load_relationships
_load_glossary = load_glossary
load_glossary = load_glossary
_get_raw_column_glossary = get_raw_column_glossary
get_raw_column_glossary = get_raw_column_glossary
_get_raw_behavioral_atlas = get_raw_behavioral_atlas
get_raw_behavioral_atlas = get_raw_behavioral_atlas
_stem_word = stem_word
stem_word = stem_word
_matches_glossary_candidate = matches_glossary_candidate
matches_glossary_candidate = matches_glossary_candidate
_GLOSSARY_STOP_WORDS = GLOSSARY_STOP_WORDS
GLOSSARY_STOP_WORDS = GLOSSARY_STOP_WORDS
_clear_prompt_caches = clear_prompt_caches
clear_prompt_caches = clear_prompt_caches
_clear_raw_relationships_cache = clear_raw_relationships_cache
clear_raw_relationships_cache = clear_raw_relationships_cache
_clear_relationships_cache = clear_relationships_cache
clear_relationships_cache = clear_relationships_cache
_clear_glossary_cache = clear_glossary_cache
clear_glossary_cache = clear_glossary_cache
_clear_raw_column_glossary_cache = clear_raw_column_glossary_cache
clear_raw_column_glossary_cache = clear_raw_column_glossary_cache
_clear_behavioral_atlas_cache = clear_behavioral_atlas_cache
clear_behavioral_atlas_cache = clear_behavioral_atlas_cache


from src.sql.schema_retrieval import (
    build_scoped_schema_fallback,
    extract_schema_table_names,
    extract_table_ddl_map,
    format_scoped_relationships,
    get_1hop_neighbors,
    retrieve_schema_from_qdrant,
)

# Re-exports from src.sql.schema_retrieval
_extract_schema_table_names = extract_schema_table_names
extract_schema_table_names = extract_schema_table_names
_extract_table_ddl_map = extract_table_ddl_map
extract_table_ddl_map = extract_table_ddl_map
_get_1hop_neighbors = get_1hop_neighbors
get_1hop_neighbors = get_1hop_neighbors
_format_scoped_relationships = format_scoped_relationships
format_scoped_relationships = format_scoped_relationships
_build_scoped_schema_fallback = build_scoped_schema_fallback
build_scoped_schema_fallback = build_scoped_schema_fallback
_retrieve_schema_from_qdrant = retrieve_schema_from_qdrant
retrieve_schema_from_qdrant = retrieve_schema_from_qdrant



# Re-exported from src.sql.soft_delete (extracted in Step F1)
from src.sql.soft_delete import (
    FALLBACK_SOFT_DELETE_TABLES,
    _SOFT_DELETE_TABLES_CACHE,
    _clear_soft_delete_tables_cache,
    _get_tables_with_soft_delete,
    detect_soft_delete_intent,
    enforce_soft_delete_filter,
)


def clear_knowledge_caches(db_id: str | None = None) -> None:
    """Clear all knowledge caches for a specific db_id or globally."""
    clear_prompt_caches(db_id)
    _get_tables_with_soft_delete.cache_clear(db_id)
    load_routing_hints.cache_clear(db_id)
    try:
        from src.sql.context import clear_context_cache
        clear_context_cache(db_id)
    except Exception:
        pass


# Re-exported from src.sql.intent (extracted in Step F2)
from src.sql.intent import extract_analytical_intent

# Re-exported from src.sql.table_router (extracted in Step F3)
from src.sql.table_router import (
    _ROUTING_HINTS_CACHE,
    _clear_routing_hints_cache,
    load_routing_hints,
    route_anchor_tables,
    route_tables_for_query,
)





_MAX_RESULT_CACHE_ENTRIES = 256


class SQLRetriever:
    """Generates and executes SQL queries for analytical questions."""
    _full_schema_cache: dict[str, str] = {}
    _column_registry: dict[str, ColumnRegistry] = {}
    # Bounded LRU cache for query results, keyed on (db_id, normalized question text).
    _result_cache: OrderedDict[tuple[str, str], tuple[float, list[RetrievedChunk]]] = OrderedDict()

    def __init__(
        self,
        router: ProviderRouter,
        vector_store: QdrantStore | None = None,
        embedding_service: EmbeddingService | None = None,
        db_id: str = DEFAULT_DB_ID,
    ) -> None:
        self.db_id = (db_id or DEFAULT_DB_ID).strip()
        self._router = router
        self._vector_store = vector_store
        self._embeddings = embedding_service
        from src.sql.registry import get_connection, get_database
        conn_cfg = get_connection(self.db_id)
        engine = conn_cfg.get("engine")
        if not engine:
            try:
                db_meta = get_database(self.db_id)
                engine = db_meta.get("engine")
            except Exception:
                pass
        if not engine:
            engine = settings.db_engine
        self._dialect = get_dialect_profile(engine)
        self._glossary = _load_glossary(self.db_id)
        self._relationships = _load_relationships(self.db_id)
        self._pattern_learner = PatternLearner()
        self._confidence_scorer = ConfidenceScorer()
        self._result_validator = ResultValidator(_get_raw_behavioral_atlas(self.db_id) or {})
        self.last_infra_error: str | None = None
        self.last_query_status: str | None = None
        self.last_cot_plan: str | None = None
        self.last_confidence_score: float | None = None
        self.last_confidence_breakdown: ConfidenceBreakdown | None = None
        self.last_sql_payload: dict[str, Any] | None = None

    @property
    def column_registry(self) -> ColumnRegistry | None:
        """Return the column registry for this retriever's database."""
        return SQLRetriever._column_registry.get(self.db_id)

    @classmethod
    def clear_result_cache(cls, db_id: str | None = None) -> None:
        """Clear the cached query results for all databases or a specific db_id."""
        if db_id is None:
            cls._result_cache.clear()
        else:
            keys_to_remove = [k for k in cls._result_cache if k[0] == db_id]
            for k in keys_to_remove:
                cls._result_cache.pop(k, None)

    async def retrieve(self, query: str, user_id: str | None = None) -> list[RetrievedChunk]:
        """Convert NL to SQL, execute, and return formatted results (with 1 retry)."""
        if user_id and user_id not in ("admin", "*", "all"):
            from src.sql.registry import can_access_database

            if not can_access_database(self.db_id, user_id):
                logger.info(
                    "User %r has no access to database %r — returning empty SQL retrieval results",
                    user_id,
                    self.db_id,
                )
                return []

        from src.core.pipeline_metrics import CURRENT_DB_ID
        CURRENT_DB_ID.set(self.db_id)


        self.last_infra_error = None
        self.last_query_status = None
        self.last_cot_plan = None
        self.last_confidence_score = None
        self.last_confidence_breakdown = None
        self.last_sql_payload = None
        cache_key = (self.db_id, query.strip().lower())
        now = time.monotonic()

        cached = SQLRetriever._result_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_chunks = cached
            if now - cached_at < settings.sql_result_cache_ttl_seconds:
                logger.info("SQL result cache hit for query [%s]: %s", self.db_id, query)
                SQLRetriever._result_cache.move_to_end(cache_key)
                self.last_query_status = "success"
                return [c.model_copy(deep=True) for c in cached_chunks]
            else:
                SQLRetriever._result_cache.pop(cache_key, None)

        result = await self._retrieve_uncached(query)
        # Only cache valid results when query executed successfully and no infra outage occurred.
        if self.last_infra_error is None and self.last_query_status in ("success", "empty_result") and result:
            while len(SQLRetriever._result_cache) >= _MAX_RESULT_CACHE_ENTRIES:
                SQLRetriever._result_cache.popitem(last=False)
            SQLRetriever._result_cache[cache_key] = (
                now,
                [c.model_copy(deep=True) for c in result],
            )
        return result

    async def _retrieve_uncached(self, query: str) -> list[RetrievedChunk]:
        qid = get_or_create_query_id()
        budget_ctrl = get_or_create_budget_controller(qid)

        if not budget_ctrl.can_proceed():
            logger.warning("Query budget exhausted before SQL retrieval for query %s", qid)
            log_telemetry(
                query_id=qid,
                stage="sql_generation",
                latency_ms=0.0,
                success=False,
                failure_type="budget_exceeded",
                extra={"budget_status": budget_ctrl.get_budget_status()},
            )
            self.last_query_status = "failed"
            return []

        with timed_stage("schema_retrieval") as schema_stage:
            schema = await self._get_schema(query)
            schema_stage["extra"] = {"schema_chars": len(schema)}
            if schema:
                schema_res = evaluate_schema_sufficiency(query, schema)
                curr_span = get_current_span()
                if curr_span:
                    curr_span.add_guard(schema_res)

        if not schema:
            self.last_query_status = "not_applicable"
            return []

        # Feature Flag Gate: Surgical Delta Repair vs Original Full-Context Retry
        if is_feature_enabled("delta_repair_enabled"):
            return await self._retrieve_with_delta_repair(query, schema)

        return await execute_with_retry(self, query, schema)

    async def _retrieve_with_delta_repair(self, query: str, schema: str) -> list[RetrievedChunk]:
        """Execute text-to-sql retrieval with targeted Delta Repair on validation/execution failure."""
        return await execute_with_delta_repair(self, query, schema)

    @classmethod
    def clear_schema_cache(cls, db_id: str | None = None) -> None:
        """Clear the cached full schema and column registry (e.g. after schema sync)."""
        if db_id is None:
            cls._full_schema_cache.clear()
            cls._column_registry.clear()
        else:
            cls._full_schema_cache.pop(db_id, None)
            cls._column_registry.pop(db_id, None)

    async def _fetch_full_schema(self) -> str:
        """Fetch the full, un-truncated DB schema to initialize the ColumnRegistry."""
        if self.db_id in SQLRetriever._full_schema_cache:
            return SQLRetriever._full_schema_cache[self.db_id]

        try:
            rows = await run_readonly_query(self._dialect.schema_query, max_rows=20000, db_id=self.db_id)
            schema = format_schema_rows(self._dialect, rows)

            if self._dialect.fk_query:  # MySQL / PostgreSQL: one query covers all FKs
                fk_rows = await run_readonly_query(self._dialect.fk_query, max_rows=20000, db_id=self.db_id)
            elif self._dialect.key == Engine.SQLITE:
                fk_rows = await fetch_sqlite_foreign_keys(db_id=self.db_id)
            else:
                fk_rows = []

            fk_text = format_fk_rows(fk_rows)
            full_schema = schema + ("\n\n" + fk_text if fk_text else "")

            # Only cache and build registry if the schema was successfully retrieved and non-empty.
            # An empty string from a missing/unready DB must never be cached as permanent truth.
            if full_schema.strip():
                SQLRetriever._full_schema_cache[self.db_id] = full_schema
                try:
                    SQLRetriever._column_registry[self.db_id] = ColumnRegistry(
                        full_schema, self._dialect.sqlglot_dialect
                    )
                except Exception as reg_err:
                    logger.warning("Failed to build column registry for %s: %s", self.db_id, reg_err)

            return full_schema
        except Exception as e:
            logger.error("Failed to fetch full schema: %s", e)
            return ""

    async def _get_schema(self, query: str) -> str:
        """Fetch relevant DB schema chunks for the prompt using Schema RAG.
        
        If vector_store is missing or fails, falls back to an intelligent, token-bounded schema.
        """
        full_schema = await self._fetch_full_schema()
        return await retrieve_schema_from_qdrant(
            query=query,
            full_schema=full_schema,
            vector_store=self._vector_store,
            embeddings=self._embeddings,
            db_id=self.db_id,
            dialect=self._dialect if hasattr(self, "_dialect") else None,
        )

    async def _generate_sql(self, query: str, schema: str, last_error: str | None = None) -> str:
        """Prompt the reasoning LLM to generate SQL."""
        sql, cot_plan, infra_error = await generate_sql(
            router=self._router,
            query=query,
            schema=schema,
            dialect=self._dialect,
            db_id=self.db_id,
            last_error=last_error,
            relationships=self._relationships,
            pattern_learner=getattr(self, "_pattern_learner", None),
        )
        if cot_plan:
            self.last_cot_plan = cot_plan
        if infra_error:
            self.last_infra_error = infra_error
        return sql

    _DANGEROUS_FUNCTIONS = _DANGEROUS_FUNCTIONS

    @classmethod
    def _get_scoped_readability_rules(cls, query: str, schema_tables: list[str]) -> str:
        """Dynamically scope readability rules to only those relevant to the query & schema tables,
        reducing SQL prompt tokens by 60% while preserving all critical schema nuances.
        """
        return get_scoped_readability_rules(query, schema_tables)

    _OUTPUT_READABILITY_RULES = OUTPUT_READABILITY_RULES

    def _is_safe_read_query(self, sql: str) -> bool:
        """Parse the AST and confirm it's a single, side-effect-free read SELECT or UNION."""
        return is_safe_read_query(sql, dialect=self._dialect.sqlglot_dialect)

