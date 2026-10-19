"""Client-supplied stored-procedure scripts (auto-schema Phase 4, the "uploaded .sql" source).

A client who cannot give us catalog access can upload a script of ``CREATE PROCEDURE / FUNCTION``
definitions (for example SSMS "Script Stored Procedures as CREATE"). This module only DECODES,
CHECKS and STORES that text. It never runs it: nothing here connects to a database or executes SQL.

Rules: text only (UTF-8 / UTF-16 / Windows-1252), size limits, must contain at least one routine
definition, and a file that looks like a data export is refused so row data is never stored.

Origin: new (no previous file)
"""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.sa.routines import Routine

UPLOAD_REL = "schema/procedures_upload"
MAX_FILE_BYTES = 5_000_000
MAX_REQUEST_FILES = 20
MAX_REQUEST_BYTES = 20_000_000
MAX_STORED_FILES = 50
DATA_DUMP_INSERT_LINES = 200  # more than this many column-0 INSERT INTO lines = a data export, not routines

_ROUTINE_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:DEFINER\s*=\s*\S+\s+)?(?:(?:NON)?EDITIONABLE\s+)?"
    r"(PROCEDURE|PROC|FUNCTION|PACKAGE\s+BODY)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\[\]\w\"`.$#@]+)",
    re.IGNORECASE,
)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_INSERT_LINE = re.compile(r"^INSERT\s+INTO\b", re.IGNORECASE | re.MULTILINE)
_COPY_STDIN = re.compile(r"^COPY\s+\S+.*\bFROM\s+stdin\b", re.IGNORECASE | re.MULTILINE)


def decode_script(raw: bytes) -> str:
    """Bytes to text. UTF-16 and UTF-8 BOMs are honoured, then UTF-8, then Windows-1252. Raises ValueError for binary."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(codecs.BOM_UTF8):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    if "\x00" in text:
        raise ValueError("This is not a text file.")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def routine_names(text: str) -> list[str]:
    """Names of the routines a script defines (comments ignored). Detection only; the text is never parsed or run."""
    stripped = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))
    return [m.group(2).strip('[]"`') for m in _ROUTINE_RE.finditer(stripped)]


def script_problem(text: str) -> str | None:
    """Why this script cannot be accepted, or None."""
    if not text.strip():
        return "The file is empty."
    if _COPY_STDIN.search(text) or len(_INSERT_LINE.findall(text)) > DATA_DUMP_INSERT_LINES:
        return "This looks like a data export. Upload only the CREATE PROCEDURE / FUNCTION definitions."
    if not routine_names(text):
        return "No CREATE PROCEDURE, FUNCTION or PACKAGE BODY definition was found."
    return None


def _folder(db_id: str, databases_dir: Path | None) -> Path:
    validate_db_id(db_id)
    base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
    return base / db_id / UPLOAD_REL


def stored_files(db_id: str, databases_dir: Path | None = None) -> list[Path]:
    folder = _folder(db_id, databases_dir)
    return sorted(folder.glob("*.sql")) if folder.is_dir() else []


def store_script(db_id: str, text: str, databases_dir: Path | None = None) -> dict[str, Any]:
    """Store one accepted script as ``<hash>.sql`` (so the same text is never stored twice). Never for erp_main."""
    if db_id == DEFAULT_DB_ID:
        raise ValueError("The main database keeps its curated files; uploads are not used.")
    folder = _folder(db_id, databases_dir)
    if not folder.parent.parent.is_dir():  # databases/<db_id> must already exist
        raise FileNotFoundError("Database folder does not exist.")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    target = folder / f"{digest}.sql"
    if not target.exists():
        if len(stored_files(db_id, databases_dir)) >= MAX_STORED_FILES:
            raise ValueError(f"This database already has {MAX_STORED_FILES} uploaded scripts.")
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".up_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return {"file": target.name, "sha256": digest, "routines": routine_names(text), "chars": len(text)}


def clear_scripts(db_id: str, databases_dir: Path | None = None) -> int:
    """Delete every uploaded script of ``db_id``. Returns how many were removed."""
    files = stored_files(db_id, databases_dir)
    for path in files:
        path.unlink()
    return len(files)


def load_uploaded_routines(db_id: str, databases_dir: Path | None = None) -> list[Routine]:
    """The stored scripts as Routines (kind SCRIPT). ``members`` lists the routines each one defines."""
    routines: list[Routine] = []
    for path in stored_files(db_id, databases_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        routines.append(Routine("", path.name, "SCRIPT", "upload", text, None, False, tuple(routine_names(text))))
    return routines


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_REQUEST_FILES",
    "MAX_STORED_FILES",
    "clear_scripts",
    "decode_script",
    "load_uploaded_routines",
    "routine_names",
    "script_problem",
    "store_script",
    "stored_files",
]
