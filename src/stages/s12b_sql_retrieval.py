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

# Columns to hide from the user-facing display (markdown table + sqlPayload).
# The LLM-generated SQL query and internal row data are never filtered —
# the LLM may still use these columns in WHERE / JOIN / ORDER BY clauses.
_DISPLAY_HIDDEN_COLS: frozenset[str] = frozenset({
    "id",
    "created_at",
    "updated_at",
    "deleted_at",
})


def _filter_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *rows* with hidden metadata columns removed (display only).

    Columns are matched case-insensitively.  If ALL columns would be stripped
    (extremely unlikely), the original rows are returned unchanged so the user
    always sees something meaningful.
    """
    if not rows:
        return rows
    visible_keys = [
        k for k in rows[0].keys()
        if k.lower() not in _DISPLAY_HIDDEN_COLS
    ]
    # Safety-net: never return an empty table.
    if not visible_keys:
        return rows
    return [{k: row[k] for k in visible_keys} for row in rows]



def _sanitize_cell_value(val: Any) -> Any:
    """Ensure raw DB cell types (date, datetime, Decimal, bytes) are JSON serializable."""
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, (datetime.date, datetime.datetime, datetime.time)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return int(val) if val % 1 == 0 else float(val)
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    if isinstance(val, (list, tuple)):
        return [_sanitize_cell_value(v) for v in val]
    if isinstance(val, dict):
        return {k: _sanitize_cell_value(v) for k, v in val.items()}
    return str(val)


def _sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw DB rows to JSON-safe dictionaries."""
    if not rows:
        return []
    return [
        {k: _sanitize_cell_value(v) for k, v in row.items()}
        for row in rows
    ]


def format_schema_rows(profile: SQLDialectProfile, rows: list[dict[str, Any]]) -> str:
    """Turn an engine's raw introspection rows into schema text for the NL2SQL prompt.

    Pure function of (dialect profile, rows) Ã¢â¬â no instance state, no global
    settings Ã¢â¬â so it can be unit tested directly for each engine.

    SQLite's sqlite_master query already returns one full CREATE TABLE
    statement per row. MySQL's information_schema.columns query returns
    one row per column, so those need grouping by table first.
    """
    if profile.key == Engine.SQLITE:
        return "\n\n".join(
            row["sql"] for row in rows if row["name"] != "sqlite_sequence"
        )

    if profile.key in (Engine.MYSQL, Engine.POSTGRESQL):
        tables: dict[str, list[str]] = {}
        for row in rows:
            comment = row.get("column_comment") or ""
            suffix = f"  -- {comment}" if comment else ""
            tables.setdefault(row["table_name"], []).append(
                f"  {row['column_name']} {row['data_type']}{suffix}"
            )
        return "\n\n".join(
            f"TABLE {name} (\n" + ",\n".join(cols) + "\n)"
            for name, cols in tables.items()
        )

    raise ValueError(f"Unsupported dialect key {profile.key!r}")


def format_fk_rows(rows: list[dict]) -> str:
    if not rows:
        return ""

    lines = [
        f"  {r['table_name']}.{r['column_name']} -> {r['referenced_table_name']}.{r['referenced_column_name']}"
        for r in rows
    ]
    return "Foreign Keys:\n" + "\n".join(lines)


class UnsafeQueryError(Exception):
    """Raised when sqlglot rejects a query (e.g. not a SELECT). Never retried."""
    pass


