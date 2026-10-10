"""Database registry and connection management for Antarkosh multi-database architecture."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATABASES_DIR = PROJECT_ROOT / "databases"
from src.sql.engine import Engine
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)

# Canonical keys allowed for a database connection entry
KNOWN_CONNECTION_KEYS: tuple[str, ...] = (
    "host",
    "port",
    "database",
    "path",
    "username",
    "password",
    "odbc_driver",
    "service_name",
    "engine",
)

# Mapping between legacy db_config_file keys and canonical connection keys
LEGACY_TO_CANONICAL_KEYS: dict[str, str] = {
    "db_host": "host",
    "db_port": "port",
    "db_name": "database",
    "db_readonly_user": "username",
    "db_readonly_password": "password",
    "db_odbc_driver": "odbc_driver",
    "db_engine": "engine",
}

CANONICAL_TO_LEGACY_KEYS: dict[str, str] = {
    v: k for k, v in LEGACY_TO_CANONICAL_KEYS.items()
}


def connections_path() -> Path:
    """Return Path to connections.json ($CONNECTIONS_FILE or $DB_CONNECTIONS_FILE if set, else config/connections.json)."""
    override = os.environ.get("CONNECTIONS_FILE", "").strip() or os.environ.get("DB_CONNECTIONS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return CONFIG_DIR / "connections.json"


def atomic_write_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    """Atomically write JSON data to path with restrictive permissions (0o600).

    Uses tempfile.mkstemp in the same directory and atomic rename (os.replace).
    Cleans up temp file on failure and preserves existing file.
    Never logs values or sensitive information.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        try:
            os.chmod(tmp, mode)  # Best effort (POSIX 0o600; no-op on Windows)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _coerce_field(key: str, value: Any) -> Any:
    """Validate and coerce a single connection field. Raises ValueError/TypeError on invalid input."""
    if key == "port":
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("port must be a number")
        try:
            port = int(value)
        except (ValueError, TypeError):
            raise ValueError(f"port must be an integer, got {value!r}")
        if not 0 <= port <= 65535:
            raise ValueError("port out of range (0-65535)")
        return port
    if key == "engine":
        if not value:
            return None
        engine = Engine.from_value(str(value))
        return engine.value
    if not isinstance(value, str):
        if value is None:
            return ""
        raise ValueError(f"{key} must be a string")
    return value


