"""Admin-editable live-database connection: validate -> test -> save to config file -> hot-apply.

The connection is persisted in ``config/db_connection.json`` (or ``$DB_CONFIG_FILE``)
by ``db_config_file`` — not in ``.env`` — so it does not depend on how the host
provides environment variables.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core import db_config_file
from src.core.config import settings
from src.sql.engine import ENGINES
from src.sql.engine import REQUIRED_FIELDS as _REQUIRED

logger = logging.getLogger(__name__)

_save_lock = asyncio.Lock()


class DBSettingsError(ValueError):
    """User-facing validation / connection error (never contains the password)."""


def current_config() -> dict[str, Any]:
    """What the UI may see. The password is never returned, only whether one is set."""
    return {
        "engine": settings.db_engine,
        "host": settings.db_host,
        "port": settings.db_port,
        "database": settings.db_name if settings.db_engine != "oracle" else "",
        "service_name": settings.db_name if settings.db_engine == "oracle" else "",
        "odbc_driver": settings.db_odbc_driver,
        "username": settings.db_readonly_user,
        "password_set": bool(settings.db_readonly_password),
        "engines": [{"key": k, **{f: v for f, v in s.items()}} for k, s in ENGINES.items()],
    }


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    engine = str(payload.get("engine", "")).strip().lower()
    spec = ENGINES.get(engine)
    if spec is None:
        raise DBSettingsError(f"Unknown database type {engine!r}.")
    if not spec["ready"]:
        raise DBSettingsError(f"{spec['label']} is not supported by the query layer yet.")

    out: dict[str, Any] = {"engine": engine}
    if engine == "sqlite":
        return out                       # SQLite: only the engine switches; stored server values stay put

    out["host"] = str(payload.get("host") or "").strip()
    out["username"] = str(payload.get("username") or "").strip()

    if engine == "oracle":
        svc = str(payload.get("service_name") or payload.get("database") or "").strip()
        out["service_name"] = svc
        out["database"] = svc
    elif engine == "mssql":
        out["database"] = str(payload.get("database") or "").strip()
        out["odbc_driver"] = str(payload.get("odbc_driver") or settings.db_odbc_driver or "ODBC Driver 18 for SQL Server").strip()
    else:
        out["database"] = str(payload.get("database") or "").strip()
    try:
        out["port"] = int(payload.get("port") or spec["default_port"] or 0)
    except (TypeError, ValueError):
        raise DBSettingsError("Port must be a number.") from None

    pw = payload.get("password")         # None = keep the stored password
    if pw is None:
        target = (engine, out["host"], out["port"], out["username"])
        stored = (settings.db_engine, settings.db_host, settings.db_port, settings.db_readonly_user)
        if settings.db_readonly_password and target != stored:
            # Never send the stored password to a different server/user/engine.
            raise DBSettingsError(
                "Re-enter the password when changing database type, host, port or username."
            )
        pw = settings.db_readonly_password
    out["password"] = str(pw)

    for f in _REQUIRED[engine]:
        if not out[f]:
            raise DBSettingsError(f"{f.capitalize()} is required.")
    if not (1 <= out["port"] <= 65535):
        raise DBSettingsError("Port must be between 1 and 65535.")
    for k, v in out.items():
        if isinstance(v, str) and any(c in v for c in "\r\n\x00"):
            raise DBSettingsError(f"{k} contains an invalid character.")
    return out


async def _test_mysql(cfg: dict[str, Any]) -> None:
    from src.sql.connectors.mysql import test
    await test(cfg)


async def _test_postgresql(cfg: dict[str, Any]) -> None:
    from src.sql.connectors.postgresql import test
    await test(cfg)


async def _test_mssql(cfg: dict[str, Any]) -> None:
    from src.sql.connectors.mssql import test
    await test(cfg)


async def _test_oracle(cfg: dict[str, Any]) -> None:
    from src.sql.connectors.oracle import test
    await test(cfg)


async def _test_connection(cfg: dict[str, Any]) -> None:
    engine = cfg["engine"]
    if engine == "sqlite":
        from src.sql.connectors.sqlite import test as test_sqlite
        try:
            await test_sqlite(cfg)
        except FileNotFoundError as e:
            raise DBSettingsError(str(e)) from None
        return
    try:
        from src.sql.connectors import get_connector
        connector = get_connector(engine)
        await connector.test(cfg)
    except DBSettingsError:
        raise
    except TimeoutError:
        raise DBSettingsError("Connection timed out. Check host and port.") from None
    except (RuntimeError, FileNotFoundError) as e:
        raise DBSettingsError(str(e)) from None
    except Exception as e:  # noqa: BLE001 - driver errors never echo the password
        raise DBSettingsError(f"Could not connect: {type(e).__name__}: {e}") from None


def _apply_runtime(cfg: dict[str, Any]) -> None:
    settings.db_engine = cfg["engine"]
    if cfg["engine"] != "sqlite":
        settings.db_host = cfg["host"]
        settings.db_port = cfg["port"]
        settings.db_name = cfg["database"]
        settings.db_readonly_user = cfg["username"]
        settings.db_readonly_password = cfg["password"]
        if cfg["engine"] == "mssql" and cfg.get("odbc_driver"):
            settings.db_odbc_driver = cfg["odbc_driver"]
    # Everything cached from the previous database is now wrong.
    from src.stages.s12b_sql_retrieval import SQLRetriever
    from src.utils.semantic_cache import SemanticCache
    SQLRetriever.clear_schema_cache()
    SQLRetriever.clear_result_cache()
    SemanticCache.reset()


async def test_only(payload: dict[str, Any]) -> None:
    await _test_connection(_clean(payload))


async def save(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = _clean(payload)
    async with _save_lock:
        await _test_connection(cfg)                   # 1. test BEFORE writing anything
        updates: dict[str, Any] = {"db_engine": cfg["engine"]}
        if cfg["engine"] != "sqlite":                 # SQLite must not wipe the saved server values
            updates.update(
                db_host=cfg["host"], db_port=cfg["port"], db_name=cfg["database"],
                db_readonly_user=cfg["username"], db_readonly_password=cfg["password"],
            )
            if cfg["engine"] == "mssql" and cfg.get("odbc_driver"):
                updates["db_odbc_driver"] = cfg["odbc_driver"]
        try:                                          # 2. persist (atomic; old file kept on failure)
            db_config_file.write(updates)
            from src.sql import registry
            from src.sql.knowledge.loaders import DEFAULT_DB_ID
            registry.save_connection(DEFAULT_DB_ID, updates)
        except (OSError, ValueError) as e:
            reason = getattr(e, "strerror", None) or str(e)
            raise DBSettingsError(f"Could not save the connection settings: {reason}") from None
        _apply_runtime(cfg)                           # 3. live, no restart
    logger.info("Database connection updated: engine=%s host=%s db=%s",
                cfg["engine"], cfg.get("host", "-"), cfg.get("database", "-"))
    return current_config()
