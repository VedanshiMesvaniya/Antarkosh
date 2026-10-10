"""Database Client — thin read-only adapter for live data retrieval.

Supports SQLite (default, local file) and MySQL, selected via settings.db_engine.
Query validation/LIMIT-enforcement and the timeout/row-cap wrapper are shared;
only connection setup and row fetching branch by engine, since the aiosqlite
and aiomysql driver APIs differ in shape (context-managed connection vs.
explicit cursor) enough that unifying them would just be boilerplate.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from src.core.config import DATA_DIR, settings
from src.core.sql_dialects import get_dialect_profile

logger = logging.getLogger(__name__)

# Path to the local SQLite database (only used when db_engine == "sqlite")
DB_PATH = DATA_DIR / "live_data.db"

# Safety limits
QUERY_TIMEOUT_SECONDS = settings.db_query_timeout_seconds
MAX_ROWS = 500


from src.sql.knowledge.loaders import DEFAULT_DB_ID


async def run_readonly_query(
    sql: str,
    params: dict | None = None,
    max_rows: int | None = None,
    db_id: str = DEFAULT_DB_ID,
) -> list[dict[str, Any]]:
    """Execute a read-only SQL query with hard timeouts and row caps.

    Args:
        sql: The SQL SELECT statement.
        params: Optional dictionary/tuple of parameters.
        max_rows: Optional limit on the number of returned rows.
        db_id: Target database ID (defaults to DEFAULT_DB_ID = "erp_main").

    Returns:
        List of rows as dictionaries.

    Raises:
        asyncio.TimeoutError: If the query exceeds the timeout limit.
        Exception: If the database throws a SQL error (e.g. syntax, missing table).
    """
    target_db_id = (db_id or DEFAULT_DB_ID).strip()
    from src.sql.registry import get_connection, get_database

    conn_cfg = get_connection(target_db_id)
    engine = conn_cfg.get("engine")
    if not engine:
        try:
            db_meta = get_database(target_db_id)
            engine = db_meta.get("engine")
        except Exception:
            pass
    if not engine:
        engine = settings.db_engine

    profile = get_dialect_profile(engine)
    row_cap = max_rows if max_rows is not None else MAX_ROWS

    if engine == "sqlite":
        import src.core.db_client as db_client_mod

        active_db_path = getattr(db_client_mod, "DB_PATH", DB_PATH)
        db_file = None
        if conn_cfg.get("path"):
            db_file = Path(conn_cfg["path"])
        elif conn_cfg.get("database"):
            db_val = str(conn_cfg["database"])
            if db_val.endswith(".db") or Path(db_val).exists():
                db_file = Path(db_val)
        if db_file is None:
            if target_db_id == DEFAULT_DB_ID:
                db_file = active_db_path
            else:
                db_file = DATA_DIR / "sqlite" / f"{target_db_id}.db"

        if not db_file.exists():
            logger.warning(f"Database file not found at {db_file}")
            return []
        conn_cfg["path"] = str(db_file)

    import sqlglot
    from sqlglot import exp

    try:
        # Enforce hard row cap safely via AST
        tree = sqlglot.parse_one(sql, read=profile.sqlglot_dialect)

        # Check Cartesian explosion risk (comma-joins without JOIN / CROSS JOIN)
        from src.sql.safety.sql_safety import check_cartesian_explosion
        is_cartesian, cart_reason = check_cartesian_explosion(sql, dialect=profile.sqlglot_dialect)
        effective_cap = min(row_cap, 100) if is_cartesian else row_cap
        if is_cartesian:
            logger.warning("Cartesian explosion risk detected (%s). Clamping execution limit to %d.", cart_reason, effective_cap)

        # Only inject LIMIT for SELECT and UNION statements
        if isinstance(tree, (exp.Select, exp.Union)):
            existing = tree.args.get("limit")
            if existing is None:
                tree.set("limit", exp.Limit(expression=exp.Literal.number(effective_cap)))
            else:
                try:
                    # If there's already a limit > effective_cap, clamp it down
                    if int(existing.expression.this) > effective_cap:
                        existing.set("expression", exp.Literal.number(effective_cap))
                except (TypeError, ValueError, AttributeError):
                    pass

            # Serialize back to string without trailing semicolons
            sql = tree.sql(dialect=profile.sqlglot_dialect)
    except Exception as e:
        logger.error(f"Failed to parse SQL for limit enforcement: {e}")
        raise ValueError(f"Invalid SQL: {e}")

    try:
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            try:
                results = await _execute(engine, sql, params, conn_cfg)
            except TypeError:
                results = await _execute(engine, sql, params)

            if len(results) >= row_cap:
                logger.warning(f"Query results capped at {row_cap} rows to protect context window.")

            return results

    except asyncio.TimeoutError:
        logger.error(f"SQL query timed out after {QUERY_TIMEOUT_SECONDS}s: {sql}")
        raise
    except Exception as e:
        logger.error(f"SQL execution failed: {e}")
        raise


async def _execute(
    engine: str,
    sql: str,
    params: dict | None,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Open a read-only connection for the configured engine and run the query.

    Delegates to the engine-specific connector in src.sql.connectors.
    """
    from src.sql.connectors import get_connector

    connector = get_connector(engine, cfg=cfg)
    return await connector.run_readonly(sql, params)
