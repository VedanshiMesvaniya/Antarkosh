"""Keep live SQL result rows out of LLM prompts built from chat history.

Assistant answers that came from the database contain the result table (and, for
fast-path templates, a sentence holding the value). Only the SQL that was executed
may be referenced; rows and values must never be forwarded to a model.
"""

from __future__ import annotations

import re


_SQL_MARKER = "SQL Query Executed:"
# The producer collapses the query to one line, so match to the LAST backtick on
# that line; this keeps queries that contain MySQL `identifier` quoting intact.
_SQL_QUERY_RE = re.compile(r"SQL Query Executed:\s*`([^\n]*)`")
_MAX_SQL_CHARS = 400


def _is_sql_generated(model_used: str | None) -> bool:
    """True when the text before the marker was produced by code, not by an LLM.

    Unknown (missing) model is treated as SQL-generated: the safe default.
    """
    if not model_used:
        return True
    return model_used == "sql/direct" or model_used.startswith("fast_path/")


def sanitize_assistant_turn(
    content: str,
    model_used: str | None = None,
    *,
    include_sql: bool = True,
    max_chars: int | None = None,
) -> str:
    """Return assistant text that is safe to place in an LLM prompt.

    No SQL marker -> returned unchanged (document answers are untouched).
    With a marker -> the result table is dropped; the executed SQL is kept as a
    reference when ``include_sql`` is True. Text before the marker is kept only
    when an LLM wrote it from document context (hybrid answers).
    """
    text = (content or "").strip()
    idx = text.find(_SQL_MARKER)
    if idx == -1:
        return text

    head = "" if _is_sql_generated(model_used) else text[:idx].strip()

    match = _SQL_QUERY_RE.search(text)
    sql = match.group(1).strip() if match else ""
    if include_sql and sql:
        if len(sql) > _MAX_SQL_CHARS:
            sql = sql[:_MAX_SQL_CHARS].rstrip() + "..."
        note = f"[Database result omitted. SQL executed: {sql}]"
    else:
        note = "[A live database result was shown to the user.]"

    if max_chars is not None:
        budget = max_chars - len(note) - 2
        head = head[:budget].rstrip() if budget > 0 else ""

    return f"{head}\n\n{note}".strip()
