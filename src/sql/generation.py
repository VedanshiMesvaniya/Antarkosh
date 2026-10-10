"""SQL generation, execution glue, and repair calls.

Extracted from Stage 12b (s12b_sql_retrieval.py) in Step F6.
Handles:
- SQL response parsing, code unwrapping, and CoT separation
- Dialect schema row formatting and SQLite foreign key fetching
- Safe read-only SQL validation and dangerous AST pattern blocking
- LLM SQL generation with soft-delete enforcement and token budget fallback
- Execution retry loop with column/alias/semantic validation
- Targeted Delta Repair loop for validation and execution error recovery
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import json
import logging
import re
import sys
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from src.core.config import settings
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID
from src.core.db_client import run_readonly_query
from src.sql.learning.pipeline_metrics import log_event as _log_pipeline_event
from src.core.provider_client import ProviderRouter
from src.sql.safety.column_registry import ColumnRegistry
from src.core.sql_dialects import SQLDialectProfile, get_dialect_profile
from src.sql.safety.result_validator import ResultValidator, ValidationSeverity
from src.sql.learning.pattern_learner import PatternLearner
from src.sql.safety.confidence_scorer import ConfidenceScorer, ConfidenceBreakdown
from src.models.schemas import Chunk, ChunkType, RetrievedChunk, DocumentType
from src.sql.prompt_builder import build_sql_prompt
from src.sql.soft_delete import detect_soft_delete_intent, enforce_soft_delete_filter
from src.sql.safety.empty_result_classifier import classify_empty_result
from src.utils.error_classification import classify_error
from src.sql.learning.failure_capture import capture_sql_failure
from src.utils.feature_flags import is_feature_enabled
from src.utils.query_budget import QueryBudgetExceededError, get_or_create_budget_controller
from src.utils.stream_token_counter import TokenBudgetExceededError
from src.utils.sql_safety import (
    DANGEROUS_FUNCTIONS as _SHARED_DANGEROUS_FUNCTIONS,
    check_dangerous_patterns,
    has_dangerous_qualified_call,
    is_destructive_sql,
    validate_tables_and_columns,
)
from src.utils.telemetry import timed_stage
from src.guards.temporal_guard import evaluate_temporal_filter
from src.models.trace import GuardResult
from src.utils.trace_context import get_current_span
from src.sql.repair.sql_repair import (
    MAX_DELTA_REPAIR_ATTEMPTS,
    attempt_delta_repair,
    extract_schema_context_from_ddl,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dynamic Patch Hook Helpers
# ---------------------------------------------------------------------------

def _check_feature_enabled(flag: str) -> bool:
    """Check feature flag, honoring mock patches on s12b_sql_retrieval or generation."""
    s12b = sys.modules.get("src.stages.s12b_sql_retrieval")
    if s12b and hasattr(s12b, "is_feature_enabled") and s12b.is_feature_enabled is not _check_feature_enabled:
        try:
            return s12b.is_feature_enabled(flag)
        except Exception:
            pass
    return is_feature_enabled(flag)


def _capture_failure(*args: Any, **kwargs: Any) -> None:
    """Capture failure, honoring mock patches on s12b_sql_retrieval or generation."""
    s12b = sys.modules.get("src.stages.s12b_sql_retrieval")
    if s12b and hasattr(s12b, "capture_sql_failure") and s12b.capture_sql_failure is not _capture_failure:
        try:
            s12b.capture_sql_failure(*args, **kwargs)
            return
        except Exception:
            pass
    capture_sql_failure(*args, **kwargs)


# ---------------------------------------------------------------------------
# Exceptions & Constants
# ---------------------------------------------------------------------------

class UnsafeQueryError(Exception):
    """Raised when sqlglot rejects a query (e.g. not a SELECT). Never retried."""
    pass


# Columns to hide from user-facing display (markdown table + sqlPayload).
DISPLAY_HIDDEN_COLS: frozenset[str] = frozenset({
    "id",
    "created_at",
    "updated_at",
    "deleted_at",
})
_DISPLAY_HIDDEN_COLS = DISPLAY_HIDDEN_COLS

ABSTAIN_RE = re.compile(r"^\W*no_sql\b", re.IGNORECASE)
_ABSTAIN_RE = ABSTAIN_RE
FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)(?:```|$)", re.IGNORECASE | re.DOTALL)
_FENCE_RE = FENCE_RE
SQL_START_RE = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)
_SQL_START_RE = SQL_START_RE

MAX_DISPLAY_ROWS = 10
_MAX_DISPLAY_ROWS = MAX_DISPLAY_ROWS

# Functions that read/write files, execute code, or cause DoS / lock contention.
DANGEROUS_FUNCTIONS: frozenset[str] = frozenset({
    "load_file", "loadfile",              # MySQL: read arbitrary file
    "sys_eval", "sys_exec", "sys_get",    # MySQL sys UDFs: shell execution
    "lo_import", "lo_export",             # Postgres large-object file I/O
    "benchmark",                          # MySQL: CPU exhaustion DoS
    "sleep",                              # MySQL/Postgres: thread sleep DoS
    "get_lock", "release_lock",           # MySQL: advisory lock contention DoS
    "release_all_locks",
    "is_free_lock", "is_used_lock",
}) | _SHARED_DANGEROUS_FUNCTIONS          # + Postgres/MSSQL/Oracle names; one shared list
_DANGEROUS_FUNCTIONS = DANGEROUS_FUNCTIONS


# ---------------------------------------------------------------------------
# Display & Cell Sanitation Helpers
# ---------------------------------------------------------------------------

def filter_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *rows* with hidden metadata columns removed (display only).

    Columns are matched case-insensitively. If ALL columns would be stripped,
    the original rows are returned unchanged so the user sees something meaningful.
    """
    if not rows:
        return rows
    visible_keys = [
        k for k in rows[0].keys()
        if k.lower() not in _DISPLAY_HIDDEN_COLS
    ]
    if not visible_keys:
        return rows
    return [{k: row[k] for k in visible_keys} for row in rows]


