"""Database management endpoints for UI API — listing, metadata, access control, connection test & save.

Origin: extracted from src/api/ui.py (database routes, DBConnectionPayload)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from src.api.auth import get_current_user, require_admin, require_db_admin

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
    schema_name: str | None = None  # PostgreSQL/Oracle: schema to work in. None = keep stored, "" = clear
    username: str = ""
    password: str | None = None
    path: str | None = None

    @field_validator("port", mode="before")
    @classmethod
    def _blank_port_is_none(cls, v: Any) -> Any:
        return None if v == "" else v


class DatabaseAccessPayload(BaseModel):
    allowed_users: list[str]


def _ensure_can_manage(db_id: str, admin: str) -> None:
    """403 unless this admin owns ``db_id`` (the super admin may manage any; a brand-new id is free to claim)."""
    from src.sql.registry import can_manage_database

    if not can_manage_database(db_id, admin):
        raise HTTPException(status_code=403, detail="This database belongs to another admin.")


@router.get("/databases")
async def list_databases_endpoint(
    current_user: str = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List databases accessible to the current user (admins see all)."""
    from src.sql.registry import can_access_database, is_database_active, list_databases

    dbs = list_databases()
    if current_user not in ("*", "all", "admin"):
        dbs = [
            db
            for db in dbs
            if can_access_database(db.get("id", ""), current_user) and is_database_active(db.get("id", ""))
        ]
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
    admin: str = Depends(require_db_admin),
) -> list[dict[str, Any]]:
    """List the databases this admin may manage: all of them for the super admin, otherwise their own."""
    from src.sql.registry import can_manage_database, list_databases

    dbs = [db for db in list_databases() if can_manage_database(db.get("id", ""), admin)]
    return [
        {
            "id": db.get("id"),
            "name": db.get("display_name") or db.get("id"),
            "display_name": db.get("display_name") or db.get("id"),
            "description": db.get("description", ""),
            "engine": db.get("engine", ""),
            "enabled": db.get("enabled", True),
            "allowed_users": db.get("allowed_users", []),
            "owner": db.get("owner"),
            "synced_by": (db.get("schema_sync") or {}).get("synced_by"),
            "last_synced_at": (db.get("schema_sync") or {}).get("last_synced_at"),
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
    admin: str = Depends(require_db_admin),
) -> dict[str, Any]:
    """Get allowed users for a database (admin only)."""
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import get_database

    try:
        validate_db_id(db_id)
        _ensure_can_manage(db_id, admin)
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
    admin: str = Depends(require_db_admin),
) -> dict[str, Any]:
    """Update allowed users for a database (admin only)."""
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import get_database, update_database_access

    try:
        validate_db_id(db_id)
        _ensure_can_manage(db_id, admin)
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


# NOTE: these two routes ('/databases/connect' and '/databases/connect/test') MUST stay above every
# '/databases/{db_id}/...' route below. FastAPI matches in order, and '{db_id}' would otherwise swallow
# the word 'connect' and send the add-a-database form to the wrong handler.
class DBConnectPayload(DBConnectionPayload):
    db_id: str | None = None  # optional: leave empty and the server derives one from the database's name


async def _prepare_connect(body: DBConnectPayload, admin: str):
    """Validate a connection an admin is adding and choose its id. Returns (db_id, existing, engine, fields)."""
    from src.sql.connect import choose_db_id
    from src.sql.engine import REQUIRED_FIELDS, SCHEMA_ENGINES, Engine, validate_schema_name
    from src.sql.registry import get_connection

    fields = body.model_dump()
    requested = (fields.pop("db_id", None) or "").strip() or None

    try:
        engine = Engine.from_value(fields["engine"])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if engine.value == "sqlite":
        raise HTTPException(status_code=422, detail="SQLite uses the local file and cannot be added here.")
    fields["engine"] = engine.value

    # The connectors fill any blank host / user / password / database from the app's own database
    # settings, so a half-filled form would be "tested" against the wrong server and pass. Require
    # every field up front so only what the admin typed is ever tested or saved.
    for name in REQUIRED_FIELDS[engine.value]:
        if not str(fields.get(name) or "").strip():
            raise HTTPException(status_code=422, detail=f"{name.replace('_', ' ').capitalize()} is required.")

    if fields.get("schema_name") is not None:
        try:
            fields["schema_name"] = validate_schema_name(fields["schema_name"])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if fields["schema_name"] and engine.value not in SCHEMA_ENGINES:
            raise HTTPException(status_code=422, detail="Schema is only supported for PostgreSQL and Oracle.")

    try:
        db_id, existing = choose_db_id(admin, fields, requested)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if fields.get("password") in (None, ""):
        stored = get_connection(db_id)
        if stored.get("password"):
            fields["password"] = stored["password"]
        else:
            raise HTTPException(status_code=422, detail="Password is required.")
    return db_id, existing, engine, fields


@router.get("/admin/database-engines")
async def database_engines_endpoint(_admin: str = Depends(require_db_admin)) -> dict[str, Any]:
    """The database types the connection form offers (no saved connection details)."""
    from src.core import db_settings

    return db_settings.engine_catalog()


@router.post("/databases/connect/test")
async def test_connect_database_endpoint(
    body: DBConnectPayload,
    admin: str = Depends(require_db_admin),
) -> dict[str, Any]:
    """Test a connection the same way ``/databases/connect`` would, without saving anything."""
    from src.sql.connectors import get_connector

    db_id, existing, engine, fields = await _prepare_connect(body, admin)
    try:
        await get_connector(engine, cfg=fields).test()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "db_id": db_id, "existing": existing}


