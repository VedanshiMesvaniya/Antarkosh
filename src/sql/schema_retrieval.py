"""Schema retrieval and DDL parsing utilities for text-to-SQL.

Extracts relevant DB schema chunks using Schema RAG against Qdrant,
with dynamic token budget selection, domain anchor injection, 1-hop graph
expansion, and scoped schema fallback.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from src.core.config import settings
from src.core.db_client import run_readonly_query
from src.core.sql_dialects import SQLDialectProfile, get_dialect_profile
from src.models.schemas import Chunk, ChunkType, DocumentType
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID, get_knowledge_path, validate_db_id
from src.sql.table_router import route_anchor_tables, route_tables_for_query
from src.utils.feature_flags import is_feature_enabled
from src.sql.schema_budget import DEFAULT_SCHEMA_TOKEN_BUDGET, select_schema_within_budget
from src.sql.schema_compactor import compact_ddl, extract_join_hints
from src.sql.schema_token_estimator import estimate_schema_tokens
from src.utils.telemetry import log_telemetry
from src.rag.stages.s10_embeddings import EmbeddingService
from src.rag.stages.s11_vector_store import QdrantStore

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


def _get_run_readonly_query():
    """Resolve run_readonly_query, honoring any monkeypatches on src.pipeline.schema_ingestion."""
    mod = sys.modules.get("src.pipeline.schema_ingestion")
    if mod and hasattr(mod, "run_readonly_query"):
        return mod.run_readonly_query
    return run_readonly_query


async def sync_live_schema(
    embedding_service: EmbeddingService | None = None,
    vector_store: QdrantStore | None = None,
    db_id: str = DEFAULT_DB_ID,
) -> dict[str, Any]:
    """Fetch the live DB schema, chunk per table, embed, and upsert to Qdrant.

    Returns a summary dict with table count and status.
    """
    validate_db_id(db_id)
    embeddings = embedding_service or EmbeddingService()
    store = vector_store or QdrantStore(embedding_service=embeddings)

    dialect = get_dialect_profile(settings.db_engine)
    _query_runner = _get_run_readonly_query()

    # 1. Fetch schema rows from the live database
    schema_rows = await _query_runner(dialect.schema_query, max_rows=20000)
    if not schema_rows:
        return {"status": "error", "message": "No schema rows returned from database"}

    # 2. Split into per-table chunks
    table_schemas = _split_schema_by_table(dialect, schema_rows)

    # 3. Fetch FK info and attach to relevant tables
    fk_map: dict[str, list[str]] = {}
    try:
        if dialect.fk_query:  # MySQL / PostgreSQL / SQL Server / Oracle: one query covers all FKs
            fk_rows = await _query_runner(dialect.fk_query, max_rows=20000)
        elif dialect.key == Engine.SQLITE:
            from src.stages.s12b_sql_retrieval import fetch_sqlite_foreign_keys
            fk_rows = await fetch_sqlite_foreign_keys()
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
    vectors, sparse_vectors = await embeddings.embed_chunks(chunks)

    # 6. Upsert new chunks into Qdrant — with deterministic per-table IDs, existing chunks
    # are atomically updated in place with zero downtime or empty-store window
    await store.upsert(chunks, vectors, sparse_vectors)

    # 7. Invalidate in-memory schema cache on SQLRetriever so next query picks up new schema
    try:
        from src.stages.s12b_sql_retrieval import SQLRetriever
        SQLRetriever.clear_schema_cache(db_id=db_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not clear SQLRetriever schema cache: %s", e)

    logger.info(
        "Schema sync complete: %d tables embedded and upserted to Qdrant for %s",
        len(chunks),
        db_id,
    )

    return {
        "status": "ok",
        "db_id": db_id,
        "document_id": doc_id,
        "tables_synced": len(chunks),
        "table_names": sorted(table_schemas.keys()),
    }

