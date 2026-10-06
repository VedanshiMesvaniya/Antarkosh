"""Admin-editable live-database connection: validate -> test -> write .env -> hot-apply."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dotenv import dotenv_values, set_key

from src.core.config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

ENV_PATH = PROJECT_ROOT / ".env"
_save_lock = asyncio.Lock()

# `ready` = the query layer (db_client / sql_dialects) can really run this engine today.
# Flip an engine to True only after its Phase-2 inner-layer work is done.
ENGINES: dict[str, dict[str, Any]] = {
    "mysql":      {"label": "MySQL",                "ready": True,  "default_port": 3306,
                   "fields": ["host", "port", "database", "username", "password"]},
    "sqlite":     {"label": "SQLite",               "ready": True,  "default_port": None,
                   "fields": []},  # fixed file data/live_data.db for now
    "mssql":      {"label": "Microsoft SQL Server", "ready": False, "default_port": 1433,
                   "fields": ["host", "port", "database", "username", "password", "odbc_driver"]},
    "postgresql": {"label": "PostgreSQL",           "ready": False, "default_port": 5432,
                   "fields": ["host", "port", "database", "username", "password"]},
    "oracle":     {"label": "Oracle",               "ready": False, "default_port": 1521,
                   "fields": ["host", "port", "service_name", "username", "password"]},
}
_REQUIRED = {"mysql": ["host", "database", "username"], "sqlite": []}


class DBSettingsError(ValueError):
    """User-facing validation / connection error (never contains the password)."""


def current_config() -> dict[str, Any]:
    """What the UI may see. The password is never returned, only whether one is set."""
    return {
        "engine": settings.db_engine,
        "host": settings.db_host,
        "port": settings.db_port,
        "database": settings.db_name,
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
        return out                       # SQLite: only the engine switches; stored MySQL values stay put

    for f in ("host", "database", "username"):
        out[f] = str(payload.get(f) or "").strip()
    try:
        out["port"] = int(payload.get("port") or spec["default_port"] or 0)
    except (TypeError, ValueError):
        raise DBSettingsError("Port must be a number.") from None

    pw = payload.get("password")         # None = keep the stored password
    if pw is None:
        target_changed = (out["host"], out["port"], out["username"]) != (
            settings.db_host, settings.db_port, settings.db_readonly_user)
        if settings.db_readonly_password and target_changed:
            # Never send the stored password to a different host/user.
            raise DBSettingsError("Re-enter the password when changing host, port or username.")
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


async def _test_connection(cfg: dict[str, Any]) -> None:
    if cfg["engine"] == "sqlite":
        from src.core.db_client import DB_PATH
        if not DB_PATH.exists():
            raise DBSettingsError(f"SQLite file not found at {DB_PATH}.")
        return
    import aiomysql
    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["username"],
                             password=cfg["password"], db=cfg["database"], connect_timeout=5),
            timeout=8,
        )
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
        finally:
            conn.close()
    except asyncio.TimeoutError:
        raise DBSettingsError("Connection timed out. Check host and port.") from None
    except Exception as e:                            # driver errors never echo the password
        raise DBSettingsError(f"Could not connect: {type(e).__name__}: {e}") from None


def _write_env(updates: dict[str, str]) -> None:
    """Edit only our keys, verify they read back identically, else restore the old file."""
    backup = ENV_PATH.read_bytes() if ENV_PATH.exists() else None
    try:
        ENV_PATH.touch(exist_ok=True)
        for k, v in updates.items():
            set_key(str(ENV_PATH), k, v, quote_mode="always")
        written = dotenv_values(ENV_PATH)
        if any(written.get(k) != v for k, v in updates.items()):
            raise DBSettingsError("A value (often a password containing '${') cannot be stored "
                                  "safely in .env. Nothing was saved.")
    except Exception:
        if backup is None:
            ENV_PATH.unlink(missing_ok=True)
        else:
            ENV_PATH.write_bytes(backup)
        raise


def _apply_runtime(cfg: dict[str, Any]) -> None:
    settings.db_engine = cfg["engine"]
    if cfg["engine"] != "sqlite":
        settings.db_host = cfg["host"]
        settings.db_port = cfg["port"]
        settings.db_name = cfg["database"]
        settings.db_readonly_user = cfg["username"]
        settings.db_readonly_password = cfg["password"]
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
        await _test_connection(cfg)                   # 1. test BEFORE touching .env
        updates = {"DB_ENGINE": cfg["engine"]}
        if cfg["engine"] != "sqlite":
            updates.update({
                "DB_HOST": cfg["host"], "DB_PORT": str(cfg["port"]), "DB_NAME": cfg["database"],
                "DB_READONLY_USER": cfg["username"], "DB_READONLY_PASSWORD": cfg["password"],
            })
        try:                                          # 2. persist (verified, rollback on failure)
            _write_env(updates)
        except OSError as e:
            raise DBSettingsError(f"Could not write {ENV_PATH.name}: {e.strerror or e}") from None
        _apply_runtime(cfg)                           # 3. live, no restart
    logger.info("Database connection updated: engine=%s host=%s db=%s",
                cfg["engine"], cfg.get("host", "-"), cfg.get("database", "-"))
    return current_config()
