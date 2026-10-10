"""Failure capture utility for logging exact failed SQL queries, errors, and metadata."""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from src.core.config import DATA_DIR, PROJECT_ROOT
from src.sql.knowledge.loaders import (
    DEFAULT_DB_ID,
    get_learned_path,
    resolve_learned_read_path,
)

logger = logging.getLogger(__name__)

DEFAULT_FAILURE_LOG_FILE = DATA_DIR / "failed_queries.jsonl"


def _get_failure_log_file(db_id: str = DEFAULT_DB_ID) -> Path:
    import sys
    shim = sys.modules.get("src.utils.failure_capture")
    if shim and hasattr(shim, "DEFAULT_FAILURE_LOG_FILE"):
        shim_val = Path(shim.DEFAULT_FAILURE_LOG_FILE)
        if shim_val != DEFAULT_FAILURE_LOG_FILE:
            return shim_val
    if DEFAULT_FAILURE_LOG_FILE != DATA_DIR / "failed_queries.jsonl":
        return Path(DEFAULT_FAILURE_LOG_FILE)
    return get_learned_path("failed_queries.jsonl", db_id=db_id, create_dir=True)


def get_failure_log_path(db_id: str = DEFAULT_DB_ID) -> Path:
    """Return the failure log path for the given database."""
    return get_learned_path("failed_queries.jsonl", db_id=db_id, create_dir=False)


def resolve_failure_log_read_path(db_id: str = DEFAULT_DB_ID) -> Path:
    """Resolve failure log path for reading, falling back to legacy global file if not found."""
    return resolve_learned_read_path("failed_queries.jsonl", fallback_path=DEFAULT_FAILURE_LOG_FILE, db_id=db_id)


def capture_sql_failure(
    query_id: str | None = None,
    stage: str = "sql_execution",
    failed_sql: str = "",
    raw_error: str | Exception = "unknown_error",
    error_type: str | None = None,
    schema_tables: list[str] | None = None,
    file_path: str | Path | None = None,
    db_id: str | None = None,
) -> dict[str, Any]:
    """Capture a SQL validation or execution failure safely to the failure log.

    Appends structured failure details to databases/<db_id>/learned/failed_queries.jsonl.
    Never throws exceptions into the calling pipeline.
    """
    try:
        from src.utils.error_classification import classify_error, normalize_error
        from src.utils.telemetry import get_current_query_id


        err_str = str(raw_error) if raw_error is not None else "unknown_error"
        resolved_error_type = error_type or classify_error(raw_error)
        clean_error = normalize_error(raw_error)

        effective_qid = str(query_id) if query_id else (get_current_query_id() or "unknown_query")
        effective_db_id = db_id or DEFAULT_DB_ID

        record: dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "query_id": effective_qid,
            "db_id": effective_db_id,
            "stage": str(stage),
            "error_type": resolved_error_type,
            "failed_sql": str(failed_sql).strip(),
            "raw_error": err_str,
            "normalized_error": clean_error,
            "schema_tables": list(schema_tables or []),
        }

        # Determine target file
        target_path = Path(file_path) if file_path else _get_failure_log_file(db_id=effective_db_id)


        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as file_exc:
            logger.warning("Failed to write to failure capture log at %s: %s", target_path, file_exc)

        return record

    except Exception as exc:
        logger.warning("capture_sql_failure encountered an unexpected error: %s", exc)
        return {}
