"""Each user's current database: read it, choose it, or go back to the default.

A choice belongs to the logged-in user alone, so two people (or two clients) using the app at the
same time never change each other's database.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.auth import get_current_user
from src.core import active_db
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)
router = APIRouter(tags=["databases"])


class ActiveDatabasePayload(BaseModel):
    db_id: str | None = None  # None / empty = the default database


@router.get("/me/database")
async def get_my_database(current_user: str = Depends(get_current_user)) -> dict[str, Any]:
    """The database this user's questions are answered from, plus the ones they may choose."""
    from src.sql.registry import can_access_database, is_database_active, list_databases

    selected = active_db.get_active_db(current_user)
    options = [
        {"id": db.get("id"), "name": db.get("display_name") or db.get("id"), "engine": db.get("engine", "")}
        for db in list_databases()
        if db.get("id") != DEFAULT_DB_ID
        and can_access_database(db.get("id", ""), current_user)
        and is_database_active(db.get("id", ""))
    ]
    return {"active": selected, "effective": selected or DEFAULT_DB_ID, "default": DEFAULT_DB_ID, "options": options}


@router.put("/me/database")
async def set_my_database(
    body: ActiveDatabasePayload,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Choose the database for this user only. Empty goes back to the default database."""
    from src.sql.registry import can_access_database, is_database_active

    target = (body.db_id or "").strip()
    if not target or target == DEFAULT_DB_ID:
        active_db.set_active_db(current_user, None)
        return {"active": None, "effective": DEFAULT_DB_ID}
    try:
        validate_db_id(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not can_access_database(target, current_user):
        raise HTTPException(status_code=403, detail="Access denied to this database")
    if not is_database_active(target):
        raise HTTPException(status_code=409, detail="This database is not active yet. An admin must sync its schema first.")
    active_db.set_active_db(current_user, target)
    return {"active": target, "effective": target}