_filter_display_rows = filter_display_rows


def sanitize_cell_value(val: Any) -> Any:
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
        return [sanitize_cell_value(v) for v in val]
    if isinstance(val, dict):
        return {k: sanitize_cell_value(v) for k, v in val.items()}
    return str(val)


_sanitize_cell_value = sanitize_cell_value


def sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw DB rows to JSON-safe dictionaries."""
    if not rows:
        return []
    return [
        {k: sanitize_cell_value(v) for k, v in row.items()}
        for row in rows
    ]


_sanitize_rows = sanitize_rows


# ---------------------------------------------------------------------------
# Schema & Foreign Key Formatting Helpers
# ---------------------------------------------------------------------------

def format_schema_rows(profile: SQLDialectProfile, rows: list[dict[str, Any]]) -> str:
    """Turn an engine's raw introspection rows into schema text for the NL2SQL prompt.

    Pure function of (dialect profile, rows) — no instance state, no global settings.
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
    """Format raw foreign key relationship rows."""
    if not rows:
        return ""

    lines = [
        f"  {r['table_name']}.{r['column_name']} -> {r['referenced_table_name']}.{r['referenced_column_name']}"
        for r in rows
    ]
    return "Foreign Keys:\n" + "\n".join(lines)


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


# ---------------------------------------------------------------------------
# SQL Unwrapping & Extraction Helpers
# ---------------------------------------------------------------------------

def unwrap_sql(text: str) -> str:
    """Extract SQL from an LLM response that may wrap it in markdown or reasoning."""
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


_unwrap_sql = unwrap_sql


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


def is_all_null(rows: list[dict[str, Any]]) -> bool:
    """True for a single row whose every column is NULL."""
    return len(rows) == 1 and all(v is None for v in rows[0].values())


_is_all_null = is_all_null


