"""Late schema expansion: show the model a table it named that exists in the live schema but was not retrieved.

Schema retrieval picks a handful of tables by meaning. On a database with hundreds of tables the right one is sometimes not
among them, the model still names it (the prompt's glossary and meanings mention it), and validation then reports "table does
not exist in schema" although the table is real. Repair is then forced to swap in a look-alike table and invent joins.

This adds the DDL of such a table (exact name match against the live schema, never a fuzzy one) to the schema text the
validation and repair steps read, so the query is checked against what the table really contains instead of being rejected.

Safety rules:

* Off unless the database runs the Phase 12 prompt flags in apply mode (``auto_prompt_inject``); never for ``erp_main``.
* Exact table-name match only; at most ``MAX_ADDED`` tables per call; a name that is not in the live schema is left alone, so
  a hallucinated table is still reported exactly as before.
* The live schema comes from the retriever's per-database cache (the retrieval step already loaded it), not a new query.
* Any failure returns the schema unchanged.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from src.utils.telemetry import log_telemetry

logger = logging.getLogger(__name__)

MAX_ADDED = 3


async def expand_schema_with_live_tables(retriever: Any, schema: str, tables: Iterable[str]) -> tuple[str, list[str]]:
    """Return ``(schema, added_tables)``; ``schema`` is unchanged and ``added_tables`` empty when nothing applies."""
    try:
        from src.sql.repair.sql_repair import extract_visible_schema_context
        from src.sql.schema_retrieval import _route_bridge_mode, extract_table_ddl_map

        db_id = getattr(retriever, "db_id", None)
        if not db_id or _route_bridge_mode(db_id) != "apply":
            return schema, []

        wanted = {str(t).lower() for t in tables if t}
        if not wanted:
            return schema, []
        shown = {name.lower() for name in extract_visible_schema_context(schema)}
        missing = sorted(wanted - shown)
        if not missing:
            return schema, []

        live = extract_table_ddl_map(await retriever._fetch_full_schema())
        added = [t for t in missing if t in live][:MAX_ADDED]
        if not added:
            return schema, []

        log_telemetry(
            query_id="",
            stage="schema_late_expansion",
            extra={"db_id": db_id, "added": added, "missing": missing},
        )
        return schema + "\n\n" + "\n\n".join(live[t] for t in added), added
    except Exception as exc:  # noqa: BLE001 - an extra safety net; validation carries on exactly as before
        logger.warning("Late schema expansion skipped: %s", exc)
        return schema, []
