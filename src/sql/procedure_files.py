"""Developer-supplied stored-procedure scripts (auto-schema Phase 4, the optional "procedures folder" source).

If a developer puts ``.sql`` files of ``CREATE PROCEDURE / FUNCTION`` definitions into
``databases/<db_id>/procedures/``, they are read at every Sync Schema together with the procedures
read from the database itself. No folder, or an empty one, means the database is the only source.
This module only READS and CHECKS those files. It never runs them: nothing here connects to a
database or executes SQL.

Rules: text only (UTF-8 / UTF-16 / Windows-1252), size and count limits, symlinks are ignored, a file
must define at least one routine, and a file that looks like a data export is skipped so row data is
never used. A skipped file is reported with its reason, never silently dropped.

Origin: replaces src/sql/procedure_upload.py (the HTTP upload was removed)
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.sa.routines import Routine

FOLDER_NAME = "procedures"
MAX_FILES = 200
MAX_FILE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 20_000_000
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
        raise ValueError("not a text file")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def routine_names(text: str) -> list[str]:
    """Names of the routines a script defines (comments ignored). Detection only; the text is never run."""
    stripped = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))
    return [m.group(2).strip('[]"`') for m in _ROUTINE_RE.finditer(stripped)]


def script_problem(text: str) -> str | None:
    """Why this script cannot be used, or None."""
    if not text.strip():
        return "the file is empty"
    if _COPY_STDIN.search(text) or len(_INSERT_LINE.findall(text)) > DATA_DUMP_INSERT_LINES:
        return "looks like a data export; only CREATE PROCEDURE / FUNCTION definitions are used"
    if not routine_names(text):
        return "no CREATE PROCEDURE, FUNCTION or PACKAGE BODY definition found"
    return None


def procedures_folder(db_id: str, databases_dir: Path | None = None) -> Path:
    validate_db_id(db_id)
    base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
    return base / db_id / FOLDER_NAME


def ensure_procedures_folder(db_id: str, databases_dir: Path | None = None) -> bool:
    """Create ``databases/<db_id>/procedures/`` if the database folder exists, so developers can see where to drop files.

    Never for erp_main, and never creates the database folder itself. Returns True if it was created now.
    """
    if db_id == DEFAULT_DB_ID:
        return False
    folder = procedures_folder(db_id, databases_dir)
    if folder.exists() or not folder.parent.is_dir():
        return False
    try:
        folder.mkdir()
    except OSError:
        return False
    return True


def load_folder_routines(db_id: str, databases_dir: Path | None = None) -> tuple[list[Routine], list[dict[str, str]]]:
    """Read the ``.sql`` files in the database's procedures folder. Returns (scripts as Routines, skipped files with reasons)."""
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files
        return [], []
    folder = procedures_folder(db_id, databases_dir)
    if not folder.is_dir():
        return [], []
    root = folder.resolve()
    routines: list[Routine] = []
    skipped: list[dict[str, str]] = []
    total = 0
    files = sorted(p for p in folder.rglob("*.sql"))
    for index, path in enumerate(files):
        rel = path.relative_to(folder).as_posix()
        if index >= MAX_FILES:
            skipped.append({"file": rel, "reason": f"more than {MAX_FILES} files; the rest are ignored"})
            continue
        try:
            if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
                skipped.append({"file": rel, "reason": "not a regular file inside the folder"})
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                skipped.append({"file": rel, "reason": "larger than 5 MB"})
                continue
            if total + size > MAX_TOTAL_BYTES:
                skipped.append({"file": rel, "reason": "total size limit reached"})
                continue
            text = decode_script(path.read_bytes())
        except (OSError, ValueError) as exc:
            skipped.append({"file": rel, "reason": str(exc) or "unreadable"})
            continue
        problem = script_problem(text)
        if problem:
            skipped.append({"file": rel, "reason": problem})
            continue
        total += size
        routines.append(Routine("", rel, "SCRIPT", "folder", text, None, False, tuple(routine_names(text))))
    return routines, skipped


__all__ = [
    "FOLDER_NAME",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "decode_script",
    "ensure_procedures_folder",
    "load_folder_routines",
    "procedures_folder",
    "routine_names",
    "script_problem",
]
