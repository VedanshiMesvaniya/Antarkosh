"""Each user's current database: which database their questions are answered from.

Stored per user (never shared), so two people using the app at the same time, even from different
clients, cannot change each other's database. A user with no entry keeps using the default database,
exactly as before. The file is written atomically and guarded by a file lock, so several workers can
update it safely.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

import portalocker

from src.core.config import DATA_DIR

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()


def _path() -> Path:
    env = os.environ.get("ACTIVE_DB_FILE")
    return Path(env) if env else DATA_DIR / "active_databases.json"


def _read(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable active-database file %s: %s", path, exc)
        return {}
    return {str(u): str(d) for u, d in raw.items() if isinstance(raw, dict) and isinstance(d, str)} if isinstance(raw, dict) else {}


def get_active_db(user: str | None) -> str | None:
    """The user's chosen database id, or None (= the default database)."""
    if not user:
        return None
    return _read(_path()).get(user)


def set_active_db(user: str, db_id: str | None) -> None:
    """Choose ``db_id`` for ``user``; None clears it (back to the default database)."""
    if not user:
        raise ValueError("user is required")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, portalocker.Lock(str(path) + ".lock", timeout=10):
        data = _read(path)
        if db_id:
            data[user] = db_id
        else:
            data.pop(user, None)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".active_db_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def clear_database_for_all(db_id: str) -> int:
    """Forget ``db_id`` for every user (for when a database is removed). Returns how many were cleared."""
    path = _path()
    if not path.exists():
        return 0
    with _LOCK, portalocker.Lock(str(path) + ".lock", timeout=10):
        data = _read(path)
        kept = {u: d for u, d in data.items() if d != db_id}
        if len(kept) != len(data):
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".active_db_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(kept, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        return len(data) - len(kept)


__all__ = ["clear_database_for_all", "get_active_db", "set_active_db"]