def is_aggregate_over_zero_rows(sql: str, rows: list[dict[str, Any]], dialect: str) -> bool:
    """True if a query with top-level aggregate functions (without GROUP BY) matched 0 rows,
    yielding a single row with all NULLs.
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


_is_aggregate_over_zero_rows = is_aggregate_over_zero_rows


def extract_table_names(sql: str, dialect: str) -> list[str]:
    """Extract list of table names referenced in an SQL query."""
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        return sorted({t.name for t in ast.find_all(exp.Table)})
    except Exception:
        return []


_extract_table_names = extract_table_names


def format_rows_as_markdown(rows: list[dict[str, Any]], query: str, is_agg_zero: bool = False) -> str:
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


_format_rows_as_markdown = format_rows_as_markdown


def is_safe_read_query(
    sql: str,
    dialect: str | SQLDialectProfile = "sqlite",
    dangerous_functions: frozenset[str] | None = None,
) -> bool:
    """Parse the AST and confirm it's a single, side-effect-free read SELECT or UNION."""
    sqlglot_dialect = dialect.sqlglot_dialect if hasattr(dialect, "sqlglot_dialect") else str(dialect)
    funcs = dangerous_functions or _DANGEROUS_FUNCTIONS
    try:
        statements = [
            s for s in sqlglot.parse(sql, read=sqlglot_dialect)
            if s is not None and not isinstance(s, exp.Semicolon) and s.sql().strip()
        ]
    except Exception as e:
        logger.error("sqlglot rejected query '%s': %s", sql, e)
        return False

    if len(statements) != 1:
        logger.warning("Blocked multi-statement / stacked SQL: %s", sql)
        return False

    ast = statements[0]
    if not isinstance(ast, (exp.Select, exp.Union)):
        return False

    if has_dangerous_qualified_call(sql, sqlglot_dialect):
        logger.warning("Blocked dangerous package-qualified call: %s", sql)
        return False

    # SELECT ... INTO OUTFILE/DUMPFILE (or INTO @var) anywhere in AST
    for sel_node in ast.find_all(exp.Select):
        if sel_node.args.get("into") is not None:
            logger.warning("Blocked SELECT ... INTO (file/variable write): %s", sql)
            return False

    # File-read / code-exec / DoS / locking functions anywhere in tree
    for anon in ast.find_all(exp.Anonymous):
        fname = anon.this or ""
        if isinstance(fname, str) and fname.lower() in funcs:
            logger.warning("Blocked dangerous function '%s' in SQL: %s", fname, sql)
            return False
    for func in ast.find_all(exp.Func):
        fname = func.sql_name() if hasattr(func, "sql_name") else getattr(func, "key", "")
        if isinstance(fname, str) and fname.lower() in funcs:
            logger.warning("Blocked dangerous function '%s' in SQL: %s", fname, sql)
            return False

    return True


_is_safe_read_query = is_safe_read_query


# ---------------------------------------------------------------------------
# Core SQL Generation Function
# ---------------------------------------------------------------------------

async def generate_sql(
    router: ProviderRouter,
    query: str,
    schema: str,
    dialect: SQLDialectProfile,
    db_id: str = DEFAULT_DB_ID,
    last_error: str | None = None,
    relationships: str | None = None,
    pattern_learner: Any | None = None,
    budget_controller: Any | None = None,
) -> tuple[str, str | None, str | None]:
    """Prompt the reasoning LLM to generate SQL.

    Returns:
        (sql_query, cot_plan, infra_error)
    """
    system_prompt = build_sql_prompt(
        query=query,
        schema=schema,
        dialect=dialect,
        db_id=db_id,
        last_error=last_error,
        relationships=relationships,
        pattern_learner=pattern_learner,
    )

    budget_ctrl = budget_controller or get_or_create_budget_controller()
    initial_calls = budget_ctrl.llm_calls if budget_ctrl else 0
    try:
        response = await router.chat(
            task="reasoning",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            max_tokens=768,
        )
        if budget_ctrl and budget_ctrl.llm_calls == initial_calls:
            budget_ctrl.record_call(tokens_used=250, is_repair=False)

        raw = (response or "").strip()

        # Abstention check
        if _ABSTAIN_RE.match(raw):
            return "", None, None

        cot_plan, sql = extract_cot_and_sql(raw)
        if not sql or _ABSTAIN_RE.match(sql):
            return "", cot_plan, None
        if not any(sql.strip().upper().startswith(kw) for kw in ("SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN")):
            return "", cot_plan, None

        # Safeguard 1: Syntactic AST Check
        try:
            ast_check = sqlglot.parse_one(sql, read=dialect.sqlglot_dialect)
            if not isinstance(ast_check, (exp.Select, exp.Union)):
                return "", cot_plan, None
        except Exception as ast_err:
            logger.warning("Extracted SQL failed syntax parse: %s", ast_err)
            return "", cot_plan, None

        # Safeguard 2: Join Complexity Heuristic Check
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
                            dialect=dialect.name,
                            router=router,
                        )
                        if repaired:
                            sql = repaired
        except Exception as ast_e:
            logger.debug("AST join check passed/skipped: %s", ast_e)

        # Safeguard 3: Mandatory Soft-Delete Filtering
        soft_intent = detect_soft_delete_intent(query)
        sql = enforce_soft_delete_filter(
            sql=sql,
            intent=soft_intent,
            dialect=dialect.sqlglot_dialect,
            db_id=db_id,
        )
        return sql, cot_plan, None

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
                f"You are an expert SQL generator for {dialect.name}. "
                "Output ONLY the final SQL query in a ```sql ... ``` code block. "
                "Strictly NO explanations, NO markdown prose, NO chain-of-thought.\n\n"
                f"Schema:\n{schema}"
            )
            response = await router.chat(
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
                        dialect=dialect.sqlglot_dialect,
                        db_id=db_id,
                    )
                    return sql, None, None
        except Exception as retry_err:
            logger.error("Compressed SQL retry failed after budget cutoff: %s", retry_err)
        return "", None, "token_budget_exceeded"

    except Exception as e:
        logger.error("Failed to generate SQL: %s", e)
        infra_err = str(e) if "All providers exhausted" in str(e) else None
        return "", None, infra_err


