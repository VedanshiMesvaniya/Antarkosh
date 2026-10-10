"""Database management endpoints for UI API — listing, metadata, access control, connection test & save."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from src.api.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)
router = APIRouter()


class DBConnectionPayload(BaseModel):
    """Body for database connection configuration forms."""

    engine: str
    host: str = ""
    port: int | None = None
    database: str = ""
    service_name: str = ""     # Oracle: used instead of "database"
    odbc_driver: str = ""      # SQL Server: ODBC driver name installed on the server
    username: str = ""
    password: str | None = None
    path: str | None = None

    @field_validator("port", mode="before")
    @classmethod
    def _blank_port_is_none(cls, v: Any) -> Any:
        return None if v == "" else v


class DatabaseAccessPayload(BaseModel):
    allowed_users: list[str]


@router.get("/databases")
async def list_databases_endpoint(
    current_user: str = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List databases accessible to the current user (admins see all)."""
    from src.sql.registry import can_access_database, list_databases

    dbs = list_databases()
    if current_user not in ("*", "all", "admin"):
        dbs = [db for db in dbs if can_access_database(db.get("id", ""), current_user)]
    return [
        {
            "id": db.get("id"),
            "name": db.get("display_name") or db.get("id"),
            "display_name": db.get("display_name") or db.get("id"),
            "description": db.get("description", ""),
            "engine": db.get("engine", ""),
            "enabled": db.get("enabled", True),
            "allowed_users": db.get("allowed_users", []),
        }
        for db in dbs
    ]


@router.get("/admin/databases")
async def admin_list_databases_endpoint(
    _admin: str = Depends(require_admin),
) -> list[dict[str, Any]]:
    """List all configured databases (admin only)."""
    from src.sql.registry import list_databases

    dbs = list_databases()
    return [
        {
            "id": db.get("id"),
            "name": db.get("display_name") or db.get("id"),
            "display_name": db.get("display_name") or db.get("id"),
            "description": db.get("description", ""),
            "engine": db.get("engine", ""),
            "enabled": db.get("enabled", True),
            "allowed_users": db.get("allowed_users", []),
        }
        for db in dbs
    ]


@router.get("/databases/{db_id}")
async def get_database_endpoint(
    db_id: str,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Get metadata for a specific database (requires user access)."""
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import can_access_database, get_database

    try:
        validate_db_id(db_id)
        db_meta = get_database(db_id)
    except (FileNotFoundError, KeyError):
        raise HTTPException(status_code=404, detail="Database not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not can_access_database(db_id, current_user):
        raise HTTPException(status_code=403, detail="Access denied to this database")

    return {
        "id": db_meta.get("id"),
        "name": db_meta.get("display_name") or db_meta.get("id"),
        "display_name": db_meta.get("display_name") or db_meta.get("id"),
        "description": db_meta.get("description", ""),
        "engine": db_meta.get("engine", ""),
        "enabled": db_meta.get("enabled", True),
        "allowed_users": db_meta.get("allowed_users", []),
    }


@router.get("/databases/{db_id}/access")
async def get_database_access_endpoint(
    db_id: str,
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Get allowed users for a database (admin only)."""
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import get_database

    try:
        validate_db_id(db_id)
        db_meta = get_database(db_id)
    except (FileNotFoundError, KeyError):
        raise HTTPException(status_code=404, detail="Database not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "db_id": db_id,
        "allowed_users": db_meta.get("allowed_users", ["admin"]),
    }


@router.post("/databases/{db_id}/access")
async def update_database_access_endpoint(
    db_id: str,
    body: DatabaseAccessPayload,
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Update allowed users for a database (admin only)."""
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import get_database, update_database_access

    try:
        validate_db_id(db_id)
        ok = update_database_access(db_id, body.allowed_users)
    except (FileNotFoundError, KeyError):
        raise HTTPException(status_code=404, detail="Database not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not ok:
        raise HTTPException(status_code=404, detail="Database not found")

    db_meta = get_database(db_id)
    return {
        "status": "ok",
        "db_id": db_id,
        "allowed_users": db_meta.get("allowed_users", body.allowed_users),
    }


@router.post("/databases/{db_id}/connection/test")
@router.post("/databases/{db_id}/test")
async def test_database_connection_endpoint(
    db_id: str,
    body: DBConnectionPayload,
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Test connection credentials for a database without saving (admin only)."""
    from src.sql.connectors import get_connector
    from src.sql.engine import Engine
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import get_connection

    try:
        validate_db_id(db_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    fields = body.model_dump()
    if fields.get("password") is None or fields.get("password") == "":
        existing_cfg = get_connection(db_id)
        if existing_cfg.get("password"):
            fields["password"] = existing_cfg["password"]

    try:
        engine = Engine.from_value(fields["engine"])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        connector = get_connector(engine, cfg=fields)
        await connector.test()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    return {"ok": True, "db_id": db_id}


@router.post("/databases/{db_id}/save")
@router.post("/databases/{db_id}/connection")
async def save_database_connection_endpoint(
    db_id: str,
    body: DBConnectionPayload,
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Test credentials and save connection for a database (admin only)."""
    from src.core import db_settings
    from src.sql.connectors import get_connector
    from src.sql.engine import Engine
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import get_connection, save_connection

    try:
        validate_db_id(db_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    fields = body.model_dump()
    if fields.get("password") is None or fields.get("password") == "":
        existing_cfg = get_connection(db_id)
        if existing_cfg.get("password"):
            fields["password"] = existing_cfg["password"]

    try:
        engine = Engine.from_value(fields["engine"])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Test before saving — nothing written if test fails
    try:
        connector = get_connector(engine, cfg=fields)
        await connector.test()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    save_connection(db_id, fields)

    # Invalidate runtime caches
    try:
        db_settings._apply_runtime(fields, db_id=db_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to apply runtime caches for %s: %s", db_id, e)

    return {"ok": True, "status": "saved", "db_id": db_id}
