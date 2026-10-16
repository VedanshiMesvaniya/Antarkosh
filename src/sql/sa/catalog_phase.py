"""The SQLAlchemy catalog phase of a schema sync (auto-schema Phase 3, multi-schema).

Connects with the database's own saved connection through SQLAlchemy, reads the catalog of EVERY
readable schema (or only the connection's pinned ``schema_name``), writes the same two files as the
native extractor (``schema.json``, ``table_inventory.json``) with schema-qualified names, and checks
that they agree. Only catalog reads run; nothing touches data rows.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.sql.dialects.base import SQLDialectProfile
from src.sql.introspection.catalog import (
    catalog_from_reflection,
    check_consistency,
    inventory_document,
    schema_document,
    write_catalog_files,
)
from src.sql.sa.engine_factory import create_sa_engine, resolve_connection
from src.sql.sa.estimates import ROW_ESTIMATE_SQL
from src.sql.sa.reflection import reflect_database

logger = logging.getLogger(__name__)

SA_TIMEOUT_SECONDS = 180


def requested_schemas(cfg: dict[str, Any], dialect_key: str) -> list[str] | None:
    """The connection's pinned schema as a one-item list, else None (= every readable schema)."""
    pinned = (cfg.get("schema_name") or "").strip() if dialect_key in ("postgresql", "oracle") else ""
    return [pinned] if pinned else None


async def run_catalog_phase_sa(
    *,
    db_id: str,
    dialect: SQLDialectProfile,
    chunk_names: set[str] | None = None,
    databases_dir: Path | None = None,
    timeout: float = SA_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read, write and check the multi-schema catalog for ``db_id``; returns a report dict."""
    engine_key, cfg = resolve_connection(db_id)
    schemas = requested_schemas(cfg, engine_key)
    warnings: list[str] = []
    unavailable: list[str] = []

    engine = create_sa_engine(db_id)
    try:
        async with engine.connect() as conn:
            reflection = await asyncio.wait_for(
                conn.run_sync(lambda c: reflect_database(c, engine_key, schemas)), timeout=timeout
            )
            estimates: list[dict[str, Any]] | None = None
            sql = ROW_ESTIMATE_SQL.get(engine_key)
            if sql is None:
                unavailable.append("row_estimates")  # this engine offers no cheap estimate
            else:
                try:
                    estimates = [dict(r._mapping) for r in await asyncio.wait_for(conn.execute(text(sql)), timeout=timeout)]
                except Exception as exc:  # noqa: BLE001 - optional
                    unavailable.append("row_estimates")
                    warnings.append(f"row_estimates query failed: {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        await engine.dispose()

    # Question answering currently covers one schema: the pinned one, else the database's default.
    answering = schemas[0] if schemas else reflection.get("default_schema")
    cat = catalog_from_reflection(
        db_id, engine_key, reflection, estimates=estimates, answering_schema=answering,
        warnings=warnings, unavailable=unavailable,
    )
    schema_doc, inventory_doc = schema_document(cat), inventory_document(cat)
    # The embedded schema chunks come from the single-schema sync, so they are not compared here.
    check = check_consistency(cat, schema_doc, inventory_doc, None)
    files = write_catalog_files(db_id, schema_doc, inventory_doc, databases_dir=databases_dir)
    counts = inventory_doc["counts"]
    return {
        "engine": "sqlalchemy",
        "ok": check["ok"],
        "tables": counts["tables"],
        "views": counts["views"],
        "schemas": {s: dict(c) for s, c in sorted(cat.schemas.items())},
        "skipped_schemas": dict(sorted(reflection.get("skipped_schemas", {}).items())),
        "foreign_keys": len(cat.foreign_keys),
        "cross_schema_foreign_keys": sum(1 for f in cat.foreign_keys if f.get("from_schema") != f.get("to_schema")),
        "name_collisions": dict(sorted(cat.name_collisions.items())),
        "answering_scope": inventory_doc["answering_scope"],
        "problems": check["problems"],
        "warnings": cat.warnings,
        "unavailable": sorted(set(cat.unavailable)),
        "files": files,
    }


__all__ = ["requested_schemas", "run_catalog_phase_sa"]
