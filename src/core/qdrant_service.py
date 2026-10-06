"""Cross-platform Qdrant service manager.

Ensures the project's local Qdrant vector database is running on startup,
handling Windows, Linux, macOS, and container environments.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx

from src.core.config import DATA_DIR, PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

_qdrant_process: Optional[subprocess.Popen] = None
_qdrant_log_file = None


def find_qdrant_binary() -> Optional[Path]:
    """Search for the Qdrant executable within the project or system PATH."""
    is_win = platform.system() == "Windows"
    binary_names = ["qdrant.exe"] if is_win else ["qdrant"]

    # 1. Check project ./qdrant directory
    qdrant_dir = PROJECT_ROOT / "qdrant"
    if qdrant_dir.is_dir():
        for name in binary_names:
            candidate = qdrant_dir / name
            if candidate.is_file():
                _ensure_executable(candidate)
                return candidate

    # 2. Check project root directory
    for name in binary_names:
        candidate = PROJECT_ROOT / name
        if candidate.is_file():
            _ensure_executable(candidate)
            return candidate

    # 3. Check system PATH
    for name in binary_names:
        found = shutil.which(name)
        if found:
            return Path(found)

    return None


def _ensure_executable(path: Path) -> None:
    """Ensure file has execute permissions on POSIX systems."""
    if platform.system() != "Windows":
        try:
            current_mode = os.stat(path).st_mode
            os.chmod(path, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception as e:
            logger.debug("Failed to set execute permissions on %s: %s", path, e)


async def is_qdrant_ready(url: str, timeout: float = 1.5) -> bool:
    """Check if Qdrant HTTP service is responding and ready."""
    target = url.rstrip("/")
    probe_url = f"{target}/readyz" if "/readyz" not in target else target
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(probe_url)
            if resp.status_code in (200, 204):
                return True
    except Exception:
        # Fallback probe to root url
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(target)
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
    return False


async def ensure_qdrant_running(timeout: float = 12.0) -> bool:
    """Ensure Qdrant is running, launching the project-bundled binary if needed.

    Returns True if Qdrant is active and healthy, False otherwise.
    """
    global _qdrant_process, _qdrant_log_file

    url = settings.qdrant_url.strip() if settings.qdrant_url else "http://localhost:6333"

    # 1. If already reachable, nothing to do
    if await is_qdrant_ready(url):
        logger.info("Qdrant service is already running at %s", url)
        if not settings.qdrant_url:
            settings.qdrant_url = "http://localhost:6333"
        return True

    # 2. Find local binary
    binary_path = find_qdrant_binary()
    if not binary_path:
        logger.warning(
            "Local Qdrant binary not found in %s/qdrant and Qdrant is not running at %s.",
            PROJECT_ROOT,
            url,
        )
        return False

    # 3. Determine working directory (qdrant/ directory holds storage/ and snapshots/)
    working_dir = (
        binary_path.parent
        if binary_path.parent.name == "qdrant"
        else (PROJECT_ROOT / "qdrant" if (PROJECT_ROOT / "qdrant").is_dir() else PROJECT_ROOT)
    )

    # 4. Prepare log file in data/
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "qdrant.log"
    try:
        _qdrant_log_file = open(log_path, "a", encoding="utf-8")
    except Exception:
        _qdrant_log_file = subprocess.DEVNULL

    logger.info("Starting local Qdrant from %s (cwd=%s)...", binary_path, working_dir)

    popen_kwargs = {
        "cwd": str(working_dir),
        "stdout": _qdrant_log_file,
        "stderr": subprocess.STDOUT,
    }

    if platform.system() == "Windows":
        # Run without creating a visible console window
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        popen_kwargs["creationflags"] = create_no_window

    try:
        _qdrant_process = subprocess.Popen([str(binary_path)], **popen_kwargs)
        atexit.register(stop_qdrant)
    except Exception as e:
        logger.error("Failed to spawn Qdrant process: %s", e)
        return False

    # 5. Wait for Qdrant to be ready
    start_time = asyncio.get_running_loop().time()
    while (asyncio.get_running_loop().time() - start_time) < timeout:
        # Check if process crashed immediately
        if _qdrant_process.poll() is not None:
            logger.error("Qdrant process exited prematurely with code %s", _qdrant_process.returncode)
            return False

        if await is_qdrant_ready("http://localhost:6333", timeout=1.0):
            logger.info("Qdrant successfully started and ready on http://localhost:6333")
            if not settings.qdrant_url:
                settings.qdrant_url = "http://localhost:6333"
            return True

        await asyncio.sleep(0.4)

    logger.warning("Qdrant process started but did not respond within %s seconds", timeout)
    return False


def stop_qdrant() -> None:
    """Gracefully terminate the Qdrant background process if spawned by this service."""
    global _qdrant_process, _qdrant_log_file
    if _qdrant_process is not None:
        try:
            if _qdrant_process.poll() is None:
                logger.info("Stopping Qdrant process (PID=%s)...", _qdrant_process.pid)
                _qdrant_process.terminate()
                try:
                    _qdrant_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _qdrant_process.kill()
        except Exception as e:
            logger.debug("Error while stopping Qdrant: %s", e)
        finally:
            _qdrant_process = None

    if _qdrant_log_file and hasattr(_qdrant_log_file, "close"):
        try:
            _qdrant_log_file.close()
        except Exception:
            pass
        _qdrant_log_file = None