# ---------------------------------------------------------------------------
# Execution Glue and Repair Calls
# ---------------------------------------------------------------------------

async def execute_with_retry(
    retriever: Any,
    query: str,
    schema: str,
) -> list[RetrievedChunk]:
    """Execute SQL retrieval with full-context retry loop (up to 3 attempts)."""
    last_error: str | None = None
    first_failed_sql: str | None = None
    first_error: str | None = None

    for attempt in range(3):
        with timed_stage("sql_generation") as gen_stage:
            sql = await retriever._generate_sql(query, schema, last_error)
            gen_stage["extra"] = {"attempt": attempt, "has_sql": bool(sql)}
            if sql:
                temporal_res = evaluate_temporal_filter(query, sql, dialect=retriever._dialect.sqlglot_dialect)
                curr_span = get_current_span()
                if curr_span:
                    curr_span.add_guard(temporal_res)

        if not sql:
            retriever.last_query_status = "not_applicable" if retriever.last_infra_error is None else "failed"
            return []

        try:
            tables = _extract_table_names(sql, retriever._dialect.sqlglot_dialect)

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

                # 1. Column validation
                col_reg = retriever.column_registry
                if col_reg:
                    validation = col_reg.validate_columns(sql)
                    if not validation.is_valid:
                        logger.warning("Column validation failed: %s", validation.errors)
                        err_msg = "\n".join(validation.errors)
                        _capture_failure(
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
                        continue

                    # Alias validation (attempt 0 only)
                    if attempt == 0:
                        alias_warnings = col_reg.validate_aliases(sql, query)
                        if alias_warnings:
                            logger.warning("Alias validation: %s", alias_warnings)
                            alias_err_msg = "\n".join(alias_warnings)
                            _capture_failure(
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

                # 2. Semantic Correctness Validation
                val_results = retriever._result_validator.validate_query(
                    sql=sql,
                    tables_involved=tables,
                    has_date_filter=("WHERE" in sql.upper() and any(k in sql.upper() for k in ["DATE", "YEAR", "CREATED_AT", "UPDATED_AT", "MONTH"])),
                    has_aggregation=any(f in sql.upper() for f in ["SUM(", "AVG(", "COUNT(", "MAX(", "MIN("]),
                )
                crit_errors = [r.message for r in val_results if r.severity == ValidationSeverity.CRITICAL and not r.passed]
                if crit_errors:
                    logger.warning("Semantic validation critical errors: %s", crit_errors)
                    crit_err_msg = "\n".join(crit_errors)
                    _capture_failure(
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

                # 3. Safety validation
                if not retriever._is_safe_read_query(sql):
                    _capture_failure(
                        query_id="",
                        stage="sql_validation",
                        failed_sql=sql,
                        raw_error=f"Unsafe or unparseable SQL generated: {sql}",
                        error_type="sql_validation_error",
                        schema_tables=tables,
                    )
                    _log_pipeline_event("unsafe_sql_blocked", {"sql": sql}, query=query)
                    val_stage["success"] = False
                    val_stage["failure_type"] = "sql_validation_error"
                    raise UnsafeQueryError(f"Unsafe or unparseable SQL generated: {sql}")

            # 4. Execute Read-Only Query
            with timed_stage("sql_execution") as exec_stage:
                rows = await run_readonly_query(sql, db_id=retriever.db_id)
                is_zero_rows = len(rows) == 0
                is_agg_zero = _is_aggregate_over_zero_rows(sql, rows, retriever._dialect.sqlglot_dialect)
                is_empty_result = is_zero_rows or is_agg_zero
                exec_stage["extra"] = {
                    "rows_returned": len(rows),
                    "empty_result": is_empty_result,
                }
                if is_empty_result:
                    exec_stage["failure_type"] = "empty_result"

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

            # 5. Result Sanity & Confidence Scoring
            retriever._result_validator.validate_results(rows)
            learned_matches = retriever._pattern_learner.get_patterns_for_query(query)
            conf = retriever._confidence_scorer.calculate(
                pattern_matches=len(learned_matches),
                validation_results=val_results,
                reflexion_attempts=attempt,
                query_complexity={"join_count": max(0, len(tables) - 1), "subquery_depth": sql.upper().count("SELECT") - 1},
            )
            retriever.last_confidence_score = conf.final_score
            retriever.last_confidence_breakdown = conf

            if attempt > 0 and first_failed_sql:
                try:
                    retriever._pattern_learner.capture_success(
                        user_question=query,
                        original_cot="",
                        failed_sql=first_failed_sql,
                        error_message=first_error or "Previous attempt error",
                        fixed_sql=sql,
                        revised_cot=retriever.last_cot_plan or "",
                    )
                except Exception as learn_err:
                    logger.debug("Failed to record learned pattern: %s", learn_err)

            retriever.last_query_status = "empty_result" if is_empty_result else "success"
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
            retriever.last_sql_payload = sql_payload

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
                 "confidence_score": retriever.last_confidence_score},
                query=query,
            )
            return [RetrievedChunk(chunk=chunk, score=1.0, retrieval_method="text-to-sql")]

        except UnsafeQueryError as e:
            logger.warning("Blocked unsafe SQL query: %s", e)
            _capture_failure(
                query_id="",
                stage="sql_validation",
                failed_sql=sql,
                raw_error=str(e),
                error_type="sql_validation_error",
                schema_tables=tables if "tables" in locals() else [],
            )
            retriever.last_query_status = "failed"
            return []
        except Exception as e:
            logger.error("SQL Execution failed on attempt %d: %s", attempt + 1, e)
            _capture_failure(
                query_id="",
                stage="sql_execution",
                failed_sql=sql,
                raw_error=str(e),
                error_type=classify_error(e),
                schema_tables=tables if "tables" in locals() else [],
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

    logger.warning("SQL generation failed after retry loop. Returning empty results.")
    retriever.last_query_status = "failed"
    _log_pipeline_event("retry_exhausted", {"last_error": last_error}, query=query)
    return []


async def execute_with_delta_repair(
    retriever: Any,
    query: str,
    schema: str,
) -> list[RetrievedChunk]:
    """Execute text-to-sql retrieval with targeted Delta Repair on validation/execution failure."""
    with timed_stage("sql_generation") as gen_stage:
        sql = await retriever._generate_sql(query, schema, None)
        gen_stage["extra"] = {"attempt": 0, "has_sql": bool(sql), "delta_repair_enabled": True}
        if sql:
            temporal_res = evaluate_temporal_filter(query, sql, dialect=retriever._dialect.sqlglot_dialect)
            curr_span = get_current_span()
            if curr_span:
                curr_span.add_guard(temporal_res)

    if not sql:
        retriever.last_query_status = "not_applicable" if retriever.last_infra_error is None else "failed"
        return []

    current_sql = sql
    first_failed_sql: str | None = None
    first_error: str | None = None

    for repair_attempt in range(MAX_DELTA_REPAIR_ATTEMPTS + 1):
        tables = _extract_table_names(current_sql, retriever._dialect.sqlglot_dialect)
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

            # 0. AST SQL Safety Layer
            if not val_error and _check_feature_enabled("sql_safety_enabled"):
                if is_destructive_sql(current_sql, dialect=retriever._dialect.sqlglot_dialect):
                    logger.warning("Destructive SQL blocked: %s", current_sql)
                    val_error = "Destructive or write SQL operation detected. Only read-only SELECT queries are allowed."
                    val_error_type = "destructive_sql_error"
                else:
                    danger_warns = check_dangerous_patterns(current_sql, dialect=retriever._dialect.sqlglot_dialect)
                    if danger_warns:
                        logger.warning("Dangerous SQL pattern blocked: %s", danger_warns)
                        val_error = "\n".join(danger_warns)
                        val_error_type = "dangerous_pattern_error"
                    elif schema_context:
                        is_valid_schema, schema_err = validate_tables_and_columns(
                            current_sql, schema_context, dialect=retriever._dialect.sqlglot_dialect
                        )
                        if not is_valid_schema:
                            logger.warning("Schema table/column validation failed: %s", schema_err)
                            val_error = schema_err
                            val_error_type = "column_not_found" if "Column" in schema_err else "table_not_found"

                if val_error:
                    val_stage["success"] = False
                    val_stage["failure_type"] = "sql_validation_error"
                    _capture_failure(
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
            col_reg = retriever.column_registry
            if not val_error and col_reg:
                validation = col_reg.validate_columns(current_sql)
                if not validation.is_valid:
                    logger.warning("Column validation failed (repair attempt %d): %s", repair_attempt, validation.errors)
                    val_error = "\n".join(validation.errors)
                    val_error_type = "column_hallucination"
                    val_stage["success"] = False
                    val_stage["failure_type"] = "sql_validation_error"
                    _capture_failure(
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
                    _capture_failure(
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
                val_results = retriever._result_validator.validate_query(
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
                    _capture_failure(
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
            if not val_error and not retriever._is_safe_read_query(current_sql):
                logger.warning("Unsafe SQL generated: %s", current_sql)
                val_stage["success"] = False
                val_stage["failure_type"] = "sql_validation_error"
                _capture_failure(
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
                router=retriever._router,
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
                dialect=retriever._dialect.sqlglot_dialect,
                db_id=retriever.db_id,
            )
            continue

        # Validation succeeded -> Execute read-only query
        try:
            with timed_stage("sql_execution") as exec_stage:
                rows = await run_readonly_query(current_sql, db_id=retriever.db_id)
                is_zero_rows = len(rows) == 0
                is_agg_zero = _is_aggregate_over_zero_rows(current_sql, rows, retriever._dialect.sqlglot_dialect)
                is_empty_result = is_zero_rows or is_agg_zero
                exec_stage["extra"] = {
                    "rows_returned": len(rows),
                    "empty_result": is_empty_result,
                    "repair_attempt": repair_attempt,
                }
                if is_empty_result:
                    exec_stage["failure_type"] = "empty_result"

            # Intelligent 0-row handling
            if is_empty_result and _check_feature_enabled("zero_row_handling_enabled"):
                classification = classify_empty_result(current_sql, dialect=retriever._dialect.sqlglot_dialect)
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
                                router=retriever._router,
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
            retriever._result_validator.validate_results(rows)
            learned_matches = retriever._pattern_learner.get_patterns_for_query(query)
            conf = retriever._confidence_scorer.calculate(
                pattern_matches=len(learned_matches),
                validation_results=val_results,
                reflexion_attempts=repair_attempt,
                query_complexity={"join_count": max(0, len(tables) - 1), "subquery_depth": current_sql.upper().count("SELECT") - 1},
            )
            retriever.last_confidence_score = conf.final_score
            retriever.last_confidence_breakdown = conf

            if repair_attempt > 0 and first_failed_sql:
                try:
                    retriever._pattern_learner.capture_success(
                        user_question=query,
                        original_cot="",
                        failed_sql=first_failed_sql,
                        error_message=first_error or "Previous attempt error",
                        fixed_sql=current_sql,
                        revised_cot=retriever.last_cot_plan or "",
                    )
                except Exception as learn_err:
                    logger.debug("Failed to record learned pattern: %s", learn_err)

            retriever.last_query_status = "empty_result" if is_empty_result else "success"
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
            retriever.last_sql_payload = sql_payload

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
                 "confidence_score": retriever.last_confidence_score},
                query=query,
            )
            return [RetrievedChunk(chunk=chunk, score=1.0, retrieval_method="text-to-sql")]

        except UnsafeQueryError as e:
            logger.warning("Blocked unsafe SQL query: %s", e)
            retriever.last_query_status = "failed"
            return []
        except Exception as e:
            logger.error("SQL Execution failed on attempt %d: %s", repair_attempt + 1, e)
            _capture_failure(
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
                router=retriever._router,
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
    retriever.last_query_status = "failed"
    _log_pipeline_event("retry_exhausted", {"last_error": first_error}, query=query)
    return []
