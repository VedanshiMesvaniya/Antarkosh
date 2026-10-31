"""Schema retrieval and DDL parsing utilities for text-to-SQL.

Extracts relevant DB schema chunks using Schema RAG against Qdrant,
with dynamic token budget selection, domain anchor injection, 1-hop graph
expansion, and scoped schema fallback.

Origin: extracted from src/stages/s12b_sql_retrieval.py (extract_schema_table_names, extract_table_ddl_map, get_1hop_neighbors, format_scoped_relationships, build_scoped_schema_fallback, retrieve_schema_from_qdrant)
Origin: merged from src/pipeline/schema_ingestion.py (sync_live_schema)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.core.db_client import run_readonly_query
from src.models.schemas import Chunk, ChunkType, DocumentType
from src.rag.stages.s10_embeddings import EmbeddingService
from src.rag.stages.s11_vector_store import QdrantStore
from src.sql.dialects import SQLDialectProfile, get_dialect_profile
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID, get_knowledge_path, validate_db_id
from src.sql.schema_budget import DEFAULT_SCHEMA_TOKEN_BUDGET, select_schema_within_budget
from src.sql.schema_compactor import compact_ddl, extract_join_hints
from src.sql.schema_token_estimator import estimate_schema_tokens
from src.sql.table_router import route_anchor_tables, route_tables_for_query
from src.utils.feature_flags import is_feature_enabled
from src.utils.telemetry import log_telemetry

logger = logging.getLogger(__name__)


def _route_bridge_mode(db_id: str) -> str:
    """How route-aware bridge tables (Phase 12, part 1) run for this database: "apply", "shadow" or "" (off).

    Uses the Phase 12 flags that already exist: ``auto_prompt_inject`` applies the bridges, ``auto_prompt_shadow`` only logs
    what would be added. Both are off for ``erp_main`` and for any database that has not been given the flags.
    """
    try:
        from src.utils.feature_flags import is_db_feature_enabled

        if is_db_feature_enabled("auto_prompt_inject", db_id):
            return "apply"
        if is_db_feature_enabled("auto_prompt_shadow", db_id):
            return "shadow"
    except Exception as exc:  # noqa: BLE001 - a flag problem must never break retrieval
        logger.debug("Route bridge flags unreadable for %s: %s", db_id, exc)
    return ""


# Tables taken from the vector index per question. 4 is the long-standing value and stays the default. With the Phase 12 prompt
# flags on, a wider net is used: the token budget (not this number) is what bounds the prompt, and on a database with hundreds
# of tables a question about "plants" or "teams" often has its real table ranked 5th to 8th behind look-alike names.
SCHEMA_VECTOR_TOP_K = 4
SCHEMA_VECTOR_TOP_K_WIDE = 8


def _vector_top_k(route_mode: str) -> int:
    return SCHEMA_VECTOR_TOP_K_WIDE if route_mode == "apply" else SCHEMA_VECTOR_TOP_K


def _compacted_cost_fn(dialect_key: str | None) -> Any:
    """Token cost of a candidate as the prompt will show it (compacted), memoised per table for one retrieval."""
    memo: dict[str, int] = {}

    def cost(cand: Any) -> int:
        ddl = cand["ddl"] if isinstance(cand, dict) else str(cand)
        key = cand.get("table_name") if isinstance(cand, dict) else None
        if key is not None and key in memo:
            return memo[key]
        try:
            value = estimate_schema_tokens(compact_ddl(ddl, dialect=dialect_key))
        except Exception:  # noqa: BLE001 - fall back to the raw estimate for this table
            value = estimate_schema_tokens(ddl)
        if key is not None:
            memo[key] = value
        return value

    return cost


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
        route_mode = _route_bridge_mode(db_id)
        dense_vec, sparse_vec = await embeddings.embed_query(query)
        chunks = await vector_store.search_hybrid(
            query_vector=dense_vec,
            sparse_vector=sparse_vec,
            query_text=query,
            top_k=_vector_top_k(route_mode),
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

        # Table authority (Phase 12, part 2): when retrieval chose a copy of a table (product_bak) whose real table (product) is
        # clearly decided in table_profiles.json, offer the real table too. The copy is never removed. Apply mode adds it;
        # shadow mode (the default with the Phase 12 flags) only logs what would be added. No profile file: nothing changes.
        authority_added: list[str] = []
        try:
            from src.sql.profiles.hints import hints_mode, plan_authority

            authority_mode = hints_mode(db_id)
            if authority_mode:
                authority_plan = plan_authority([c["table_name"] for c in candidate_list], db_id, available=set(full_ddls))
                if authority_plan is not None:
                    if authority_mode == "apply":
                        for primary in authority_plan.add:
                            if primary in full_ddls and primary not in seen_tables:
                                seen_tables.add(primary)
                                authority_added.append(primary)
                                candidate_list.append({
                                    "table_name": primary,
                                    "ddl": full_ddls[primary],
                                    "source": "table_authority",
                                })
                        if authority_added:
                            retrieved_schema += "\n\n-- Real-data tables for look-alike copies:\n" + "\n\n".join(
                                full_ddls[t] for t in authority_added
                            )
                    log_telemetry(
                        query_id="",
                        stage="table_authority_applied" if authority_mode == "apply" else "table_authority_shadow",
                        extra={
                            "db_id": db_id,
                            "would_add": list(authority_plan.add),
                            "added": authority_added,
                            "pairs": [list(p) for p in authority_plan.pairs],
                            "skipped": dict(authority_plan.skipped),
                        },
                    )
        except Exception as authority_err:  # noqa: BLE001 - authority is an extra; retrieval carries on without it
            logger.warning("Table authority planning failed for %s: %s", db_id, authority_err)
            authority_added = []

        # Route-aware bridge tables (Phase 12, part 1): when two chosen tables only connect through other tables, offer those
        # tables to the budget right after the retrieved ones and before the 1-hop neighbours. Needs join_paths.json (Phase 8);
        # without it, or with the flags off, nothing here changes.
        route_plan = None
        route_bridge_tables: list[str] = []
        if route_mode:
            try:
                from src.sql.paths.route_index import plan_route_bridges

                route_plan = plan_route_bridges(
                    [c["table_name"] for c in candidate_list], db_id, available=set(full_ddls)
                )
                if route_plan is not None:
                    if route_mode == "apply":
                        for bridge in route_plan.bridges:
                            if bridge in full_ddls and bridge not in seen_tables:
                                seen_tables.add(bridge)
                                route_bridge_tables.append(bridge)
                                candidate_list.append({
                                    "table_name": bridge,
                                    "ddl": full_ddls[bridge],
                                    "source": "route_bridge",
                                })
                        if route_bridge_tables:
                            retrieved_schema += "\n\n-- Bridge tables on an indirect join route:\n" + "\n\n".join(
                                full_ddls[b] for b in route_bridge_tables
                            )
                    log_telemetry(
                        query_id="",
                        stage="route_bridge_applied" if route_mode == "apply" else "route_bridge_shadow",
                        extra={
                            "db_id": db_id,
                            "bridges": list(route_plan.bridges),
                            "added": route_bridge_tables,
                            "pairs_checked": route_plan.pairs_checked,
                            "pairs_routed": route_plan.pairs_routed,
                            "skipped": dict(route_plan.skipped),
                        },
                    )
            except Exception as route_err:  # noqa: BLE001 - routes are an extra; retrieval carries on without them
                logger.warning("Route bridge planning failed for %s: %s", db_id, route_err)
                route_plan = None
                route_bridge_tables = []

        # 1-hop graph expansion: add directly connected neighbor tables if missing
        neighbors = get_1hop_neighbors(retrieved_tables, db_id=db_id)
        needed_neighbors = neighbors - retrieved_tables - set(route_bridge_tables) - set(authority_added)

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
        # With route bridges applied and compaction on, the budget is spent on the compacted text the model will see
        # (the raw DDL of a wide table is several times larger). Everything else keeps the original raw estimate.
        dialect_key_for_budget = dialect.sqlglot_dialect if hasattr(dialect, "sqlglot_dialect") else None
        use_compacted_cost = route_mode == "apply" and is_feature_enabled("schema_compaction_enabled")
        selected, dropped = select_schema_within_budget(
            candidate_list,
            token_budget=DEFAULT_SCHEMA_TOKEN_BUDGET,
            id_key="table_name",
            cost_fn=_compacted_cost_fn(dialect_key_for_budget) if use_compacted_cost else None,
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
            if route_plan is not None and route_bridge_tables:
                # Join conditions of each applied route, only for routes whose every table survived the budget.
                from src.sql.paths.route_index import merge_join_hints

                join_hints = merge_join_hints(
                    join_hints,
                    route_plan.hint_lines(c["table_name"] for c in selected if isinstance(c, dict)),
                )

            compacted_schema = "\n\n".join(compact_ddls)
            if join_hints:
                compacted_schema += "\n\n" + join_hints

            before_tokens = estimate_schema_tokens(budgeted_schema)
            after_tokens = estimate_schema_tokens(compacted_schema)
            # Tables the compactor could not parse come back as their raw DDL: named here so the cause can be fixed on evidence.
            uncompacted = [
                (c["table_name"] if isinstance(c, dict) else str(c)[:40])
                for c, raw_ddl, compact in zip(selected, raw_ddls, compact_ddls, strict=False)
                if compact.strip() == raw_ddl.strip()
            ]

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
                    "uncompacted_tables": uncompacted,
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


SCHEMA_DOCUMENT_ID = "live_db_schema"


def get_schema_document_id(db_id: str) -> str:
    """Return the canonical Qdrant document_id for a database's schema chunks."""
    return f"schema:{db_id}"