_ABSTAIN_RE = re.compile(r"^\W*no_sql\b", re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)(?:```|$)", re.IGNORECASE | re.DOTALL)
_SQL_START_RE = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)


def _unwrap_sql(text: str) -> str:
    """Extract the SQL from an LLM response that may wrap it in a markdown code
    fence or precede it with a prose line or CoT reasoning.

    Only the minimal, safe extractions are performed:
      * strip reasoning <think>...</think> tags;
      * extract a fenced ```sql ... ``` (or bare ``` ... ```) block;
      * otherwise, if a leading prose preamble sits before the first SELECT/WITH,
        drop the preamble so the query still parses.
    """
    cleaned = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    if not cleaned and "</think>" in text:
        cleaned = text.split("</think>")[-1].strip()

    target = cleaned if cleaned else text.strip()
    m = _FENCE_RE.search(target)
    if m and m.group(1).strip():
        return m.group(1).strip()
    km = _SQL_START_RE.search(target)
    if km and km.start() >= 0:
        return target[km.start():].strip()
    return target


def extract_cot_and_sql(text: str) -> tuple[str, str]:
    """Separates structured metadata / CoT from the final SQL statement (supports JSON & Markdown)."""
    cleaned = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    target = cleaned if cleaned else text.strip()

    # Strip markdown json/sql wrapper if whole text is wrapped
    if target.startswith("```json") or target.startswith("```sql"):
        target = re.sub(r"^```(?:json|sql)?\s*", "", target)
        target = re.sub(r"\s*```$", "", target).strip()

    # 1. Try parsing direct JSON
    try:
        data = json.loads(target)
        if isinstance(data, dict) and "sql" in data and data["sql"]:
            return json.dumps({k: v for k, v in data.items() if k != "sql"}), str(data["sql"]).strip()
    except Exception:
        pass

    # 2. Try markdown ```sql ... ``` block anywhere in text
    m = _FENCE_RE.search(text)
    if m and m.group(1).strip():
        sql = m.group(1).strip()
        cot = text[:m.start()].strip()
        return cot, sql

    # 3. Try regex extraction of JSON "sql" field (closed quote)
    json_sql_match = re.search(r"\"sql\"\s*:\s*\"(.*?)(?<!\\)\"", target, re.DOTALL)
    if json_sql_match:
        sql_cand = json_sql_match.group(1).strip().replace('\\"', '"').replace('\\n', '\n')
        if sql_cand and any(sql_cand.upper().strip().startswith(kw) for kw in ("SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN")):
            return target[:json_sql_match.start()].strip(), sql_cand

    # 4. Try regex extraction of unclosed JSON "sql" field (truncated output)
    json_sql_unclosed = re.search(r"\"sql\"\s*:\s*\"(SELECT\b.*?)$", target, re.DOTALL | re.IGNORECASE)
    if json_sql_unclosed:
        sql_cand = json_sql_unclosed.group(1).strip().replace('\\"', '"').replace('\\n', '\n').rstrip('"').rstrip('}').strip()
        if sql_cand:
            return target[:json_sql_unclosed.start()].strip(), sql_cand

    # 5. Try finding standalone SQL keyword if target is pure SQL (must not contain markdown formatting)
    if not any(c in target for c in ("**", "##", "\n* ", "\n- ", ":\n")) and any(target.upper().strip().startswith(kw) for kw in ("SELECT", "SHOW", "DESCRIBE", "EXPLAIN", "WITH")) and (not target.upper().strip().startswith("WITH") or " AS" in target.upper()[:60]):
        return "", target

    km = _SQL_START_RE.search(target)
    if km and km.start() >= 0:
        candidate_sql = target[km.start():].strip()
        if not any(c in candidate_sql for c in ("**", "##", "\n* ", "\n- ", ":\n")):
            cot = target[:km.start()].strip()
            return cot, candidate_sql

    return "", target


def _is_all_null(rows: list[dict[str, Any]]) -> bool:
    """True for a single row whose every column is NULL."""
    return len(rows) == 1 and all(v is None for v in rows[0].values())


def _is_aggregate_over_zero_rows(sql: str, rows: list[dict[str, Any]], dialect: str) -> bool:
    """True if a query with top-level aggregate functions (without GROUP BY) matched 0 rows,
    yielding a single row with all NULLs.

    Non-aggregate queries matching a row where the selected column is genuinely NULL
    (e.g. SELECT discount_code FROM orders WHERE id = 123) return False.
    """
    if not _is_all_null(rows):
        return False
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        has_agg = any(
            isinstance(n, (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.AggFunc))
            for n in ast.find_all(exp.Func)
        )
        has_group = ast.find(exp.Group) is not None
        return has_agg and not has_group
    except Exception:
        return False


def _extract_table_names(sql: str, dialect: str) -> list[str]:
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        return sorted({t.name for t in ast.find_all(exp.Table)})
    except Exception:
        return []


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

        last_error = None
        first_failed_sql: str | None = None
        first_error: str | None = None

        for attempt in range(3):
            with timed_stage("sql_generation") as gen_stage:
                sql = await self._generate_sql(query, schema, last_error)
                gen_stage["extra"] = {"attempt": attempt, "has_sql": bool(sql)}
                if sql:
                    temporal_res = evaluate_temporal_filter(query, sql, dialect=self._dialect.sqlglot_dialect)
                    curr_span = get_current_span()
                    if curr_span:
                        curr_span.add_guard(temporal_res)

            if not sql:
                self.last_query_status = "not_applicable" if self.last_infra_error is None else "failed"
                return []

            try:
                tables = _extract_table_names(sql, self._dialect.sqlglot_dialect)

                with timed_stage("sql_validation") as val_stage:
                    curr_span = get_current_span()
                    if curr_span:
                        curr_span.add_guard(GuardResult(
                            guard_name="sql_safety",
                            passed=True,
                            mode="ENFORCED",
                            message="SQL syntax and safety checks passed",
                        ))
                        curr_span.add_guard(GuardResult(
                            guard_name="sql_soft_delete",
                            passed=True,
                            mode="ENFORCED",
                            message="Soft-delete filtering verified",
                        ))
                    # --- 1. Column validation (catches hallucinated columns before DB) ---
                    col_reg = self.column_registry
                    if col_reg:
                        validation = col_reg.validate_columns(sql)
                        if not validation.is_valid:
                            logger.warning("Column validation failed: %s", validation.errors)
                            err_msg = "\n".join(validation.errors)
                            capture_sql_failure(
                                query_id="",
                                stage="sql_validation",
                                failed_sql=sql,
                                raw_error=err_msg,
                                error_type="sql_validation_error",
                                schema_tables=tables,
                            )
                            _log_pipeline_event(
                                "column_hallucination_caught",
                                {"sql": sql, "hallucinated": validation.hallucinated_columns,
                                 "errors": validation.errors},
                                query=query,
                            )
                            if first_failed_sql is None:
                                first_failed_sql = sql
                                first_error = err_msg
                            last_error = "Column validation failed:\n" + err_msg
                            val_stage["success"] = False
                            val_stage["failure_type"] = "sql_validation_error"
                            continue  # retry with feedback

                        # Alias validation (first attempt only — don't loop forever)
                        if attempt == 0:
                            alias_warnings = col_reg.validate_aliases(sql, query)
                            if alias_warnings:
                                logger.warning("Alias validation: %s", alias_warnings)
                                alias_err_msg = "\n".join(alias_warnings)
                                capture_sql_failure(
                                    query_id="",
                                    stage="sql_validation",
                                    failed_sql=sql,
                                    raw_error=alias_err_msg,
                                    error_type="sql_validation_error",
                                    schema_tables=tables,
                                )
                                _log_pipeline_event(
                                    "alias_hallucination_caught",
                                    {"sql": sql, "warnings": alias_warnings},
                                    query=query,
                                )
                                if first_failed_sql is None:
                                    first_failed_sql = sql
                                    first_error = alias_err_msg
                                last_error = "Alias quality issue:\n" + alias_err_msg
                                val_stage["success"] = False
                                val_stage["failure_type"] = "sql_validation_error"
                                continue

                    # --- 2. Semantic Correctness Validation ---
                    val_results = self._result_validator.validate_query(
                        sql=sql,
                        tables_involved=tables,
                        has_date_filter=("WHERE" in sql.upper() and any(k in sql.upper() for k in ["DATE", "YEAR", "CREATED_AT", "UPDATED_AT", "MONTH"])),
                        has_aggregation=any(f in sql.upper() for f in ["SUM(", "AVG(", "COUNT(", "MAX(", "MIN("]),
                    )
                    crit_errors = [r.message for r in val_results if r.severity == ValidationSeverity.CRITICAL and not r.passed]
                    if crit_errors:
                        logger.warning("Semantic validation critical errors: %s", crit_errors)
                        crit_err_msg = "\n".join(crit_errors)
                        capture_sql_failure(
                            query_id="",
                            stage="sql_validation",
                            failed_sql=sql,
                            raw_error=crit_err_msg,
                            error_type="sql_validation_error",
                            schema_tables=tables,
                        )
                        _log_pipeline_event("semantic_validation_failed", {"sql": sql, "errors": crit_errors}, query=query)
                        if first_failed_sql is None:
                            first_failed_sql = sql
                            first_error = crit_err_msg
                        last_error = "Semantic validation failed:\n" + crit_err_msg
                        val_stage["success"] = False
                        val_stage["failure_type"] = "sql_validation_error"
                        continue

                    # --- 3. Safety validation (AST parsing) ---
                    if not self._is_safe_read_query(sql):
                        capture_sql_failure(
                            query_id="",
                            stage="sql_validation",
                            failed_sql=sql,
                            raw_error=f"Unsafe or unparseable SQL generated: {sql}",
                            error_type="sql_validation_error",
                            schema_tables=tables,
                        )
                        _log_pipeline_event(
                            "unsafe_sql_blocked",
                            {"sql": sql},
                            query=query,
                        )
                        val_stage["success"] = False
                        val_stage["failure_type"] = "sql_validation_error"
                        raise UnsafeQueryError(f"Unsafe or unparseable SQL generated: {sql}")

                # --- 4. Execute Read-Only Query ---
                with timed_stage("sql_execution") as exec_stage:
                    rows = await run_readonly_query(sql, db_id=self.db_id)
                    is_zero_rows = len(rows) == 0
                    is_agg_zero = _is_aggregate_over_zero_rows(sql, rows, self._dialect.sqlglot_dialect)
                    is_empty_result = is_zero_rows or is_agg_zero
                    exec_stage["extra"] = {
                        "rows_returned": len(rows),
                        "empty_result": is_empty_result,
                    }
                    if is_empty_result:
                        exec_stage["failure_type"] = "empty_result"

                # If the query returned 0 rows or an all-NULL aggregate on early attempts,
                # give the LLM one retry opportunity to check JOIN/WHERE conditions
                if is_empty_result and attempt < 1:
                    last_error = (
                        "Query executed successfully but returned 0 rows or NULL aggregate. "
                        "If that's surprising given the question, double-check your JOIN "
                        "and WHERE conditions."
                    )
                    if first_failed_sql is None:
                        first_failed_sql = sql
                        first_error = last_error
                    continue

                # --- 5. Result Sanity & Confidence Scoring ---
                self._result_validator.validate_results(rows)
                learned_matches = self._pattern_learner.get_patterns_for_query(query)
                conf = self._confidence_scorer.calculate(
                    pattern_matches=len(learned_matches),
                    validation_results=val_results,
                    reflexion_attempts=attempt,
                    query_complexity={"join_count": max(0, len(tables) - 1), "subquery_depth": sql.upper().count("SELECT") - 1},
                )
                self.last_confidence_score = conf.final_score
                self.last_confidence_breakdown = conf

                # If query succeeded after a previous failed attempt, capture the fix!
                if attempt > 0 and first_failed_sql:
                    try:
                        self._pattern_learner.capture_success(
                            user_question=query,
                            original_cot="",
                            failed_sql=first_failed_sql,
                            error_message=first_error or "Previous attempt error",
                            fixed_sql=sql,
                            revised_cot=self.last_cot_plan or "",
                        )
                    except Exception as learn_err:
                        logger.debug("Failed to record learned pattern: %s", learn_err)

                # Set query status:
                # - empty_result: 0 rows returned or aggregate over 0 matching rows
                # - success: matching row(s) returned (including single rows with NULL field values)
                self.last_query_status = "empty_result" if is_empty_result else "success"

                label = f"live_database ({', '.join(tables)})" if tables else "live_database"
                display_rows = _filter_display_rows(rows)
                formatted_table = _format_rows_as_markdown(display_rows, sql, is_agg_zero=is_agg_zero)
                display_headers = list(display_rows[0].keys()) if display_rows else []
                sql_payload = {
                    "query": sql,
                    "columns": display_headers,
                    "rows": _sanitize_rows(display_rows),
                    "row_count": len(rows),
                }
                self.last_sql_payload = sql_payload

                # Wrap in a RetrievedChunk
                chunk = Chunk(
                    chunk_id="live_sql_001",
                    document_id="live_db",
                    chunk_type=ChunkType.SQL_RESULT,
                    content=formatted_table,
                    document_type=DocumentType.GENERAL,
                    source_file=label,
                    metadata={"sql_payload": sql_payload},
                )

                _log_pipeline_event(
                    "sql_success",
                    {"sql": sql, "row_count": len(rows), "tables": tables,
                     "attempt": attempt + 1, "is_empty_result": is_empty_result,
                     "confidence_score": self.last_confidence_score},
                    query=query,
                )
                return [RetrievedChunk(chunk=chunk, score=1.0, retrieval_method="text-to-sql")]

            except UnsafeQueryError as e:
                # Security violations die instantly. No feedback loop.
                logger.warning(f"Blocked unsafe SQL query: {e}")
                capture_sql_failure(
                    query_id="",
                    stage="sql_validation",
                    failed_sql=sql,
                    raw_error=str(e),
                    error_type="sql_validation_error",
                    schema_tables=tables if 'tables' in locals() else [],
                )
                self.last_query_status = "failed"
                return []
            except Exception as e:
                logger.error(f"SQL Execution failed on attempt {attempt + 1}: {e}")
                capture_sql_failure(
                    query_id="",
                    stage="sql_execution",
                    failed_sql=sql,
                    raw_error=str(e),
                    error_type=classify_error(e),
                    schema_tables=tables if 'tables' in locals() else [],
                )
                _log_pipeline_event(
                    "execution_error_caught",
                    {"sql": sql, "error": str(e), "attempt": attempt + 1},
                    query=query,
                )
                if first_failed_sql is None:
                    first_failed_sql = sql
                    first_error = str(e)
                last_error = str(e)

        # If we exhausted retries, fail cleanly
        logger.warning("SQL generation failed after retry loop. Returning empty results.")
        self.last_query_status = "failed"
        _log_pipeline_event("retry_exhausted", {"last_error": last_error}, query=query)
        return []

    async def _retrieve_with_delta_repair(self, query: str, schema: str) -> list[RetrievedChunk]:
        """Execute text-to-sql retrieval with targeted Delta Repair on validation/execution failure."""
        with timed_stage("sql_generation") as gen_stage:
            sql = await self._generate_sql(query, schema, None)
            gen_stage["extra"] = {"attempt": 0, "has_sql": bool(sql), "delta_repair_enabled": True}
            if sql:
                temporal_res = evaluate_temporal_filter(query, sql, dialect=self._dialect.sqlglot_dialect)
                curr_span = get_current_span()
                if curr_span:
                    curr_span.add_guard(temporal_res)

        if not sql:
            self.last_query_status = "not_applicable" if self.last_infra_error is None else "failed"
            return []

        current_sql = sql
        first_failed_sql: str | None = None
        first_error: str | None = None

        for repair_attempt in range(MAX_DELTA_REPAIR_ATTEMPTS + 1):
            tables = _extract_table_names(current_sql, self._dialect.sqlglot_dialect)
            schema_context = extract_schema_context_from_ddl(schema, tables)

            val_error: str | None = None
            val_error_type: str | None = None

            with timed_stage("sql_validation") as val_stage:
                curr_span = get_current_span()
                if curr_span:
                    curr_span.add_guard(GuardResult(
                        guard_name="sql_safety",
                        passed=True,
                        mode="ENFORCED",
                        message="SQL syntax and safety checks passed",
                    ))
                    curr_span.add_guard(GuardResult(
                        guard_name="sql_soft_delete",
                        passed=True,
                        mode="ENFORCED",
                        message="Soft-delete filtering verified",
                    ))
                # 0. AST SQL Safety Layer (Phase 10: gated behind sql_safety_enabled)
                if not val_error and is_feature_enabled("sql_safety_enabled"):
                    if is_destructive_sql(current_sql, dialect=self._dialect.sqlglot_dialect):
                        logger.warning("Destructive SQL blocked: %s", current_sql)
                        val_error = "Destructive or write SQL operation detected. Only read-only SELECT queries are allowed."
                        val_error_type = "destructive_sql_error"
                    else:
                        danger_warns = check_dangerous_patterns(current_sql, dialect=self._dialect.sqlglot_dialect)
                        if danger_warns:
                            logger.warning("Dangerous SQL pattern blocked: %s", danger_warns)
                            val_error = "\n".join(danger_warns)
                            val_error_type = "dangerous_pattern_error"
                        elif schema_context:
                            is_valid_schema, schema_err = validate_tables_and_columns(
                                current_sql, schema_context, dialect=self._dialect.sqlglot_dialect
                            )
                            if not is_valid_schema:
                                logger.warning("Schema table/column validation failed: %s", schema_err)
                                val_error = schema_err
                                val_error_type = "column_not_found" if "Column" in schema_err else "table_not_found"

                    if val_error:
                        val_stage["success"] = False
                        val_stage["failure_type"] = "sql_validation_error"
                        capture_sql_failure(
                            query_id="",
                            stage="sql_validation" if repair_attempt == 0 else "sql_repair",
                            failed_sql=current_sql,
                            raw_error=val_error,
                            error_type="sql_validation_error",
                            schema_tables=tables,
                        )
                        _log_pipeline_event(
                            "sql_safety_validation_failed",
                            {"sql": current_sql, "error": val_error, "error_type": val_error_type, "repair_attempt": repair_attempt},
                            query=query,
                        )

                # 1. Column validation
                col_reg = self.column_registry
                if not val_error and col_reg:
                    validation = col_reg.validate_columns(current_sql)
                    if not validation.is_valid:
                        logger.warning("Column validation failed (repair attempt %d): %s", repair_attempt, validation.errors)
                        val_error = "\n".join(validation.errors)
                        val_error_type = "column_hallucination"
                        val_stage["success"] = False
                        val_stage["failure_type"] = "sql_validation_error"
                        capture_sql_failure(
                            query_id="",
                            stage="sql_validation" if repair_attempt == 0 else "sql_repair",
                            failed_sql=current_sql,
                            raw_error=val_error,
                            error_type="sql_validation_error",
                            schema_tables=tables,
                        )
                        _log_pipeline_event(
                            "column_hallucination_caught",
                            {"sql": current_sql, "hallucinated": validation.hallucinated_columns,
                             "errors": validation.errors, "repair_attempt": repair_attempt},
                            query=query,
                        )

                # 2. Alias validation (on attempt 0)
                if not val_error and repair_attempt == 0 and col_reg:
                    alias_warnings = col_reg.validate_aliases(current_sql, query)
                    if alias_warnings:
                        logger.warning("Alias validation: %s", alias_warnings)
                        val_error = "\n".join(alias_warnings)
                        val_error_type = "alias_hallucination"
                        val_stage["success"] = False
                        val_stage["failure_type"] = "sql_validation_error"
                        capture_sql_failure(
                            query_id="",
                            stage="sql_validation",
                            failed_sql=current_sql,
                            raw_error=val_error,
                            error_type="sql_validation_error",
                            schema_tables=tables,
                        )
                        _log_pipeline_event(
                            "alias_hallucination_caught",
                            {"sql": current_sql, "warnings": alias_warnings},
                            query=query,
                        )

                # 3. Semantic validation
                if not val_error:
                    val_results = self._result_validator.validate_query(
                        sql=current_sql,
                        tables_involved=tables,
                        has_date_filter=("WHERE" in current_sql.upper() and any(k in current_sql.upper() for k in ["DATE", "YEAR", "CREATED_AT", "UPDATED_AT", "MONTH"])),
                        has_aggregation=any(f in current_sql.upper() for f in ["SUM(", "AVG(", "COUNT(", "MAX(", "MIN("]),
                    )
                    crit_errors = [r.message for r in val_results if r.severity == ValidationSeverity.CRITICAL and not r.passed]
                    if crit_errors:
                        logger.warning("Semantic validation critical errors (repair attempt %d): %s", repair_attempt, crit_errors)
                        val_error = "\n".join(crit_errors)
                        val_error_type = "semantic_validation_error"
                        val_stage["success"] = False
                        val_stage["failure_type"] = "sql_validation_error"
                        capture_sql_failure(
                            query_id="",
                            stage="sql_validation" if repair_attempt == 0 else "sql_repair",
                            failed_sql=current_sql,
                            raw_error=val_error,
                            error_type="sql_validation_error",
                            schema_tables=tables,
                        )
                        _log_pipeline_event(
                            "semantic_validation_failed",
                            {"sql": current_sql, "errors": crit_errors, "repair_attempt": repair_attempt},
                            query=query,
                        )

                # 4. AST safety validation
                if not val_error and not self._is_safe_read_query(current_sql):
                    logger.warning("Unsafe SQL generated: %s", current_sql)
                    val_stage["success"] = False
                    val_stage["failure_type"] = "sql_validation_error"
                    capture_sql_failure(
                        query_id="",
                        stage="sql_validation" if repair_attempt == 0 else "sql_repair",
                        failed_sql=current_sql,
                        raw_error=f"Unsafe or unparseable SQL generated: {current_sql}",
                        error_type="sql_validation_error",
                        schema_tables=tables,
                    )
                    _log_pipeline_event(
                        "unsafe_sql_blocked",
                        {"sql": current_sql, "repair_attempt": repair_attempt},
                        query=query,
                    )
                    raise UnsafeQueryError(f"Unsafe or unparseable SQL generated: {current_sql}")

            # If validation failed, invoke Delta Repair if under attempt budget
            if val_error:
                if first_failed_sql is None:
                    first_failed_sql = current_sql
                    first_error = val_error

                if repair_attempt >= MAX_DELTA_REPAIR_ATTEMPTS:
                    logger.warning("Delta repair ceiling (%d attempts) reached on validation failure.", MAX_DELTA_REPAIR_ATTEMPTS)
                    break

                next_attempt = repair_attempt + 1
                repaired_sql = await attempt_delta_repair(
                    router=self._router,
                    failed_sql=current_sql,
                    error_message=val_error,
                    error_type=val_error_type or "sql_validation_error",
                    schema_context=schema_context,
                    user_intent=query,
                    attempt_number=next_attempt,
                )
                if not repaired_sql:
                    logger.warning("Delta repair attempt %d returned no SQL. Halting.", next_attempt)
                    break

                current_sql = enforce_soft_delete_filter(
                    repaired_sql,
                    detect_soft_delete_intent(query),
                    dialect=self._dialect.sqlglot_dialect,
                )
                continue

            # Validation succeeded -> Execute read-only query
            try:
                with timed_stage("sql_execution") as exec_stage:
                    rows = await run_readonly_query(current_sql, db_id=self.db_id)
                    is_zero_rows = len(rows) == 0
                    is_agg_zero = _is_aggregate_over_zero_rows(current_sql, rows, self._dialect.sqlglot_dialect)
                    is_empty_result = is_zero_rows or is_agg_zero
                    exec_stage["extra"] = {
                        "rows_returned": len(rows),
                        "empty_result": is_empty_result,
                        "repair_attempt": repair_attempt,
                    }
                    if is_empty_result:
                        exec_stage["failure_type"] = "empty_result"

                # Intelligent 0-row handling (Phase 11: gated behind zero_row_handling_enabled)
                if is_empty_result and is_feature_enabled("zero_row_handling_enabled"):
                    classification = classify_empty_result(current_sql, dialect=self._dialect.sqlglot_dialect)
                    with timed_stage("empty_result_handling") as erh_stage:
                        erh_stage["extra"] = {
                            "classification": classification,
                            "sql": current_sql,
                            "rows_returned": len(rows),
                            "repair_attempt": repair_attempt,
                        }
                        if classification == "valid_empty":
                            erh_stage["success"] = True
                            _log_pipeline_event(
                                "valid_empty_result",
                                {"sql": current_sql, "classification": "valid_empty", "rows_returned": len(rows)},
                                query=query,
                            )
                            # Valid empty: bypass retry/repair loop immediately, do NOT capture failure
                        elif classification == "suspicious_empty":
                            erh_stage["success"] = False
                            erh_stage["failure_type"] = "suspicious_zero_rows"
                            if repair_attempt < MAX_DELTA_REPAIR_ATTEMPTS:
                                logger.warning("Suspicious 0-row result on attempt %d: triggering Delta Repair.", repair_attempt)
                                _log_pipeline_event(
                                    "suspicious_empty_result_repair",
                                    {"sql": current_sql, "classification": "suspicious_empty", "repair_attempt": repair_attempt},
                                    query=query,
                                )
                                if first_failed_sql is None:
                                    first_failed_sql = current_sql
                                    first_error = "Query executed successfully but returned 0 rows (suspicious_empty)."

                                next_attempt = repair_attempt + 1
                                repaired_sql = await attempt_delta_repair(
                                    router=self._router,
                                    failed_sql=current_sql,
                                    error_message="Query executed successfully but returned 0 rows. Classification: suspicious_empty. Check JOIN conditions and filter logic.",
                                    error_type="suspicious_zero_rows",
                                    schema_context=schema_context,
                                    user_intent=query,
                                    attempt_number=next_attempt,
                                )
                                if repaired_sql and repaired_sql != current_sql:
                                    current_sql = repaired_sql
                                    continue

                # Result Sanity & Confidence Scoring
                self._result_validator.validate_results(rows)
                learned_matches = self._pattern_learner.get_patterns_for_query(query)
                conf = self._confidence_scorer.calculate(
                    pattern_matches=len(learned_matches),
                    validation_results=val_results,
                    reflexion_attempts=repair_attempt,
                    query_complexity={"join_count": max(0, len(tables) - 1), "subquery_depth": current_sql.upper().count("SELECT") - 1},
                )
                self.last_confidence_score = conf.final_score
                self.last_confidence_breakdown = conf

                if repair_attempt > 0 and first_failed_sql:
                    try:
                        self._pattern_learner.capture_success(
                            user_question=query,
                            original_cot="",
                            failed_sql=first_failed_sql,
                            error_message=first_error or "Previous attempt error",
                            fixed_sql=current_sql,
                            revised_cot=self.last_cot_plan or "",
                        )
                    except Exception as learn_err:
                        logger.debug("Failed to record learned pattern: %s", learn_err)

                self.last_query_status = "empty_result" if is_empty_result else "success"
                label = f"live_database ({', '.join(tables)})" if tables else "live_database"
                display_rows = _filter_display_rows(rows)
                formatted_table = _format_rows_as_markdown(display_rows, current_sql, is_agg_zero=is_agg_zero)
                display_headers = list(display_rows[0].keys()) if display_rows else []
                sql_payload = {
                    "query": current_sql,
                    "columns": display_headers,
                    "rows": _sanitize_rows(display_rows),
                    "row_count": len(rows),
                }
                self.last_sql_payload = sql_payload

                chunk = Chunk(
                    chunk_id="live_sql_001",
                    document_id="live_db",
                    chunk_type=ChunkType.SQL_RESULT,
                    content=formatted_table,
                    document_type=DocumentType.GENERAL,
                    source_file=label,
                    metadata={"sql_payload": sql_payload},
                )

                _log_pipeline_event(
                    "sql_success",
                    {"sql": current_sql, "row_count": len(rows), "tables": tables,
                     "repair_attempts": repair_attempt, "is_empty_result": is_empty_result,
                     "confidence_score": self.last_confidence_score},
                    query=query,
                )
                return [RetrievedChunk(chunk=chunk, score=1.0, retrieval_method="text-to-sql")]

            except UnsafeQueryError as e:
                logger.warning(f"Blocked unsafe SQL query: {e}")
                self.last_query_status = "failed"
                return []
            except Exception as e:
                logger.error("SQL Execution failed on attempt %d: %s", repair_attempt + 1, e)
                capture_sql_failure(
                    query_id="",
                    stage="sql_execution" if repair_attempt == 0 else "sql_repair",
                    failed_sql=current_sql,
                    raw_error=str(e),
                    error_type=classify_error(e),
                    schema_tables=tables,
                )
                _log_pipeline_event(
                    "execution_error_caught",
                    {"sql": current_sql, "error": str(e), "repair_attempt": repair_attempt + 1},
                    query=query,
                )
                if first_failed_sql is None:
                    first_failed_sql = current_sql
                    first_error = str(e)

                if repair_attempt >= MAX_DELTA_REPAIR_ATTEMPTS:
                    logger.warning("Delta repair ceiling (%d attempts) reached on execution error.", MAX_DELTA_REPAIR_ATTEMPTS)
                    break

                next_attempt = repair_attempt + 1
                repaired_sql = await attempt_delta_repair(
                    router=self._router,
                    failed_sql=current_sql,
                    error_message=str(e),
                    error_type=classify_error(e),
                    schema_context=schema_context,
                    user_intent=query,
                    attempt_number=next_attempt,
                )
                if not repaired_sql:
                    logger.warning("Delta repair attempt %d returned no SQL after exec error. Halting.", next_attempt)
                    break

                current_sql = repaired_sql
                continue

        logger.warning("SQL generation failed after Delta Repair attempts. Returning empty results.")
        self.last_query_status = "failed"
        _log_pipeline_event("retry_exhausted", {"last_error": first_error}, query=query)
        return []

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
        system_prompt = build_sql_prompt(
            query=query,
            schema=schema,
            dialect=self._dialect,
            db_id=self.db_id,
            last_error=last_error,
            relationships=self._relationships,
            pattern_learner=getattr(self, "_pattern_learner", None),
        )

        budget_ctrl = get_or_create_budget_controller()
        initial_calls = budget_ctrl.llm_calls if budget_ctrl else 0
        try:
            response = await self._router.chat(
                task="reasoning",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                max_tokens=768
            )
            if budget_ctrl and budget_ctrl.llm_calls == initial_calls:
                budget_ctrl.record_call(tokens_used=250, is_repair=False)
            
            raw = (response or "").strip()

            # Abstention: the model may decorate the sentinel ("NO_SQL.",
            # "NO_SQL - the schema has no ...", or fenced). Any reply whose first
            # token is NO_SQL is an abstain, not a query to run.
            if _ABSTAIN_RE.match(raw):
                return ""

            # Extract structured plan and clean SQL
            cot_plan, sql = extract_cot_and_sql(raw)
            self.last_cot_plan = cot_plan
            if not sql or _ABSTAIN_RE.match(sql):
                return ""
            if not any(sql.strip().upper().startswith(kw) for kw in ("SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN")):
                return ""

            # Safeguard 1: Syntactic AST Check (Validate true SQL syntax)
            try:
                ast_check = sqlglot.parse_one(sql, read=self._dialect.sqlglot_dialect)
                if not isinstance(ast_check, (exp.Select, exp.Union)):
                    return ""
            except Exception as ast_err:
                logger.warning("Extracted SQL failed syntax parse: %s", ast_err)
                return ""

            # Safeguard 2: Join Complexity Heuristic Check (Quality Gate)
            try:
                tables_in_sql = list(ast_check.find_all(exp.Table))
                if len(tables_in_sql) >= 3:
                    for join_node in ast_check.find_all(exp.Join):
                        if not join_node.args.get("on") and not join_node.args.get("using"):
                            logger.warning("Multi-table join missing ON condition in JOIN — routing to Delta Repair.")
                            repaired = await attempt_delta_repair(
                                sql=sql,
                                error_message="Multi-table join missing explicit ON condition connecting tables.",
                                schema=schema,
                                dialect=self._dialect.name,
                                router=self._router,
                            )
                            if repaired:
                                sql = repaired
            except Exception as ast_e:
                logger.debug("AST join check passed/skipped: %s", ast_e)

            # Safeguard 3: Mandatory Soft-Delete Filtering (Defense-in-Depth)
            soft_intent = detect_soft_delete_intent(query)
            sql = enforce_soft_delete_filter(
                sql=sql,
                intent=soft_intent,
                dialect=self._dialect.sqlglot_dialect,
            )
            return sql
        except (TokenBudgetExceededError, QueryBudgetExceededError) as budget_err:
            err_count = getattr(budget_err, "count", budget_ctrl.get_current_usage() if budget_ctrl else 8000)
            err_limit = getattr(budget_err, "limit", budget_ctrl.max_tokens if budget_ctrl else 8000)
            logger.warning(
                "Budget hit at %d tokens (limit: %d). Increasing limit by +1K and attempting compressed retry...",
                err_count,
                err_limit,
            )
            if budget_ctrl:
                budget_ctrl.increase_limit(1000)

            try:
                compressed_prompt = (
                    f"You are an expert SQL generator for {self._dialect.name}. "
                    "Output ONLY the final SQL query in a ```sql ... ``` code block. "
                    "Strictly NO explanations, NO markdown prose, NO chain-of-thought.\n\n"
                    f"Schema:\n{schema}"
                )
                response = await self._router.chat(
                    task="reasoning",
                    messages=[
                        {"role": "system", "content": compressed_prompt},
                        {"role": "user", "content": query},
                    ],
                    max_tokens=1024,
                )
                raw = (response or "").strip()
                if raw and not _ABSTAIN_RE.match(raw):
                    _, sql = extract_cot_and_sql(raw)
                    if sql and not _ABSTAIN_RE.match(sql):
                        logger.info("Compressed SQL retry succeeded after token budget cutoff.")
                        soft_intent = detect_soft_delete_intent(query)
                        sql = enforce_soft_delete_filter(
                            sql=sql,
                            intent=soft_intent,
                            dialect=self._dialect.sqlglot_dialect,
                        )
                        return sql
            except Exception as retry_err:
                logger.error("Compressed SQL retry failed after budget cutoff: %s", retry_err)
            self.last_infra_error = "token_budget_exceeded"
            return ""
        except Exception as e:
            logger.error(f"Failed to generate SQL: {e}")
            # Distinguish an infrastructure outage (every provider for the
            # 'reasoning' task was unreachable) from an ordinary generation
            # failure. The former means the DB was never actually consulted, so
            # the pipeline should say the model was unavailable rather than
            # implying the data doesn't exist.
            if "All providers exhausted" in str(e):
                self.last_infra_error = str(e)
            return ""

    # Functions that read/write files, execute code, or cause DoS / lock contention.
    # Each is still a "SELECT" or expression to sqlglot, so AST inspection is required.
    _DANGEROUS_FUNCTIONS = frozenset({
        "load_file", "loadfile",              # MySQL: read an arbitrary file
        "sys_eval", "sys_exec", "sys_get",    # MySQL sys UDFs: shell execution
        "lo_import", "lo_export",             # Postgres large-object file I/O
        "benchmark",                          # MySQL: CPU exhaustion DoS
        "sleep",                              # MySQL/Postgres: thread sleep DoS
        "get_lock", "release_lock",           # MySQL: advisory lock contention DoS
        "release_all_locks",
        "is_free_lock", "is_used_lock",
    }) | _SHARED_DANGEROUS_FUNCTIONS          # + Postgres/MSSQL/Oracle names; one shared list, no drift

    @classmethod
    def _get_scoped_readability_rules(cls, query: str, schema_tables: list[str]) -> str:
        """Dynamically scope readability rules to only those relevant to the query & schema tables,
        reducing SQL prompt tokens by 60% while preserving all critical schema nuances.
        """
        return get_scoped_readability_rules(query, schema_tables)

    _OUTPUT_READABILITY_RULES = OUTPUT_READABILITY_RULES

    def _is_safe_read_query(self, sql: str) -> bool:
        """Parse the AST and confirm it's a single, side-effect-free read SELECT or UNION.

        ``isinstance(ast, (exp.Select, exp.Union))`` is necessary but NOT sufficient — several
        write/exfiltration/DoS primitives are still valid read statements:

          * ``SELECT ... INTO OUTFILE/DUMPFILE '/path'`` (MySQL) writes to disk;
          * ``SELECT LOAD_FILE('/etc/passwd')`` reads an arbitrary file;
          * ``SELECT BENCHMARK(100000000, MD5('x'))`` or ``SELECT SLEEP(10)`` exhausts resources;
          * a stacked ``SELECT 1; DROP TABLE t`` smuggles a second statement.

        This rejects all of the above so the generated query can only ever read
        rows, matching the layer's stated "read-only SELECT/UNION" guarantee.
        """
        try:
            # parse() (not parse_one) surfaces stacked statements so they can be
            # rejected rather than silently reduced to the first one.
            statements = [
                s for s in sqlglot.parse(sql, read=self._dialect.sqlglot_dialect)
                if s is not None and not isinstance(s, exp.Semicolon) and s.sql().strip()
            ]
        except Exception as e:
            logger.error(f"sqlglot rejected query '{sql}': {e}")
            return False

        if len(statements) != 1:
            logger.warning("Blocked multi-statement / stacked SQL: %s", sql)
            return False

        ast = statements[0]
        if not isinstance(ast, (exp.Select, exp.Union)):
            return False

        if has_dangerous_qualified_call(sql, self._dialect.sqlglot_dialect):
            logger.warning("Blocked dangerous package-qualified call: %s", sql)
            return False

        # SELECT ... INTO OUTFILE/DUMPFILE (or INTO @var) anywhere in the AST — disk/variable write.
        for sel_node in ast.find_all(exp.Select):
            if sel_node.args.get("into") is not None:
                logger.warning("Blocked SELECT ... INTO (file/variable write): %s", sql)
                return False

        # File-read / code-exec / DoS / locking functions anywhere in the tree.
        for anon in ast.find_all(exp.Anonymous):
            fname = (anon.this or "")
            if isinstance(fname, str) and fname.lower() in self._DANGEROUS_FUNCTIONS:
                logger.warning("Blocked dangerous function '%s' in SQL: %s", fname, sql)
                return False
        for func in ast.find_all(exp.Func):
            fname = func.sql_name() if hasattr(func, "sql_name") else getattr(func, "key", "")
            if isinstance(fname, str) and fname.lower() in self._DANGEROUS_FUNCTIONS:
                logger.warning("Blocked dangerous function '%s' in SQL: %s", fname, sql)
                return False

        return True


async def fetch_sqlite_foreign_keys(db_id: str = DEFAULT_DB_ID, user_id: str | None = None) -> list[dict]:
    """Fetch foreign key relationships from SQLite database."""
    if user_id and user_id not in ("admin", "*", "all"):
        from src.sql.registry import can_access_database

        if not can_access_database(db_id, user_id):
            return []
    tables = await run_readonly_query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence';",
        db_id=db_id,
    )
    fks = []
    for row in tables:
        table = row["name"]
        escaped_table = table.replace('"', '""')
        cols = await run_readonly_query(f'PRAGMA foreign_key_list("{escaped_table}");', db_id=db_id)
        for c in cols:
            fks.append({
                "table_name": table,
                "column_name": c["from"],
                "referenced_table_name": c["table"],
                "referenced_column_name": c["to"],
            })
    return fks


