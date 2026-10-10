"""Schema retrieval and DDL parsing utilities for text-to-SQL.

Extracts relevant DB schema chunks using Schema RAG against Qdrant,
with dynamic token budget selection, domain anchor injection, 1-hop graph
expansion, and scoped schema fallback.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.models.schemas import ChunkType
from src.sql.knowledge.loaders import DEFAULT_DB_ID
from src.sql.table_router import route_anchor_tables, route_tables_for_query
from src.utils.feature_flags import is_feature_enabled
from src.utils.schema_budget import DEFAULT_SCHEMA_TOKEN_BUDGET, select_schema_within_budget
from src.utils.schema_compactor import compact_ddl, extract_join_hints
from src.utils.schema_token_estimator import estimate_schema_tokens
from src.utils.telemetry import log_telemetry

logger = logging.getLogger(__name__)


def extract_schema_table_names(schema: str) -> set[str]:
    """Extract table names present in a DDL schema string."""
    names: set[str] = set()
    for m in re.finditer(r'(?:TABLE|CREATE\s+TABLE)\s+([a-zA-Z0-9_]+)', schema, re.IGNORECASE):
        names.add(m.group(1).lower())
    return names


def extract_table_ddl_map(full_schema: str) -> dict[str, str]:
    """Parse full schema string into {table_name: ddl_string}."""
    ddls: dict[str, str] = {}
    for block in full_schema.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        m = re.search(r'(?:TABLE|CREATE\s+TABLE)\s+([a-zA-Z0-9_]+)', block, re.IGNORECASE)
        if m:
            ddls[m.group(1).lower()] = block
    return ddls


def get_1hop_neighbors(
    tables: set[str],
    rels: list[dict[str, Any]] | None = None,
    db_id: str = DEFAULT_DB_ID,
) -> set[str]:
    """Find 1-hop connected tables from the relationship graph, skipping audit noise."""
    if rels is not None:
        raw_rels = rels
    else:
        from src.sql.prompt_builder import get_raw_relationships as _get_raw_relationships
        raw_rels = _get_raw_relationships(db_id)

    neighbors: set[str] = set()
    for r in raw_rels:
        frm = (r.get("from_table") or "").lower()
        to = (r.get("to_table") or "").lower()
        fcol = (r.get("from_column") or "").lower()
        # Skip audit trail links to users unless explicitly asked
        if to == "users" and fcol in ("created_id", "updated_id", "deleted_id"):
            continue
        if frm in tables and to:
            neighbors.add(to)
        if to in tables and frm:
            neighbors.add(frm)
    return neighbors


def format_scoped_relationships(
    active_tables: set[str],
    query: str = "",
    rels: list[dict[str, Any]] | None = None,
    db_id: str = DEFAULT_DB_ID,
) -> str:
    """Format relationships only between the tables present in the active schema prompt."""
    if not active_tables:
        return ""
    if rels is not None:
        raw_rels = rels
    else:
        from src.sql.prompt_builder import get_raw_relationships as _get_raw_relationships
        raw_rels = _get_raw_relationships(db_id)

    if not raw_rels:
        return ""

    include_audit = any(
        w in query.lower()
        for w in ("created by", "creator", "updated by", "who entered", "who deleted")
    )
    grouped: dict[str, list[str]] = {}
    for r in raw_rels:
        frm = (r.get("from_table") or "").lower()
        to = (r.get("to_table") or "").lower()
        fcol = r.get("from_column")
        tcol = r.get("to_column")
        if not (frm and to and fcol and tcol):
            continue
        if frm not in active_tables or to not in active_tables:
            continue
        if not include_audit and to == "users" and str(fcol).lower() in ("created_id", "updated_id", "deleted_id"):
            continue
        grouped.setdefault(frm, []).append(f"{fcol}->{to}.{tcol}")

    if not grouped:
        return ""
    return "\n".join(
        f"- {table}: {', '.join(edges)}" for table, edges in sorted(grouped.items())
    )


def build_scoped_schema_fallback(
    full_schema: str,
    query: str,
    db_id: str = DEFAULT_DB_ID,
) -> str:
    """Build a concise, token-efficient subset of schema (max 6-8 tables) matching query intent."""
    if not full_schema:
        return ""
    full_ddls = extract_table_ddl_map(full_schema)
    if not full_ddls:
        return full_schema[:3500]

    query_lower = query.lower()
    candidate_tables: list[str] = []

    from src.sql.prompt_builder import (
        build_column_glossary_for_query as _build_column_glossary_for_query,
    )

    glossary_text = _build_column_glossary_for_query(query, db_id=db_id)
    glossary_tables = set(re.findall(r"\b([a-zA-Z0-9_]+)\.[a-zA-Z0-9_]+", glossary_text))
    for t in glossary_tables:
        if t in full_ddls and t not in candidate_tables:
            candidate_tables.append(t)

    domain_matched = route_tables_for_query(query_lower, db_id=db_id, section="fallback_rules")
    for t in domain_matched:
        if t in full_ddls and t not in candidate_tables:
            candidate_tables.append(t)

    # Filter to candidate tables present in full_ddls
    valid_tables = [t for t in candidate_tables if t in full_ddls]

    # If no candidate matched the actual database tables (e.g. test fixture or custom DB), use available tables
    if not valid_tables:
        valid_tables = list(full_ddls.keys())[:8]
    else:
        valid_tables = valid_tables[:6]
        # 1-hop expansion for bridge tables
        neighbors = get_1hop_neighbors(set(valid_tables), db_id=db_id)
        for n in sorted(neighbors):
            if n in full_ddls and n not in valid_tables and len(valid_tables) < 8:
                valid_tables.append(n)

    candidate_items = [
        {"table_name": t, "ddl": full_ddls[t], "source": "fallback"}
        for t in valid_tables
        if t in full_ddls
    ]
    selected, dropped = select_schema_within_budget(
        candidate_items,
        token_budget=DEFAULT_SCHEMA_TOKEN_BUDGET,
        id_key="table_name",
    )

    budgeted_schema = "\n\n".join(c["ddl"] for c in selected)
    original_schema = "\n\n".join(full_ddls[t] for t in valid_tables if t in full_ddls)
    estimated_tokens = estimate_schema_tokens(budgeted_schema)
    flag_enabled = is_feature_enabled("token_budget_enabled")
    stage_name = "schema_budget_applied" if flag_enabled else "schema_budget_shadow"

    log_telemetry(
        query_id="",
        stage=stage_name,
        input_tokens=estimated_tokens,
        extra={
            "original_table_count": len(candidate_items),
            "budgeted_table_count": len(selected),
            "estimated_tokens": estimated_tokens,
            "dropped_tables": [c["table_name"] for c in dropped],
            "token_budget_enabled": flag_enabled,
            "fallback": True,
        },
    )

    if is_feature_enabled("schema_compaction_enabled"):
        compact_ddls = [compact_ddl(c["ddl"]) for c in selected]
        raw_ddls = [c["ddl"] for c in selected]
        join_hints = extract_join_hints(raw_ddls)

        compacted_schema = "\n".join(compact_ddls)
        if join_hints:
            compacted_schema += "\n\n" + join_hints

        before_tokens = estimate_schema_tokens(budgeted_schema)
        after_tokens = estimate_schema_tokens(compacted_schema)

        log_telemetry(
            query_id="",
            stage="schema_compaction_applied",
            input_tokens=after_tokens,
            extra={
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "token_savings": max(0, before_tokens - after_tokens),
                "table_count": len(selected),
                "fallback": True,
            },
        )
        return compacted_schema

    if flag_enabled:
        return budgeted_schema
    return original_schema


async def retrieve_schema_from_qdrant(
    query: str,
    full_schema: str,
    vector_store: Any | None = None,
    embeddings: Any | None = None,
    db_id: str = DEFAULT_DB_ID,
    dialect: Any | None = None,
) -> str:
    """Fetch relevant DB schema chunks for the prompt using Schema RAG.

    If vector_store is missing or fails, falls back to an intelligent, token-bounded schema.
    """
    if not vector_store or not embeddings:
        return build_scoped_schema_fallback(full_schema, query, db_id=db_id)

    try:
        dense_vec, sparse_vec = await embeddings.embed_query(query)
        chunks = await vector_store.search_hybrid(
            query_vector=dense_vec,
            sparse_vector=sparse_vec,
            query_text=query,
            top_k=4,
            filters={
                "chunk_type": ChunkType.SQL_SCHEMA.value,
                "db_id": db_id,
            },
        )

        # Transition rule & isolation defense: ensure retrieved chunks strictly belong to db_id
        filtered_chunks = []
        for c in chunks:
            chunk_obj = c.chunk
            c_db_id = getattr(chunk_obj, "db_id", None) or chunk_obj.metadata.get("db_id")
            c_doc_id = chunk_obj.document_id
            if c_db_id:
                if c_db_id == db_id:
                    filtered_chunks.append(c)
            elif c_doc_id == f"schema:{db_id}":
                filtered_chunks.append(c)
            elif c_doc_id == "live_db_schema" and db_id == DEFAULT_DB_ID:
                # Legacy chunks (document_id "live_db_schema", no db_id) accepted ONLY for erp_main until re-synced
                filtered_chunks.append(c)
        chunks = filtered_chunks

        if not chunks:
            logger.warning("Schema RAG returned 0 chunks. Falling back to scoped schema.")
            return build_scoped_schema_fallback(full_schema, query, db_id=db_id)

        # Combine the retrieved CREATE TABLE statements
        retrieved_schema = "\n\n".join(chunk.chunk.content for chunk in chunks)
        retrieved_tables = extract_schema_table_names(retrieved_schema)

        # Seed anchor tables from matched glossary terms & domain concepts
        from src.sql.prompt_builder import (
            build_column_glossary_for_query as _build_column_glossary_for_query,
        )
        from src.sql.prompt_builder import (
            get_raw_relationships as _get_raw_relationships,
        )

        glossary_text = _build_column_glossary_for_query(query, db_id=db_id)
        glossary_tables = set(re.findall(r"\b([a-zA-Z0-9_]+)\.[a-zA-Z0-9_]+", glossary_text))

        glossary_tables.update(route_anchor_tables(query, db_id=db_id))

        full_ddls = extract_table_ddl_map(full_schema) if full_schema else {}
        candidate_list: list[dict[str, Any]] = []
        seen_tables: set[str] = set()

        # Priority 1: Domain anchor & glossary tables first
        anchor_extra = []
        for g_table in sorted(glossary_tables):
            if g_table in full_ddls:
                if g_table not in retrieved_tables:
                    anchor_extra.append(full_ddls[g_table])
                    retrieved_tables.add(g_table)
                if g_table not in seen_tables:
                    seen_tables.add(g_table)
                    candidate_list.append({
                        "table_name": g_table,
                        "ddl": full_ddls[g_table],
                        "source": "domain_anchor",
                    })

        # Priority 2: Vector RAG chunks
        for chunk in chunks:
            tbls = extract_schema_table_names(chunk.chunk.content)
            for tbl in tbls:
                if tbl not in seen_tables and (not full_ddls or tbl in full_ddls):
                    seen_tables.add(tbl)
                    candidate_list.append({
                        "table_name": tbl,
                        "ddl": chunk.chunk.content,
                        "source": "vector_rag",
                    })

        if anchor_extra:
            retrieved_schema += "\n\n-- Domain Anchor & Glossary Tables:\n" + "\n\n".join(anchor_extra)

        # 1-hop graph expansion: add directly connected neighbor tables if missing
        neighbors = get_1hop_neighbors(retrieved_tables, db_id=db_id)
        needed_neighbors = neighbors - retrieved_tables

        if needed_neighbors and full_ddls:
            # Rank neighbors by how many active tables they connect to (bridge priority)
            raw_rels = _get_raw_relationships(db_id)
            conn_scores: dict[str, int] = {}
            for r in raw_rels:
                frm = (r.get("from_table") or "").lower()
                to = (r.get("to_table") or "").lower()
                if frm in retrieved_tables and to in needed_neighbors:
                    conn_scores[to] = conn_scores.get(to, 0) + 1
                if to in retrieved_tables and frm in needed_neighbors:
                    conn_scores[frm] = conn_scores.get(frm, 0) + 1

            ranked_neighbors = sorted(needed_neighbors, key=lambda t: conn_scores.get(t, 0), reverse=True)
            added = 0
            extra_ddls = []
            for n_table in ranked_neighbors:
                if n_table in full_ddls and added < 4:
                    extra_ddls.append(full_ddls[n_table])
                    added += 1
                if n_table in full_ddls and n_table not in seen_tables:
                    seen_tables.add(n_table)
                    candidate_list.append({
                        "table_name": n_table,
                        "ddl": full_ddls[n_table],
                        "source": "graph_expansion",
                    })
            if extra_ddls:
                retrieved_schema += "\n\n-- Directly connected related tables:\n" + "\n\n".join(extra_ddls)

        # --- Dynamic Schema Token Budget Selection & Shadow Mode ---
        selected, dropped = select_schema_within_budget(
            candidate_list,
            token_budget=DEFAULT_SCHEMA_TOKEN_BUDGET,
            id_key="table_name",
        )

        original_table_count = len(candidate_list)
        budgeted_table_count = len(selected)
        dropped_tables = [c["table_name"] if isinstance(c, dict) else str(c) for c in dropped]
        budgeted_schema = "\n\n".join(c["ddl"] if isinstance(c, dict) else str(c) for c in selected)
        estimated_tokens = estimate_schema_tokens(budgeted_schema)
        flag_enabled = is_feature_enabled("token_budget_enabled")
        stage_name = "schema_budget_applied" if flag_enabled else "schema_budget_shadow"

        log_telemetry(
            query_id="",
            stage=stage_name,
            input_tokens=estimated_tokens,
            extra={
                "original_table_count": original_table_count,
                "budgeted_table_count": budgeted_table_count,
                "estimated_tokens": estimated_tokens,
                "dropped_tables": dropped_tables,
                "token_budget_enabled": flag_enabled,
            },
        )

        if is_feature_enabled("schema_compaction_enabled"):
            dialect_key = dialect.sqlglot_dialect if hasattr(dialect, "sqlglot_dialect") else None
            compact_ddls = [
                compact_ddl(c["ddl"] if isinstance(c, dict) else str(c), dialect=dialect_key)
                for c in selected
            ]
            raw_ddls = [c["ddl"] if isinstance(c, dict) else str(c) for c in selected]
            join_hints = extract_join_hints(raw_ddls, dialect=dialect_key)

            compacted_schema = "\n\n".join(compact_ddls)
            if join_hints:
                compacted_schema += "\n\n" + join_hints

            before_tokens = estimate_schema_tokens(budgeted_schema)
            after_tokens = estimate_schema_tokens(compacted_schema)

            log_telemetry(
                query_id="",
                stage="schema_compaction_applied",
                input_tokens=after_tokens,
                extra={
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "token_savings": max(0, before_tokens - after_tokens),
                    "table_count": len(selected),
                    "has_join_hints": bool(join_hints),
                },
            )
            return compacted_schema

        if flag_enabled:
            return budgeted_schema
        return retrieved_schema

    except Exception as e:  # noqa: BLE001
        logger.error("Schema RAG search failed: %s", e)
        return build_scoped_schema_fallback(full_schema, query, db_id=db_id)


# Aliases with leading underscores for explicit re-export compatibility
_extract_schema_table_names = extract_schema_table_names
_extract_table_ddl_map = extract_table_ddl_map
_get_1hop_neighbors = get_1hop_neighbors
_format_scoped_relationships = format_scoped_relationships
_build_scoped_schema_fallback = build_scoped_schema_fallback
_retrieve_schema_from_qdrant = retrieve_schema_from_qdrant
