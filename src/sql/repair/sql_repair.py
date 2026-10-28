"""Delta Repair Executor for surgically fixing failed SQL queries with compact prompts.

Origin: moved from src/stages/sql_repair.py
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Any

from src.sql.repair.delta_repair import build_delta_repair_payload, count_tokens
from src.utils.error_classification import classify_error
from src.sql.learning.failure_capture import capture_sql_failure
from src.utils.query_budget import get_or_create_budget_controller
from src.utils.telemetry import get_current_query_id, log_telemetry, timed_stage

logger = logging.getLogger(__name__)

# Hard ceiling: maximum 3 repair attempts per query (raised from 2)
MAX_DELTA_REPAIR_ATTEMPTS = 3  # raised from 2: three repair attempts before giving up


def extract_sql_from_response(response: str) -> str:
    """Extract and sanitize raw SQL from an LLM response string.

    Handles:
    - Fenced markdown: ```sql ... ``` or ``` ... ```
    - Leading/trailing prose commentary (e.g., 'Here is the fixed query: SELECT ...')
    - Semicolon and whitespace formatting
    """
    if not response or not response.strip():
        return ""

    text = response.strip()

    # 1. Match fenced code blocks (```sql ... ``` or ``` ... ```)
    fenced_match = re.search(r"```(?:sql)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced_match:
        extracted = fenced_match.group(1).strip()
        if extracted:
            return extracted

    # 2. Check if text starts with SQL keywords directly
    upper_text = text.upper()
    if upper_text.startswith("SELECT") or upper_text.startswith("WITH"):
        return text.rstrip(";").strip()

    # 3. Look for line starting with SELECT or WITH ... AS
    for line in text.splitlines():
        line_clean = line.strip()
        if re.match(r"^(?:SELECT\b|WITH\s+[a-zA-Z0-9_]+\s+AS\b)", line_clean, re.IGNORECASE):
            idx = text.find(line)
            candidate = text[idx:].strip()
            return candidate.rstrip(";").strip()

    # 4. Fallback search for SELECT or WITH keyword
    sql_start_match = re.search(r"\b(SELECT\b[\s\S]*|WITH\s+[a-zA-Z0-9_]+\s+AS[\s\S]*)", text, re.IGNORECASE)
    if sql_start_match:
        candidate = sql_start_match.group(0).strip()
        return candidate.rstrip(";").strip()

    return text


def extract_schema_context_from_ddl(
    full_schema: str,
    relevant_tables: list[str] | set[str] | None = None,
) -> dict[str, list[str]]:
    """Parse a DDL schema string into a compact {table_name: [column_names]} dictionary."""
    if not full_schema:
        return {}

    schema_dict: dict[str, list[str]] = {}
    target_tables = {t.lower() for t in relevant_tables} if relevant_tables else None

    # Split by table blocks (handles both double-newline and compacted single-newline DDLs)
    table_blocks = re.split(r"(?=(?:TABLE|CREATE\s+TABLE)\s+[a-zA-Z0-9_]+)", full_schema, flags=re.IGNORECASE)
    for block in table_blocks:
        block = block.strip()
        if not block:
            continue

        match_table = re.search(r"(?:TABLE|CREATE\s+TABLE)\s+([a-zA-Z0-9_]+)", block, re.IGNORECASE)
        if not match_table:
            continue

        tbl_name = match_table.group(1).lower()
        if target_tables and tbl_name not in target_tables:
            continue

        # Extract column names from lines like "  column_name TYPE ...," or "- column_name: TYPE"
        columns: list[str] = []
        for line in block.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("CREATE", "TABLE", ")", "PRIMARY", "CONSTRAINT", "FOREIGN", "KEY")):
                continue
            col_match = re.match(r"^[-*]?\s*([a-zA-Z0-9_]+)\b", line)
            if col_match:
                col_name = col_match.group(1).lower()
                if col_name not in ("table", "create", "constraint", "primary", "foreign", "unique", "check"):
                    columns.append(col_name)

        schema_dict[tbl_name] = columns

    return schema_dict


# A table the schema prompt shows in its COMPACTED form: ``table: col(type, PK), col2(type, FK->t.c)`` (one line per table).
_COMPACT_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.+?)\s*$")
_COMPACT_COLUMN = re.compile(r"(?:^|,\s*)([A-Za-z0-9_]+)\(")
# The "Join Hints:" block appended after the tables: ``- orders.user_id -> users.id``. It is not part of any table.
_JOIN_HINT_LINE = re.compile(r"^\s*(?:Join Hints:|-\s+\S+\.\S+\s*->\s*\S+\.\S+)\s*$")


def extract_visible_schema_context(
    full_schema: str,
    relevant_tables: list[str] | set[str] | None = None,
) -> dict[str, list[str]]:
    """Every table the schema text the model was shown describes, as ``{table: [columns]}``.

    ``extract_schema_context_from_ddl`` only understands raw ``TABLE name (...)`` blocks. The prompt, however, mixes two
    forms: tables that compacted cleanly are one ``name: col(type), ...`` line, while tables whose DDL could not be parsed
    (for example a chunk that starts with a comment header) stay raw. A validator that reads only the raw form cannot see the
    compacted tables and reports them as missing. This reads both forms.

    The two forms are separated line by line first, so a compacted line that follows a raw block is never mistaken for one of
    that block's columns. A table present in both forms keeps its raw columns.
    """
    if not full_schema:
        return {}
    wanted = {t.lower() for t in relevant_tables} if relevant_tables else None
    raw_lines: list[str] = []
    compact: list[tuple[str, str]] = []
    for line in full_schema.splitlines():
        if _JOIN_HINT_LINE.match(line):
            continue
        match = _COMPACT_LINE.match(line)
        if match and (match.group(2) == "(no columns)" or _COMPACT_COLUMN.search(match.group(2))):
            compact.append((match.group(1).lower(), match.group(2)))
        else:
            raw_lines.append(line)
    context = extract_schema_context_from_ddl("\n".join(raw_lines), relevant_tables)
    for name, rest in compact:
        if (wanted and name not in wanted) or name in context:
            continue
        context[name] = [] if rest == "(no columns)" else [c.lower() for c in _COMPACT_COLUMN.findall(rest)]
    return context


def closest_tables(missing: str, available: list[str] | set[str], limit: int = 5) -> list[str]:
    """The existing tables whose names are most like ``missing``: shared words count as much as shared letters."""
    wanted = set(missing.lower().split("_")) - {""}
    scored: list[tuple[float, str]] = []
    for name in available:
        words = set(name.lower().split("_")) - {""}
        overlap = len(wanted & words) / len(wanted | words) if wanted | words else 0.0
        ratio = difflib.SequenceMatcher(None, missing.lower(), name.lower()).ratio()
        score = 0.5 * ratio + 0.5 * overlap
        if score >= 0.25:
            scored.append((-score, name))
    return [name for _, name in sorted(scored)[:limit]]


_MISSING_TABLE = re.compile(r"Table '([^']+)'")


def missing_table_name(error: str) -> str | None:
    match = _MISSING_TABLE.search(error or "")
    return match.group(1).lower() if match else None


def describe_missing_table(error: str, available: dict[str, list[str]]) -> str:
    """The validator's "table does not exist" message, made truthful: the validator lists only the tables the SQL happened to
    mention, which hides the rest of the schema from the repair step. This names the tables that really exist and the closest
    ones. Anything it cannot improve is returned unchanged."""
    name = missing_table_name(error)
    if not name or not available:
        return error
    close = closest_tables(name, list(available))
    message = f"Table '{name}' does not exist in the provided schema ({len(available)} tables are provided)."
    if close:
        message += f" Closest existing tables: {', '.join(close)}."
    return message


def repair_context_for_missing_table(
    error: str,
    available: dict[str, list[str]],
    referenced: dict[str, list[str]],
    *,
    with_columns: int = 3,
    extra_names: int = 10,
) -> dict[str, list[str]]:
    """What the repair step is shown after a missing table: the tables the SQL already uses (with columns), the closest
    existing tables (with columns), then the names of other tables in the schema. The repair prompt has a tight token budget
    and falls back to names only by itself, so listing names here is cheap."""
    name = missing_table_name(error)
    ctx: dict[str, list[str]] = dict(referenced)
    for t in closest_tables(name, list(available), limit=with_columns) if name else []:
        ctx.setdefault(t, available[t])
    for t in closest_tables(name, list(available), limit=with_columns + 2) if name else []:
        ctx.setdefault(t, [])
    for t in sorted(available):
        if len(ctx) >= len(referenced) + with_columns + extra_names:
            break
        ctx.setdefault(t, [])
    return ctx


async def attempt_delta_repair(
    router: Any,
    failed_sql: str,
    error_message: str,
    error_type: str = "sql_error",
    schema_context: dict[str, Any] | None = None,
    user_intent: str | None = None,
    attempt_number: int = 1,
    query_id: str | None = None,
    join_context: str = "",
) -> str | None:
    """Attempt to repair a failed SQL query using a surgical Delta Repair prompt.

    Args:
        router: The ProviderRouter instance used for LLM calls.
        failed_sql: The SQL string that failed validation or execution.
        error_message: The normalized or raw error message.
        error_type: The classified failure type.
        schema_context: Compact {table: [columns]} mapping.
        user_intent: Natural language user question.
        attempt_number: The current repair attempt index (1 or 2).
        query_id: Active query correlation ID.
        join_context: Optional block of known join conditions (see ``join_context.py``); empty keeps the prompt unchanged.

    Returns:
        Repaired SQL string on success, or None on failure / budget exhaustion.
    """
    effective_qid = query_id or get_current_query_id() or ""

    if attempt_number > MAX_DELTA_REPAIR_ATTEMPTS:
        logger.warning(
            "Delta repair attempt %d exceeds maximum allowed attempts (%d). Aborting.",
            attempt_number, MAX_DELTA_REPAIR_ATTEMPTS,
        )
        return None

    budget_ctrl = get_or_create_budget_controller(effective_qid)
    if not budget_ctrl.can_proceed(is_repair=True):
        logger.warning(
            "Delta repair blocked by query budget (query %s): %s",
            effective_qid, budget_ctrl.get_budget_status(),
        )
        log_telemetry(
            query_id=effective_qid,
            stage="sql_repair",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0.0,
            success=False,
            failure_type="budget_exceeded",
            repair_attempts=attempt_number,
            extra={"budget_status": budget_ctrl.get_budget_status()},
        )
        return None

    # 1. Assemble the surgical prompt payload (< 500 tokens)
    payload = build_delta_repair_payload(
        failed_sql=failed_sql,
        error_message=error_message,
        error_type=error_type,
        schema_context=schema_context,
        user_intent=user_intent,
        join_context=join_context,
    )

    prompt_tokens = count_tokens(payload[0]["content"]) + count_tokens(payload[1]["content"])
    logger.info(
        "Firing Delta Repair attempt %d/%d for query %s (~%d input tokens)",
        attempt_number, MAX_DELTA_REPAIR_ATTEMPTS, effective_qid, prompt_tokens,
    )

    start_time = time.perf_counter()
    try:
        with timed_stage("sql_repair", query_id=effective_qid) as repair_stage:
            repair_stage["extra"] = {
                "repair_attempt": attempt_number,
                "error_type": error_type,
                "input_tokens": prompt_tokens,
            }

            initial_repairs = budget_ctrl.repair_attempts if budget_ctrl else 0
            # Call provider router on the repair task lane (Phase 13 task-aware routing)
            response = await router.chat(
                messages=payload,
                task="repair",
                max_tokens=512,
                temperature=0.0,
            )
            if budget_ctrl and budget_ctrl.repair_attempts == initial_repairs:
                budget_ctrl.record_call(tokens_used=prompt_tokens + count_tokens(response or ""), is_repair=True)

            latency = round((time.perf_counter() - start_time) * 1000.0, 2)
            repaired_sql = extract_sql_from_response(response)

            if not repaired_sql:
                logger.warning("Delta repair attempt %d returned empty SQL", attempt_number)
                repair_stage["success"] = False
                repair_stage["failure_type"] = "empty_repair_response"
                capture_sql_failure(
                    query_id=effective_qid,
                    stage="sql_repair",
                    failed_sql=failed_sql,
                    raw_error="Delta repair returned empty response",
                    error_type="empty_repair_response",
                    schema_tables=list(schema_context.keys()) if schema_context else [],
                )
                return None

            repair_stage["success"] = True
            log_telemetry(
                query_id=effective_qid,
                stage="sql_repair",
                input_tokens=prompt_tokens,
                output_tokens=count_tokens(repaired_sql),
                latency_ms=latency,
                success=True,
                repair_attempts=attempt_number,
                extra={"repair_attempt": attempt_number, "repaired_sql": repaired_sql},
            )
            return repaired_sql

    except Exception as exc:
        latency = round((time.perf_counter() - start_time) * 1000.0, 2)
        err_type = classify_error(exc)
        logger.warning("Delta repair attempt %d failed with exception: %s", attempt_number, exc)

        log_telemetry(
            query_id=effective_qid,
            stage="sql_repair",
            input_tokens=prompt_tokens,
            output_tokens=0,
            latency_ms=latency,
            success=False,
            failure_type=err_type,
            repair_attempts=attempt_number,
            extra={"repair_attempt": attempt_number, "error": str(exc)},
        )

        capture_sql_failure(
            query_id=effective_qid,
            stage="sql_repair",
            failed_sql=failed_sql,
            raw_error=exc,
            error_type=err_type,
            schema_tables=list(schema_context.keys()) if schema_context else [],
        )
        return None