def _split_mysql_tables(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Group MySQL information_schema rows into per-table CREATE-TABLE-like text."""
    tables: dict[str, list[str]] = {}
    for i, row in enumerate(rows):
        if row.get("sql"):
            # Direct CREATE statement present
            name = row.get("name") or row.get("table_name") or f"table_{i}"
            tables[name] = [row["sql"]]
            continue
        tname = row.get("table_name") or row.get("TABLE_NAME") or row.get("name")
        cname = row.get("column_name") or row.get("COLUMN_NAME", "")
        ctype = row.get("data_type") or row.get("DATA_TYPE", "")
        if not tname:
            continue
        comment = row.get("column_comment") or ""
        suffix = f"  -- {comment}" if comment else ""
        tables.setdefault(tname, []).append(
            f"  {cname} {ctype}{suffix}".strip()
        )
    return {
        name: cols[0] if (len(cols) == 1 and cols[0].startswith("CREATE TABLE")) else f"TABLE {name} (\n" + ",\n".join(cols) + "\n)"
        for name, cols in tables.items()
    }


def _split_sqlite_tables(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Split SQLite sqlite_master rows into per-table CREATE statements."""
    return {
        row["name"]: row["sql"]
        for row in rows
        if row.get("name") != "sqlite_sequence" and row.get("sql")
    }


def _split_schema_by_table(
    dialect: SQLDialectProfile, rows: list[dict[str, Any]]
) -> dict[str, str]:
    """Return {table_name: schema_text} for each table in the database."""
    if dialect.key in (Engine.MYSQL, Engine.POSTGRESQL, Engine.MSSQL, Engine.ORACLE):
        return _split_mysql_tables(rows)  # same one-row-per-column shape
    if dialect.key == Engine.SQLITE:
        return _split_sqlite_tables(rows)
    raise ValueError(f"Unsupported dialect key {dialect.key!r}")


def _load_table_metadata(db_id: str = DEFAULT_DB_ID) -> dict[str, dict[str, Any]]:
    """Load table domain and metadata from schema knowledge pack if present."""
    try:
        schema_file = get_knowledge_path("schema", db_id=db_id)
        if not schema_file.exists():
            return {}
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        tables = data.get("tables", [])
        return {t["name"].lower(): t for t in tables if isinstance(t, dict) and "name" in t}
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not load schema metadata for %s: %s", db_id, e)
        return {}


def _enrich_table_schema(
    table_name: str,
    schema_text: str,
    table_meta: dict[str, Any] | None = None,
    fk_lines: list[str] | None = None,
) -> str:
    """Format an enriched table chunk with domain header, primary key, and foreign keys."""
    lines: list[str] = []

    if table_meta:
        domain = table_meta.get("domain")
        pk = table_meta.get("primary_key")
        parts = [f"-- Table: {table_name}"]
        if domain:
            parts.append(f"Domain: {domain}")
        if pk:
            pk_str = ", ".join(pk) if isinstance(pk, list) else str(pk)
            parts.append(f"Primary Key: ({pk_str})")
        lines.append(" | ".join(parts))

    lines.append(schema_text)

    if fk_lines:
        lines.append("-- Relationships / Foreign Keys:")
        lines.extend(fk_lines)

    return "\n".join(lines)


def _chunk_table(chunk: Chunk) -> str:
    """The table a schema chunk describes, lower-cased (its id is ``<db_id>_schema_<table>``)."""
    prefix = f"{chunk.db_id}_schema_"
    return (chunk.chunk_id[len(prefix):] if chunk.chunk_id.startswith(prefix) else chunk.source_file.rsplit("/", 1)[-1]).lower()


def _embedding_texts(chunks: list[Chunk], extras: dict[str, str]) -> list[str]:
    """The text each table chunk is EMBEDDED from: its content plus, when there is one, its block of column meanings.
    The chunk's stored content is not changed, so the token budget and the prompts built from it are not affected."""
    texts: list[str] = []
    for c in chunks:
        extra = extras.get(_chunk_table(c))
        texts.append(f"{c.content}\n{extra}" if extra else c.content)
    return texts


async def _embed_with_meanings(embeddings: Any, chunks: list[Chunk], extras: dict[str, str]) -> tuple[Any, Any]:
    """``embed_chunks`` exactly as before when there is nothing to add; otherwise embed the augmented text."""
    if extras and hasattr(embeddings, "embed_texts"):
        texts = _embedding_texts(chunks, extras)
        if texts != [c.content for c in chunks]:
            try:
                return await embeddings.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 - e.g. the model rejects the longer text: never worse than before this phase
                logger.warning("Embedding with column meanings failed (%s); embedding the plain table text instead", exc)
    return await embeddings.embed_chunks(chunks)


async def _reembed_changed_tables(embeddings: Any, store: Any, chunks: list[Chunk], extras: dict[str, str], tables: list[str]) -> int:
    """Re-embed only the chunks whose column meanings changed during this sync (same ids, so they are updated in place)."""
    wanted = {t.lower() for t in tables}
    subset = [c for c in chunks if _chunk_table(c) in wanted]
    if not subset:
        return 0
    vectors, sparse_vectors = await _embed_with_meanings(embeddings, subset, extras)
    await store.upsert(subset, vectors, sparse_vectors)
    return len(subset)


def _relationship_fk_map(db_id: str) -> dict[str, list[str]]:
    """The foreign-key lines the sync falls back to when the database declares none: read from relationships.json."""
    fk_map: dict[str, list[str]] = {}
    try:
        rel_path = get_knowledge_path("relationships", db_id=db_id)
        if rel_path.exists():
            rel_data = json.loads(rel_path.read_text(encoding="utf-8"))
            rels = rel_data.get("relationships") if isinstance(rel_data, dict) else rel_data
            for r in rels or []:
                frm, fcol, to, tcol = r.get("from_table"), r.get("from_column"), r.get("to_table"), r.get("to_column")
                if frm and fcol and to and tcol:
                    fk_map.setdefault(frm, []).append(f"  FOREIGN KEY ({fcol}) REFERENCES {to}({tcol})")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not load inferred relationships: %s", e)
    return fk_map


async def _refresh_relationship_text(
    db_id: str,
    embeddings: Any,
    store: Any,
    chunks: list[Chunk],
    table_schemas: dict[str, str],
    meta_map: dict[str, Any],
    extras: dict[str, str],
) -> int:
    """Auto-schema Phase 11: the table text was built from relationships.json as it stood BEFORE Phase 6 rewrote it. Rebuild the
    text from the file as it is now and re-embed only the tables whose text changed (same ids, updated in place)."""
    fk_map = _relationship_fk_map(db_id)
    by_lower = {name.lower(): name for name in table_schemas}
    changed: list[str] = []
    for chunk in chunks:
        name = by_lower.get(_chunk_table(chunk))
        if name is None:
            continue
        content = _enrich_table_schema(
            table_name=name, schema_text=table_schemas[name], table_meta=meta_map.get(name.lower()), fk_lines=fk_map.get(name)
        )
        if content != chunk.content:
            chunk.content = content
            chunk.token_count = len(content) // 4
            changed.append(name)
    if not changed:
        return 0
    return await _reembed_changed_tables(embeddings, store, chunks, extras, changed)


async def _sync_live_schema_body(
    embedding_service: EmbeddingService | None = None,
    vector_store: QdrantStore | None = None,
    db_id: str = DEFAULT_DB_ID,
    synced_by: str | None = None,
) -> dict[str, Any]:
    """Fetch the live DB schema, chunk per table, embed, and upsert to Qdrant.

    Returns a summary dict with table count and status.
    """
    validate_db_id(db_id)
    from src.sql.sync.orchestrate import orchestration_enabled

    orchestration_on = orchestration_enabled(db_id)

    # Auto-schema Phase 2: ensure feature flags are registered for the database,
    # then scaffold folder if enabled.
    try:
        from src.utils.feature_flags import register_db_feature_flags

        register_db_feature_flags(db_id)
        from src.sql.registry import get_connection

        conn = get_connection(db_id)
        raw_db = conn.get("database") or conn.get("service_name")
        if raw_db:
            from src.sql.connect import derive_db_id

            slug = derive_db_id({"database": raw_db})
            if slug != db_id:
                register_db_feature_flags(slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not register feature flags for %s: %s", db_id, exc)

    from src.sql.introspection.scaffold import scaffold_if_enabled

    scaffold_if_enabled(db_id)

    embeddings = embedding_service or EmbeddingService()
    store = vector_store or QdrantStore(embedding_service=embeddings)

    # Use the TARGET database's engine and connection (not the process-wide default),
    # the same way SQLRetriever and run_readonly_query resolve them.
    from src.sql.registry import resolve_db_engine

    dialect = get_dialect_profile(resolve_db_engine(db_id))
    _query_runner = run_readonly_query

    # 1. Fetch schema rows from the live database
    schema_rows = await _query_runner(dialect.schema_query, max_rows=20000, db_id=db_id)
    if not schema_rows:
        from src.sql.engine import SCHEMA_ENGINES

        message = "No schema rows returned from database"
        if dialect.key in SCHEMA_ENGINES:
            message += (
                ". If your tables are in a schema other than the default one, "
                "set the connection's Schema field and sync again"
            )
        return {"status": "error", "message": message}

    # 2. Split into per-table chunks
    table_schemas = _split_schema_by_table(dialect, schema_rows)

    # 3. Fetch FK info and attach to relevant tables
    fk_map: dict[str, list[str]] = {}
    fk_rows: list[dict[str, Any]] = []
    try:
        if dialect.fk_query:  # MySQL / PostgreSQL / SQL Server / Oracle: one query covers all FKs
            fk_rows = await _query_runner(dialect.fk_query, max_rows=20000, db_id=db_id)
        elif dialect.key == Engine.SQLITE:
            from src.sql.generation import fetch_sqlite_foreign_keys
            fk_rows = await fetch_sqlite_foreign_keys(db_id=db_id)
        else:
            fk_rows = []

        for fk_row in fk_rows:
            from_table = fk_row.get("table_name") or fk_row.get("TABLE_NAME", "")
            from_col = fk_row.get("column_name") or fk_row.get("COLUMN_NAME", "")
            to_table = fk_row.get("referenced_table_name") or fk_row.get("REFERENCED_TABLE_NAME", "")
            to_col = fk_row.get("referenced_column_name") or fk_row.get("REFERENCED_COLUMN_NAME", "")
            if from_table and to_table:
                fk_line = f"  FOREIGN KEY ({from_col}) REFERENCES {to_table}({to_col})"
                fk_map.setdefault(from_table, []).append(fk_line)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not fetch FK info for schema sync: %s", e)

    # If DB introspection gave no FKs (databases without formal FK constraints),
    # fall back to inferred relationships from relationships knowledge pack
    if not fk_map:
        try:
            rel_path = get_knowledge_path("relationships", db_id=db_id)
            if rel_path.exists():
                rel_data = json.loads(rel_path.read_text(encoding="utf-8"))
                rels = rel_data.get("relationships") if isinstance(rel_data, dict) else rel_data
                for r in rels or []:
                    frm, fcol = r.get("from_table"), r.get("from_column")
                    to, tcol = r.get("to_table"), r.get("to_column")
                    if frm and fcol and to and tcol:
                        fk_line = f"  FOREIGN KEY ({fcol}) REFERENCES {to}({tcol})"
                        fk_map.setdefault(frm, []).append(fk_line)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not load fallback inferred relationships: %s", e)

    # 4. Build Chunk objects — one per table, enriched with domain metadata and FKs
    meta_map = _load_table_metadata(db_id=db_id)
    chunks: list[Chunk] = []
    doc_id = get_schema_document_id(db_id)
    for table_name, schema_text in table_schemas.items():
        enriched_content = _enrich_table_schema(
            table_name=table_name,
            schema_text=schema_text,
            table_meta=meta_map.get(table_name.lower()),
            fk_lines=fk_map.get(table_name),
        )

        chunk_id = f"{db_id}_schema_{table_name}"
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                document_id=doc_id,
                chunk_type=ChunkType.SQL_SCHEMA,
                content=enriched_content,
                token_count=len(enriched_content) // 4,  # rough estimate
                document_type=DocumentType.DATABASE,
                source_file=f"database/{db_id}/{table_name}",
                db_id=db_id,
                metadata={"db_id": db_id},
            )
        )

    if not chunks:
        return {"status": "error", "message": "No tables found in schema"}

    # 5. Embed all table chunks FIRST — if embedding fails/rate-limits, old schema remains safe
    meaning_extras: dict[str, str] = {}
    try:  # Auto-schema Phase 9c: column meanings from the previous sync make the tables easier to find
        from src.utils.feature_flags import is_db_feature_enabled as _meaning_enabled

        if _meaning_enabled("auto_column_meaning", db_id):
            from src.sql.semantics.wiring import load_embedding_extras

            meaning_extras = load_embedding_extras(db_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load column meanings for embedding (%s): %s", db_id, exc)
    vectors, sparse_vectors = await _embed_with_meanings(embeddings, chunks, meaning_extras)

    # 6. Upsert new chunks into Qdrant — with deterministic per-table IDs, existing chunks
    # are atomically updated in place with zero downtime or empty-store window
    await store.upsert(chunks, vectors, sparse_vectors)

    # Auto-schema Phase 3: write schema.json and table_inventory.json from the catalog and check
    # they agree with what was just embedded. Per-DB flag, off by default; never fails the sync.
    catalog_report: dict[str, Any] | None = None
    from src.utils.feature_flags import is_db_feature_enabled

    if is_db_feature_enabled("auto_catalog", db_id):
        from src.sql.introspection.catalog import run_catalog_phase

        sa_failure: str | None = None
        if is_db_feature_enabled("auto_sqlalchemy", db_id):
            # SQLAlchemy reader: every readable schema, not just the default one.
            try:
                from src.sql.sa.catalog_phase import run_catalog_phase_sa

                catalog_report = await run_catalog_phase_sa(db_id=db_id, dialect=dialect, chunk_names=set(table_schemas))
            except Exception as exc:  # noqa: BLE001 - fall back to the single-schema extractor below
                sa_failure = f"{type(exc).__name__}: {str(exc)[:200]}"
                logger.warning("SQLAlchemy catalog reader failed for %s, using the native extractor: %s", db_id, sa_failure)

        if catalog_report is None:
            try:
                catalog_report = await run_catalog_phase(
                    db_id=db_id,
                    dialect=dialect,
                    runner=_query_runner,
                    schema_rows=schema_rows,
                    fk_rows=fk_rows,
                    chunk_names=set(table_schemas),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Catalog extraction failed for %s: %s", db_id, exc)
                catalog_report = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            if sa_failure is not None:
                catalog_report["sqlalchemy_fallback"] = sa_failure

    procedures_report: dict[str, Any] | None = None
    procedure_routines: list[Any] = []  # the routines stay in memory only; Phase 5 mines them and they are then dropped
    if is_db_feature_enabled("auto_procedures", db_id):
        try:
            from src.sql.sa.procedures_phase import run_procedures_phase

            procedures_report, procedure_routines = await run_procedures_phase(db_id=db_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Procedure phase failed for %s: %s", db_id, exc)
            procedures_report = {"ok": False, "mode": "basic", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # Auto-schema Phase 5: learn joins, tables and string values from those procedures (facts only; the
    # text is discarded). Needs Phase 4's routines, so it only runs when that phase ran. Never fails the sync.
    sp_evidence_report: dict[str, Any] | None = None
    if procedures_report is not None and procedures_report.get("ok") and is_db_feature_enabled("auto_sp_mining", db_id):
        try:
            from src.sql.sa.mining_phase import run_mining_phase

            mining_kwargs: dict[str, Any] = {}
            if is_db_feature_enabled("auto_column_meaning", db_id):  # Phase 9a: also keep aliases, parameter names, CASE labels
                mining_kwargs["collect_hints"] = True
            sp_evidence_report = await run_mining_phase(
                db_id=db_id, engine_key=procedures_report.get("engine") or "", routines=procedure_routines, **mining_kwargs
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Procedure mining failed for %s: %s", db_id, exc)
            sp_evidence_report = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    procedure_routines = []  # drop the routine text

    # Auto-schema Phase 6: infer relationships from the files the earlier phases wrote (never connects to the
    # database). auto_relationships writes the review file; auto_relationships_apply also publishes the
    # high-confidence edges to relationships.json. Never fails the sync.
    relationships_report: dict[str, Any] | None = None
    if is_db_feature_enabled("auto_relationships", db_id):
        try:
            from src.sql.sa.relationships_phase import run_relationships_phase

            relationships_report = await run_relationships_phase(
                db_id=db_id, apply=is_db_feature_enabled("auto_relationships_apply", db_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Relationship inference failed for %s: %s", db_id, exc)
            relationships_report = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # Auto-schema Phase 7: tag test/backup/temp tables and say which of several similar tables holds the real
    # data. Reads the same files, writes schema/table_profiles.json, hides nothing, and prompts do not read it
    # yet. Per-DB flag, off by default. Never fails the sync.
    table_profiles_report: dict[str, Any] | None = None
    if is_db_feature_enabled("auto_table_profiles", db_id):
        try:
            from src.sql.sa.profiles_phase import run_profiles_phase

            table_profiles_report = await run_profiles_phase(db_id=db_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Table profiles failed for %s: %s", db_id, exc)
            table_profiles_report = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # Auto-schema Phase 8: indirect join routes (2 to 5 joins) between tables that do not join directly, scored with hub
    # damping, built only from trusted edges and left in schema/join_paths.json. Prompts do not read it yet. Per-DB flag,
    # off by default. Runs after relationships and profiles so it sees their fresh files. Never fails the sync.
    join_paths_report: dict[str, Any] | None = None
    if is_db_feature_enabled("auto_join_paths", db_id):
        try:
            from src.sql.sa.paths_phase import run_paths_phase

            join_paths_report = await run_paths_phase(db_id=db_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Join paths failed for %s: %s", db_id, exc)
            join_paths_report = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # Auto-schema Phase 11: the table text embedded above used relationships.json from BEFORE Phase 6 ran. When Phase 6 produced
    # the file this sync, rebuild that text and re-embed only the tables whose lines changed. Only for databases without declared
    # keys (declared keys do not depend on Phase 6). Flag-gated; a failure leaves the old vectors, which stay valid.
    relationship_refresh: int | None = None
    if orchestration_on and not fk_rows and relationships_report is not None and relationships_report.get("ok"):
        try:
            relationship_refresh = await _refresh_relationship_text(db_id, embeddings, store, chunks, table_schemas, meta_map, meaning_extras)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Re-embedding tables whose relationship lines changed failed for %s: %s", db_id, exc)

    # Auto-schema Phase 9: column meanings. Evidence from the earlier phases' files, one batched model call per table whose
    # evidence changed (metadata only), strict checks, then the glossary (high confidence only) and re-embedding of just
    # the tables whose meanings changed. A first run on a large database is spread over several syncs by design.
    # Per-DB flag; never fails the sync.
    column_meaning_report: dict[str, Any] | None = None
    if is_db_feature_enabled("auto_column_meaning", db_id):
        try:
            from src.sql.sa.meaning_phase import run_meaning_phase
            from src.sql.semantics.wiring import load_embedding_extras

            column_meaning_report = await run_meaning_phase(db_id=db_id, inject=is_db_feature_enabled("auto_prompt_inject", db_id))
            changed = column_meaning_report.pop("embed_tables", [])
            if changed:
                try:
                    column_meaning_report["reembedded_tables"] = await _reembed_changed_tables(
                        embeddings, store, chunks, load_embedding_extras(db_id), changed
                    )
                except Exception as exc:  # noqa: BLE001 - the old vectors stay valid
                    logger.warning("Re-embedding changed column meanings failed for %s: %s", db_id, exc)
                    column_meaning_report["reembed_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Column meaning failed for %s: %s", db_id, exc)
            column_meaning_report = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # 7. Invalidate in-memory schema cache on SQLRetriever so next query picks up new schema
    try:
        from src.sql.pipeline import SQLRetriever
        SQLRetriever.clear_schema_cache(db_id=db_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not clear SQLRetriever schema cache: %s", e)

    logger.info(
        "Schema sync complete: %d tables embedded and upserted to Qdrant for %s",
        len(chunks),
        db_id,
    )

    # Auto-schema Phase 2: a database that was scaffolded inactive becomes active only now,
    # after its first successful sync. Flag-gated, never raises.
    from src.sql.introspection.scaffold import activate_after_sync

    activate_after_sync(db_id, synced_by)

    result: dict[str, Any] = {
        "status": "ok",
        "db_id": db_id,
        "document_id": doc_id,
        "tables_synced": len(chunks),
        "table_names": sorted(table_schemas.keys()),
    }
    if catalog_report is not None:
        result["catalog"] = catalog_report
    if procedures_report is not None:
        result["procedures"] = procedures_report
    if sp_evidence_report is not None:
        result["sp_evidence"] = sp_evidence_report
    if relationships_report is not None:
        result["relationships"] = relationships_report
    if table_profiles_report is not None:
        result["table_profiles"] = table_profiles_report
    if join_paths_report is not None:
        result["join_paths"] = join_paths_report
    if column_meaning_report is not None:
        result["column_meaning"] = column_meaning_report
    if orchestration_on:
        from src.sql.sync.report import schema_fingerprint

        result["_sync"] = {
            "schema_hash": schema_fingerprint(table_schemas),
            "reembedded_for_relationships": relationship_refresh,
        }
    return result


async def sync_live_schema(
    embedding_service: EmbeddingService | None = None,
    vector_store: QdrantStore | None = None,
    db_id: str = DEFAULT_DB_ID,
    synced_by: str | None = None,
) -> dict[str, Any]:
    """Fetch the live DB schema, chunk per table, embed, and upsert to Qdrant; return a summary dict.

    With the per-database ``auto_sync_orchestration`` flag on (Phase 11), the sync runs under a guard that snapshots the generated
    files, validates what the phases wrote, rolls a broken result back, clears caches and adds an ``orchestration`` report.
    With it off this is exactly the sync body, as before.
    """
    validate_db_id(db_id)
    from src.sql.sync.orchestrate import orchestration_enabled, run_orchestrated

    async def body() -> dict[str, Any]:
        return await _sync_live_schema_body(embedding_service, vector_store, db_id, synced_by)

    if not orchestration_enabled(db_id):
        return await body()
    return await run_orchestrated(body, db_id)