@router.post("/databases/connect")
async def connect_database_endpoint(
    body: DBConnectPayload,
    admin: str = Depends(require_db_admin),
) -> dict[str, Any]:
    """Connect ANY database: test it, save it under its own id, create its folder, record the owner.

    No id is needed. The id comes from the database's name (``QTS_AIS`` -> ``qts_ais``); connecting the same
    database again as the same admin updates it instead of creating a duplicate. The default database is
    never touched, and one admin can never overwrite another admin's database.
    """
    from src.core import db_settings
    from src.sql.connectors import get_connector
    from src.sql.engine import SCHEMA_ENGINES
    from src.sql.registry import get_database_owner, save_connection

    db_id, existing, engine, fields = await _prepare_connect(body, admin)

    try:  # test before saving: nothing is written if it fails
        await get_connector(engine, cfg=fields).test()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    if engine.value not in SCHEMA_ENGINES:
        fields["schema_name"] = ""
    save_connection(db_id, fields)
    try:
        db_settings._apply_runtime(fields, db_id=db_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to apply runtime caches for %s: %s", db_id, e)

    folder_created = False
    try:
        from src.sql.introspection.scaffold import ensure_db_folder

        folder_created = ensure_db_folder(db_id, engine=engine.value, owner=admin).created
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not create the folder for %s: %s", db_id, e)

    return {
        "ok": True,
        "status": "saved",
        "db_id": db_id,
        "existing": existing,
        "folder_created": folder_created,
        "owner": get_database_owner(db_id),
    }


@router.post("/databases/{db_id}/connection/test")
@router.post("/databases/{db_id}/test")
async def test_database_connection_endpoint(
    db_id: str,
    body: DBConnectionPayload,
    admin: str = Depends(require_db_admin),
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
    _ensure_can_manage(db_id, admin)

    fields = body.model_dump()
    if fields.get("password") is None or fields.get("password") == "":
        existing_cfg = get_connection(db_id)
        if existing_cfg.get("password"):
            fields["password"] = existing_cfg["password"]

    try:
        engine = Engine.from_value(fields["engine"])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    from src.sql.engine import SCHEMA_ENGINES, validate_schema_name

    if fields.get("schema_name") is None:  # not sent: the form is about the stored connection
        stored_schema = get_connection(db_id).get("schema_name")
        if stored_schema:
            fields["schema_name"] = stored_schema
    if fields.get("schema_name") is not None:
        try:
            fields["schema_name"] = validate_schema_name(fields["schema_name"])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if fields["schema_name"] and engine.value not in SCHEMA_ENGINES:
            raise HTTPException(status_code=422, detail="Schema is only supported for PostgreSQL and Oracle.")

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
    admin: str = Depends(require_db_admin),
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
    _ensure_can_manage(db_id, admin)

    fields = body.model_dump()
    if fields.get("password") is None or fields.get("password") == "":
        existing_cfg = get_connection(db_id)
        if existing_cfg.get("password"):
            fields["password"] = existing_cfg["password"]

    try:
        engine = Engine.from_value(fields["engine"])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    from src.sql.engine import SCHEMA_ENGINES, validate_schema_name

    if fields.get("schema_name") is None:  # not sent: the form is about the stored connection
        stored_schema = get_connection(db_id).get("schema_name")
        if stored_schema:
            fields["schema_name"] = stored_schema
    if fields.get("schema_name") is not None:
        try:
            fields["schema_name"] = validate_schema_name(fields["schema_name"])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if fields["schema_name"] and engine.value not in SCHEMA_ENGINES:
            raise HTTPException(status_code=422, detail="Schema is only supported for PostgreSQL and Oracle.")

    # Test before saving — nothing written if test fails
    try:
        connector = get_connector(engine, cfg=fields)
        await connector.test()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    if engine.value not in SCHEMA_ENGINES:
        fields["schema_name"] = ""  # an engine without schemas must not keep an old schema setting

    save_connection(db_id, fields)

    # Invalidate runtime caches
    try:
        db_settings._apply_runtime(fields, db_id=db_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to apply runtime caches for %s: %s", db_id, e)

    # Adding a connection is an explicit admin action, so the database gets its folder now (never for
    # the default database, never touching a folder that already exists). Failure here never fails the save.
    folder_created = False
    from src.sql.knowledge.loaders import DEFAULT_DB_ID

    if db_id != DEFAULT_DB_ID:
        try:
            from src.sql.introspection.scaffold import ensure_db_folder

            folder_created = ensure_db_folder(db_id, engine=engine.value, owner=admin).created
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not create the folder for %s: %s", db_id, e)

    return {"ok": True, "status": "saved", "db_id": db_id, "folder_created": folder_created}


@router.get("/databases/{db_id}/connection")
async def get_database_connection_endpoint(
    db_id: str,
    admin: str = Depends(require_db_admin),
) -> dict[str, Any]:
    """The saved connection of one database, for the edit form (admin only). The password is never returned."""
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.registry import get_connection

    try:
        validate_db_id(db_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _ensure_can_manage(db_id, admin)

    cfg = get_connection(db_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="No connection saved for this database")
    return {
        "db_id": db_id,
        "engine": cfg.get("engine", ""),
        "host": cfg.get("host", ""),
        "port": cfg.get("port", ""),
        "database": cfg.get("database", ""),
        "username": cfg.get("username", ""),
        "service_name": cfg.get("service_name", ""),
        "schema_name": cfg.get("schema_name", ""),
        "odbc_driver": cfg.get("odbc_driver", ""),
        "password_set": bool(cfg.get("password")),
    }
