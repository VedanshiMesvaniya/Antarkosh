"""Persisted live-database connection — backward-compatible shim delegating to src.sql.registry.

Why a file: the host that runs Antarkosh (Render, Docker, a VM, a laptop) decides
where environment variables come from, and real env vars silently beat ``.env``.
This file is applied *after* env/.env at startup and wins for the six ``DB_*``
settings below, so the connection an admin saves in the UI is the one the app
uses on any host. If the host's filesystem is ephemeral, point ``DB_CONFIG_FILE``
at a path on a persistent volume.

Stdlib only on purpose: ``src/core/config.py`` imports this at import time.
A missing or corrupt file is never fatal — the app falls back to env/.env.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The only settings this file may set. Anything else in it is ignored, so a
# hand-edited file can never override API keys or other configuration.
CONFIG_KEYS: tuple[str, ...] = (
    "db_engine",
    "db_host",
    "db_port",
    "db_name",
    "db_readonly_user",
    "db_readonly_password",
    "db_odbc_driver",
)


def _get_default_path() -> Path:
    current = Path(__file__).resolve()
    for p in current.parents:
        if (p / "pyproject.toml").is_file():
            return p / "config" / "db_connection.json"
    return current.parent.parent.parent / "config" / "db_connection.json"


_DEFAULT_PATH = _get_default_path()


def config_path() -> Path:
    """``$DB_CONFIG_FILE`` if set, else ``config/db_connection.json``.

    Read from the real process environment on purpose (not from ``.env``): it
    locates the file that overrides ``.env``, so it is a host/deploy-level setting.
    """
    override = os.environ.get("DB_CONFIG_FILE", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_PATH


def _coerce(key: str, value: Any) -> Any:
    """Validate/normalise one value. Raises ValueError/TypeError if unusable."""
    if key == "db_port":
        if isinstance(value, bool):
            raise ValueError("port must be a number")
        port = int(value)
        if not 0 <= port <= 65535:
            raise ValueError("port out of range")
        return port
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value.strip().lower() if key == "db_engine" else value


def read() -> dict[str, Any]:
    """Return the valid ``CONFIG_KEYS`` found in the file ({} if absent/unreadable)."""
    override = os.environ.get("DB_CONFIG_FILE", "").strip()
    if override:
        path = Path(override).expanduser()
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))  # -sig: tolerate a Windows BOM
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable DB config file %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            logger.warning("Ignoring DB config file %s: top level must be a JSON object", path)
            return {}

        out: dict[str, Any] = {}
        for key in CONFIG_KEYS:
            if key in raw:
                try:
                    out[key] = _coerce(key, raw[key])
                except (TypeError, ValueError):
                    logger.warning("Ignoring invalid %r in DB config file %s", key, path)
        return out

    # If no DB_CONFIG_FILE override is set, delegate to registry for erp_main
    from src.sql import registry
    from src.sql.knowledge.loaders import DEFAULT_DB_ID

    conn = registry.get_connection(DEFAULT_DB_ID)
    if not conn:
        return {}

    out = {}
    for canon_k, leg_k in registry.CANONICAL_TO_LEGACY_KEYS.items():
        if canon_k in conn and conn[canon_k] is not None:
            try:
                out[leg_k] = _coerce(leg_k, conn[canon_k])
            except (TypeError, ValueError):
                pass
    return out


def write(values: dict[str, Any]) -> None:
    """Merge ``values`` into the file atomically (temp file + rename).

    Keys not in ``values`` keep their stored value, so switching to SQLite does
    not erase saved server credentials. Raises ValueError for a bad value and
    OSError if the file cannot be written; the old file is left untouched.
    """
    clean = {k: _coerce(k, v) for k, v in values.items() if k in CONFIG_KEYS}

    override = os.environ.get("DB_CONFIG_FILE", "").strip()
    if override:
        path = Path(override).expanduser()
        merged = {**read(), **clean}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".db_connection.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=2)
                fh.write("\n")
            try:
                os.chmod(tmp, 0o600)  # holds a password; best effort (no-op on Windows)
            except OSError:
                pass
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            from src.sql import registry
            from src.sql.knowledge.loaders import DEFAULT_DB_ID

            registry.save_connection(DEFAULT_DB_ID, clean)
        except (OSError, ValueError) as exc:
            logger.debug("Could not mirror connection to registry: %s", exc)
        return

    from src.sql import registry
    from src.sql.knowledge.loaders import DEFAULT_DB_ID

    registry.save_connection(DEFAULT_DB_ID, clean)


def apply_to(target: Any) -> list[str]:
    """Set the stored values on ``target`` (the settings object). Returns the keys applied."""
    applied: list[str] = []
    for key, value in read().items():
        setattr(target, key, value)
        applied.append(key)
    if applied:
        logger.info("DB connection settings loaded from %s: %s", config_path(), ", ".join(applied))
    return applied
