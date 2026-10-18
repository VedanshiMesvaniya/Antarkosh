"""Which database a question is answered from, decided by the server for every request.

Order: the database id the request names, else the user's own current database, else the default
database (exactly the behaviour before per-user databases existed). Access and "is it active" are
checked the same way for both sources. A user whose chosen database is no longer available is told so;
they are never silently moved to a different database.

Origin: new (no previous file)
"""

from __future__ import annotations

from fastapi import HTTPException

from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id


def resolve_target_db(requested: str | None, user: str) -> str:
    from src.core.active_db import get_active_db
    from src.sql.registry import can_access_database, is_database_active

    explicit = requested is not None
    chosen = requested if explicit else get_active_db(user)
    if chosen is None:
        return DEFAULT_DB_ID

    try:
        validate_db_id(chosen)
    except ValueError as e:
        if explicit:
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=409, detail="Your selected database is no longer valid. Choose another database.")
    target = chosen.strip()

    if not can_access_database(target, user):
        if explicit:
            raise HTTPException(status_code=403, detail="Access denied to this database")
        raise HTTPException(
            status_code=409,
            detail=f"Your selected database {target!r} is not available to you any more. Choose another database.",
        )
    if not is_database_active(target):
        if explicit:
            raise HTTPException(
                status_code=409,
                detail="This database is not active yet. An admin must sync its schema first.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Your selected database {target!r} is not active yet. An admin must sync its schema first.",
        )
    return target
