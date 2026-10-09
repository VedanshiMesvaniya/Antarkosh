"""Simple Session-based Auth API for Alpha Testing."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

try:
    from config.alpha_users import ALPHA_USERS
except ImportError:
    ALPHA_USERS = {
        "mihir": "alpha_pass_1",
        "rahul": "alpha_pass_2",
        "priya": "alpha_pass_3",
        "admin": "alpha_admin_2026",
        "tester": "alpha_test_pass",
    }

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "alpha_session"
SESSION_TTL_SECONDS = 86400 * 7  # 7 days


class SessionStore:
    """Server-side session store: random session id -> username.

    The browser cookie holds only the random id, so it cannot be forged by
    guessing a username. Sessions persist in a JSON file (so a restart does not
    log everyone out) and are re-read when another worker changes the file.
    Only a SHA-256 of each id is written to disk.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._mtime: float | None = None

    def _file(self) -> Path:
        if self._path is not None:
            return self._path
        env = os.environ.get("SESSION_STORE_FILE")
        if env:
            return Path(env)
        from src.core.config import DATA_DIR
        return DATA_DIR / "sessions.json"

    @staticmethod
    def _key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _reload_if_changed(self) -> None:
        path = self._file()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            self._sessions = json.loads(path.read_text(encoding="utf-8"))
            self._mtime = mtime
        except Exception:  # noqa: BLE001 - a corrupt file must not break login
            logger.warning("Session store file unreadable; keeping in-memory sessions")

    def _save(self) -> None:
        path = self._file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="sessions")
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
            self._mtime = path.stat().st_mtime
        except Exception:  # noqa: BLE001 - persistence is best effort
            logger.warning("Could not persist session store", exc_info=True)

    def _purge_expired(self) -> None:
        now = time.time()
        for k in [k for k, v in self._sessions.items() if v.get("expires", 0) <= now]:
            self._sessions.pop(k, None)

    def create(self, user: str) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._reload_if_changed()
            self._purge_expired()
            self._sessions[self._key(session_id)] = {
                "user": user,
                "expires": time.time() + SESSION_TTL_SECONDS,
            }
            self._save()
        return session_id

    def get(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        with self._lock:
            self._reload_if_changed()
            entry = self._sessions.get(self._key(session_id))
            if not entry:
                return None
            if entry.get("expires", 0) <= time.time():
                self._sessions.pop(self._key(session_id), None)
                self._save()
                return None
            return entry.get("user")

    def delete(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._reload_if_changed()
            if self._sessions.pop(self._key(session_id), None) is not None:
                self._save()


session_store = SessionStore()


def _header_auth_enabled() -> bool:
    """X-User-Id is spoofable, so it only works when explicitly enabled (tests / curl)."""
    return os.environ.get("ALLOW_HEADER_AUTH", "").strip().lower() in ("1", "true", "yes")


def _resolve_user(request: Request) -> str | None:
    user = session_store.get(request.cookies.get(COOKIE_NAME))
    if user and user in ALPHA_USERS:
        return user
    if _header_auth_enabled():
        header_user = (request.headers.get("X-User-Id") or "").strip()
        if header_user in ALPHA_USERS:
            return header_user
    return None


class LoginForm(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(form: LoginForm, response: Response) -> dict[str, Any]:
    """Authenticate an alpha tester and start a server-side session."""
    username = form.username.strip()
    expected_pass = ALPHA_USERS.get(username)
    if not expected_pass or not hmac.compare_digest(
        expected_pass.encode("utf-8"), form.password.encode("utf-8")
    ):
        logger.warning("Failed login attempt for user '%s'", form.username)
        raise HTTPException(status_code=401, detail="Invalid Alpha credentials")

    session_id = session_store.create(username)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        httponly=True,
        max_age=SESSION_TTL_SECONDS,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
    )
    logger.info("Alpha user '%s' logged in successfully", username)
    return {"status": "logged_in", "user": username, "user_id": username}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    """End the session on the server and clear the cookie."""
    session_store.delete(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(key=COOKIE_NAME)
    return {"status": "logged_out"}


def get_current_user(request: Request) -> str:
    """Dependency: the logged-in user (session cookie), else 401."""
    user = _resolve_user(request)
    if user:
        return user
    raise HTTPException(status_code=401, detail="Alpha access required. Please login.")


def get_current_user_optional(request: Request) -> str:
    """Optional user dependency — returns username if authenticated, else 'anonymous'."""
    return _resolve_user(request) or "anonymous"


def require_admin(user: str = Depends(get_current_user)) -> str:
    """Dependency for admin-only endpoints (the alpha ``admin`` account)."""
    if user != "admin":
        raise HTTPException(status_code=403, detail="Access denied: Admin access required.")
    return user


@router.get("/me")
async def get_me(user: str = Depends(get_current_user_optional)) -> dict[str, Any]:
    """Get currently logged-in user."""
    authenticated = user != "anonymous"
    return {"user": user, "user_id": user, "authenticated": authenticated}