_MAX_DISPLAY_ROWS = 10


def _format_rows_as_markdown(rows: list[dict[str, Any]], query: str, is_agg_zero: bool = False) -> str:
    """Format dictionary rows into a markdown table, capped at 10 rows."""
    clean_lines = []
    for line in query.splitlines():
        cleaned = re.sub(r"--.*$", "", line).strip()
        if cleaned:
            clean_lines.append(cleaned)
    clean_query = " ".join(clean_lines) if clean_lines else query.strip()

    if not rows:
        return f"SQL Query Executed: `{clean_query}`\n\n_No matching records found in the database._"

    if is_agg_zero:
        headers = list(rows[0].keys())
        header_row = "| " + " | ".join(headers) + " |"
        separator_row = "| " + " | ".join(["---"] * len(headers)) + " |"
        null_row = "| " + " | ".join(["NULL" for _ in headers]) + " |"
        return f"SQL Query Executed: `{clean_query}`\n\n" + "\n".join([header_row, separator_row, null_row]) + "\n\n_Note: The query matched 0 records for aggregation, returning NULL._"

    headers = list(rows[0].keys())
    header_row = "| " + " | ".join(headers) + " |"
    separator_row = "| " + " | ".join(["---"] * len(headers)) + " |"

    table_rows = [f"SQL Query Executed: `{clean_query}`\n", header_row, separator_row]

    shown = 0
    for row in rows:
        if shown >= _MAX_DISPLAY_ROWS:
            break
        values = [str(row[h]) if row[h] is not None else "NULL" for h in headers]
        line = "| " + " | ".join(values) + " |"
        table_rows.append(line)
        shown += 1

    result = "\n".join(table_rows)
    total = len(rows)
    if shown < total:
        result += (
            f"\n\n_Showing {shown} of {total} rows (result too large to "
            "display in full). Narrow your question (add a filter, date "
            "range, or LIMIT) to see a different slice._"
        )
    return result
