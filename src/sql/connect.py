"""Choosing the database id for a connection an admin is adding.

A connection is identified by WHAT it points at (engine, host, port, database, schema), never by its
credentials. The same admin connecting the same database again updates the same id and folder; a
different database gets a new id derived from its name; another admin connecting the same server gets
their own separate id, folder and ownership.

Origin: new (no previous file)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

_MAX_BASE = 34  # leaves room for a "_NN" suffix inside the 40-character limit


def connection_fingerprint(cfg: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """(engine, host, port, database-or-service, schema), lower-cased. Credentials are not part of it."""
    port = cfg.get("port")
    try:
        port_text = str(int(port)) if port not in (None, "") else ""
    except (TypeError, ValueError):
        port_text = str(port)
    target = cfg.get("database") or cfg.get("service_name") or ""
    return (
        str(cfg.get("engine") or "").lower(),
        str(cfg.get("host") or "").strip().lower(),
        port_text,
        str(target).strip().lower(),
        str(cfg.get("schema_name") or "").strip().lower(),
    )


def derive_db_id(cfg: dict[str, Any]) -> str:
    """A valid database id from the database's name: ``QTS_AIS`` -> ``qts_ais``."""
    raw = str(cfg.get("database") or cfg.get("service_name") or cfg.get("host") or "db")
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"db_{slug}".rstrip("_")
    slug = slug[:_MAX_BASE].rstrip("_")
    if len(slug) < 2:
        slug = f"db_{slug}".rstrip("_")
    if slug in (DEFAULT_DB_ID, "template"):
        slug = f"{slug}_db"
    validate_db_id(slug)
    return slug


def choose_db_id(
    admin: str,
    cfg: dict[str, Any],
    requested: str | None = None,
    databases_dir: Path | None = None,
    conn_path: Path | None = None,
) -> tuple[str, bool]:
    """Return (db_id, already_exists) for a connection being added by ``admin``.

    Raises ValueError for an invalid id (or a request to overwrite erp_main through this form) and
    PermissionError if the caller is not an authorized administrator. Any other connection, even one
    with the same server details as erp_main, is simply a new database with its own id and folder.
    """
    base = databases_dir or registry.DATABASES_DIR
    fingerprint = connection_fingerprint(cfg)

    if requested:
        validate_db_id(requested)
        if requested == DEFAULT_DB_ID:
            raise ValueError("The main database is edited with its own settings, not through this form.")
        if not registry.can_manage_database(requested, admin, databases_dir=base):
            raise PermissionError("Access denied: Admin access required.")
        existing = bool(registry.get_connection(requested, conn_path=conn_path)) or (base / requested).exists()
        return requested, existing

    for existing_id in registry.list_connection_ids(conn_path=conn_path):
        if existing_id == DEFAULT_DB_ID:
            continue
        if connection_fingerprint(registry.get_connection(existing_id, conn_path=conn_path)) != fingerprint:
            continue
        return existing_id, True  # same database: update it in place, keep its folder

    candidate = derive_db_id(cfg)
    n = 1
    while registry.get_connection(candidate, conn_path=conn_path) or (base / candidate).exists():
        n += 1
        if n > 99:
            raise ValueError("Could not find a free database id for this connection.")
        candidate = f"{derive_db_id(cfg)}_{n}"
    return candidate, False


__all__ = ["choose_db_id", "connection_fingerprint", "derive_db_id"]
