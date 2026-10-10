"""Stage 12b text-to-SQL retrieval pipeline.

Extracted from Stage 12b (s12b_sql_retrieval.py) in Step F7.
Houses the SQLRetriever engine, result cache, and lifecycle management.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from src.core.confidence_scorer import ConfidenceBreakdown, ConfidenceScorer
from src.core.config import settings
from src.core.db_client import run_readonly_query
from src.core.pattern_learner import PatternLearner
from src.core.pipeline_metrics import CURRENT_DB_ID
from src.core.provider_client import ProviderRouter
from src.core.result_validator import ResultValidator
from src.core.sql_column_registry import ColumnRegistry
from src.core.sql_dialects import get_dialect_profile
from src.guards.schema_guard import evaluate_schema_sufficiency
from src.models.schemas import RetrievedChunk
from src.sql.engine import Engine
from src.sql.generation import (
    _DANGEROUS_FUNCTIONS,
    execute_with_delta_repair,
    execute_with_retry,
    fetch_sqlite_foreign_keys,
    format_fk_rows,
    format_schema_rows,
    generate_sql,
    is_safe_read_query,
)
from src.sql.knowledge.loaders import DEFAULT_DB_ID
from src.sql.prompt_builder import (
    OUTPUT_READABILITY_RULES,
    clear_prompt_caches,
    get_raw_behavioral_atlas,
    get_scoped_readability_rules,
    load_glossary,
    load_relationships,
)
from src.sql.registry import get_connection, get_database
from src.sql.schema_retrieval import retrieve_schema_from_qdrant
from src.sql.soft_delete import _get_tables_with_soft_delete
from src.sql.table_router import load_routing_hints
from src.stages.s10_embeddings import EmbeddingService
from src.utils.feature_flags import is_feature_enabled
from src.utils.query_budget import get_or_create_budget_controller
from src.utils.telemetry import get_or_create_query_id, log_telemetry, timed_stage
from src.utils.trace_context import get_current_span

if TYPE_CHECKING:
    from src.stages.s11_vector_store import QdrantStore

logger = logging.getLogger(__name__)

_MAX_RESULT_CACHE_ENTRIES = 256
MAX_RESULT_CACHE_ENTRIES = _MAX_RESULT_CACHE_ENTRIES


def _check_feature_enabled(flag: str) -> bool:
    """Check feature flag, honoring mock patches on s12b_sql_retrieval or pipeline."""
    s12b = sys.modules.get("src.stages.s12b_sql_retrieval")
    if s12b and hasattr(s12b, "is_feature_enabled"):
        try:
            return s12b.is_feature_enabled(flag)
        except Exception:
            pass
    return is_feature_enabled(flag)


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


_clear_knowledge_caches = clear_knowledge_caches


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
        self._glossary = load_glossary(self.db_id)
        self._relationships = load_relationships(self.db_id)
        self._pattern_learner = PatternLearner()
        self._confidence_scorer = ConfidenceScorer()
        self._result_validator = ResultValidator(get_raw_behavioral_atlas(self.db_id) or {})
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
        if _check_feature_enabled("delta_repair_enabled"):
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


_SQLRetriever = SQLRetriever