def _normalize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate incoming connection fields (supporting legacy and canonical keys)."""
    normalized: dict[str, Any] = {}
    for raw_k, v in fields.items():
        canonical_k = LEGACY_TO_CANONICAL_KEYS.get(raw_k, raw_k)
        if canonical_k in KNOWN_CONNECTION_KEYS:
            coerced_v = _coerce_field(canonical_k, v)
            if coerced_v is not None:
                normalized[canonical_k] = coerced_v
    return normalized


def _import_legacy_erp_connection(target_path: Path | None = None) -> dict[str, Any]:
    """Build the fallback connection dict for erp_main from db_connection.json or settings.

    Never logs passwords or values.
    """
    from src.core.config import settings

    legacy_candidates = [
        CONFIG_DIR / "db_connection.json",
    ]
    db_cfg_override = os.environ.get("DB_CONFIG_FILE", "").strip()
    if db_cfg_override:
        legacy_candidates.insert(0, Path(db_cfg_override).expanduser())

    raw_legacy: dict[str, Any] = {}
    legacy_file_used: Path | None = None
    for cand in legacy_candidates:
        if cand.is_file():
            try:
                parsed = json.loads(cand.read_text(encoding="utf-8-sig"))
                if isinstance(parsed, dict) and parsed:
                    raw_legacy = parsed
                    legacy_file_used = cand
                    break
            except (OSError, ValueError) as exc:
                logger.warning("Ignoring unreadable legacy DB config file %s: %s", cand, exc)

    entry: dict[str, Any] = {
        "engine": (
            raw_legacy.get("db_engine")
            or raw_legacy.get("engine")
            or getattr(settings, "db_engine", "sqlite")
        ),
        "host": (
            raw_legacy.get("db_host")
            or raw_legacy.get("host")
            or getattr(settings, "db_host", "")
        ),
        "port": (
            raw_legacy.get("db_port")
            if "db_port" in raw_legacy
            else raw_legacy.get("port", getattr(settings, "db_port", 3306))
        ),
        "database": (
            raw_legacy.get("db_name")
            or raw_legacy.get("database")
            or getattr(settings, "db_name", "")
        ),
        "username": (
            raw_legacy.get("db_readonly_user")
            or raw_legacy.get("username")
            or getattr(settings, "db_readonly_user", "")
        ),
        "password": (
            raw_legacy.get("db_readonly_password")
            if "db_readonly_password" in raw_legacy
            else raw_legacy.get("password", getattr(settings, "db_readonly_password", ""))
        ),
        "odbc_driver": (
            raw_legacy.get("db_odbc_driver")
            or raw_legacy.get("odbc_driver")
            or getattr(settings, "db_odbc_driver", "")
        ),
    }

    cleaned = _normalize_fields(entry)
    if legacy_file_used:
        logger.info(
            "Importing existing legacy DB connection from %s for %s",
            legacy_file_used.name,
            DEFAULT_DB_ID,
        )
    else:
        logger.info("Importing DB connection from settings for %s", DEFAULT_DB_ID)
    return cleaned


def _load_connections(conn_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load connections from connections.json, importing erp_main on first use if absent."""
    path = conn_path or connections_path()
    if not path.exists():
        erp_entry = _import_legacy_erp_connection(path)
        initial_data = {DEFAULT_DB_ID: erp_entry}
        try:
            atomic_write_json(path, initial_data)
        except OSError as exc:
            logger.warning("Could not persist initial connections.json to %s: %s", path, exc)
        return initial_data

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable connections file %s: %s", path, exc)
        return {}

    if not isinstance(raw, dict):
        logger.warning("Ignoring connections file %s: top level must be a JSON object", path)
        return {}

    connections: dict[str, dict[str, Any]] = {}
    for db_id, fields in raw.items():
        if isinstance(fields, dict):
            connections[db_id] = _normalize_fields(fields)
    return connections


def get_connection(
    db_id: str,
    conn_path: Path | None = None,
    raise_if_missing: bool = False,
) -> dict[str, Any]:
    """Retrieve connection credentials/config dict for db_id.

    Returns a copy of the connection dictionary (or empty dict if not found).
    Never logs values or credentials.
    """
    validate_db_id(db_id)
    connections = _load_connections(conn_path=conn_path)
    conn = connections.get(db_id)
    if conn is None:
        if raise_if_missing:
            raise KeyError(f"No connection configuration found for {db_id!r}")
        return {}
    return conn.copy()


def save_connection(
    db_id: str,
    fields: dict[str, Any],
    conn_path: Path | None = None,
) -> None:
    """Save/update connection fields for db_id into config/connections.json atomically.

    Merges with existing connection fields so unspecified keys keep their stored values.
    Never logs values or credentials.
    """
    validate_db_id(db_id)
    clean_updates = _normalize_fields(fields)

    connections = _load_connections(conn_path=conn_path)
    existing = connections.get(db_id, {})
    merged_entry = {**existing, **clean_updates}
    connections[db_id] = merged_entry

    path = conn_path or connections_path()
    atomic_write_json(path, connections)
    logger.info(
        "Saved connection for db_id %s (keys: %s)",
        db_id,
        ", ".join(sorted(clean_updates.keys())),
    )


def get_database(
    db_id: str,
    databases_dir: Path | None = None,
) -> dict[str, Any]:
    """Load databases/<db_id>/db.yaml and validate its engine with Engine.

    Raises FileNotFoundError if db.yaml does not exist.
    Raises ValueError if YAML is invalid or engine is unsupported.
    """
    validate_db_id(db_id)
    base_dir = databases_dir or DATABASES_DIR
    yaml_path = base_dir / db_id / "db.yaml"
    if not yaml_path.is_file():
        raise FileNotFoundError(f"Database configuration not found for {db_id!r} at {yaml_path}")

    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ValueError(f"Failed to parse database configuration {yaml_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise TypeError(f"Database configuration {yaml_path} must be a mapping")

    raw_engine = data.get("engine")
    if not raw_engine:
        raise ValueError(f"Missing required 'engine' field in {yaml_path}")

    engine = Engine.from_value(str(raw_engine))
    data["engine"] = engine.value
    return data


def atomic_write_yaml(path: Path, data: dict[str, Any], mode: int = 0o644) -> None:
    """Atomically write YAML data to path.

    Uses tempfile.mkstemp in the same directory and atomic rename (os.replace).
    Cleans up temp file on failure and preserves existing file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def can_access_database(
    db_id: str,
    user: str | None,
    databases_dir: Path | None = None,
) -> bool:
    """Check if user has permission to access the specified database.

    Admins ('admin') and wildcard callers ('*', 'all') are always allowed.
    Regular users must be listed in databases/<db_id>/db.yaml['allowed_users'].
    """
    if not user:
        return False
    if user in ("admin", "*", "all"):
        return True

    try:
        db_meta = get_database(db_id, databases_dir=databases_dir)
    except (FileNotFoundError, KeyError, ValueError, TypeError, OSError):
        return False

    allowed = db_meta.get("allowed_users") or []
    if isinstance(allowed, list):
        return user in allowed or "*" in allowed
    return False


def get_database_access(
    db_id: str,
    databases_dir: Path | None = None,
) -> list[str]:
    """Retrieve the list of allowed users for a database."""
    validate_db_id(db_id)
    db_meta = get_database(db_id, databases_dir=databases_dir)
    allowed = db_meta.get("allowed_users")
    if isinstance(allowed, list):
        return list(allowed)
    return ["admin"]


def update_database_access(
    db_id: str,
    allowed_users: list[str],
    databases_dir: Path | None = None,
) -> bool:
    """Update allowed_users in databases/<db_id>/db.yaml atomically.

    Guarantees 'admin' is always included in allowed_users.
    """
    validate_db_id(db_id)
    base_dir = databases_dir or DATABASES_DIR
    yaml_path = base_dir / db_id / "db.yaml"
    if not yaml_path.is_file():
        return False

    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ValueError(f"Failed to read database configuration {yaml_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise TypeError(f"Database configuration {yaml_path} must be a mapping")

    clean_users = sorted({str(u).strip() for u in allowed_users if str(u).strip()})
    if "admin" not in clean_users:
        clean_users.insert(0, "admin")

    data["allowed_users"] = clean_users
    atomic_write_yaml(yaml_path, data)

    # Invalidate cached DatabaseContext for db_id
    try:
        from src.sql.context import clear_context_cache

        clear_context_cache(db_id)
    except (ImportError, OSError) as exc:
        logger.debug("Failed to clear context cache for %s: %s", db_id, exc)

    return True


def list_databases(
    databases_dir: Path | None = None,
    user: str | None = None,
) -> list[dict[str, Any]]:
    """List all configured databases from DATABASES_DIR, validated with Engine.

    If user is provided and not admin/wildcard, unauthorized databases are hidden.
    """
    base_dir = databases_dir or DATABASES_DIR
    if not base_dir.is_dir():
        return []

    databases: list[dict[str, Any]] = []
    for item in sorted(base_dir.iterdir()):
        if not item.is_dir() or item.name.startswith("_"):
            continue
        yaml_path = item / "db.yaml"
        if not yaml_path.is_file():
            continue
        try:
            db_data = get_database(item.name, databases_dir=base_dir)
            if user and user not in ("admin", "*", "all"):
                allowed = db_data.get("allowed_users") or []
                if not (user in allowed or "*" in allowed):
                    continue
            databases.append(db_data)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Skipping invalid database config in %s: %s", item.name, exc)
    return databases


__all__ = [
    "CANONICAL_TO_LEGACY_KEYS",
    "KNOWN_CONNECTION_KEYS",
    "LEGACY_TO_CANONICAL_KEYS",
    "atomic_write_json",
    "atomic_write_yaml",
    "can_access_database",
    "connections_path",
    "get_connection",
    "get_database",
    "get_database_access",
    "list_databases",
    "save_connection",
    "update_database_access",
]

