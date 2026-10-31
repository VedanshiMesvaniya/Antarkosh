"""Table-authority hints for the SQL prompt (auto-schema Phase 12, part 2).

Phase 7 writes ``databases/<db_id>/schema/table_profiles.json``: for look-alike tables (``product`` / ``products`` /
``product_bak``) it says which one holds the real data. Nothing read that file, so when retrieval picked the copy because
its name matched the question better, the model never saw the real table and never knew a better one existed.

Two additive effects, both off unless a database is switched to *apply* mode:

* **Retrieval** - when a chosen table is a copy (``secondary``) of a table with a clearly decided primary, offer the primary
  to the schema budget as well (capped). The copy is never removed.
* **Prompt** - a short ``Table notes`` block, only for pairs where BOTH tables are in the prompt, saying the copy duplicates
  the primary and that the primary is the one to use unless the question names the copy.

Shadow mode (the default for every database that has the Phase 12 flags) computes the same thing and only logs it.

Safety rules, each tested:

* ``erp_main`` is never read (its curated files are pinned).
* A missing, unreadable, differently shaped or hand-written file gives ``None``; callers then behave exactly as before.
* Only decided groups count: a primary must be named by Phase 7 with ``high`` or ``medium`` confidence. ``undecided``,
  ``equal`` (series such as ``sales_2023`` / ``sales_2024``) and ``standalone`` tables are never touched.
* A table name that exists in more than one schema is ambiguous and is skipped.
* Pure reading: no database access, no writes, no clock.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)

PROFILES_REL = "schema/table_profiles.json"
GENERATED_BY = "auto_table_profiles"

MAX_PRIMARY_ADDS = 3  # most tables one question may gain from this hint
MAX_NOTES = 6  # most note lines in one prompt
TRUSTED_CONFIDENCE = frozenset({"high", "medium"})


@dataclass(frozen=True)
class AuthorityPlan:
    add: tuple[str, ...]  # primaries to offer to the budget, in order
    pairs: tuple[tuple[str, str], ...]  # (copy, primary) for every chosen copy whose primary is known
    skipped: tuple[tuple[str, int], ...] = ()  # why tables were not used: (reason, count), for telemetry only

    def note_lines(self, present: Iterable[str]) -> list[str]:
        """One line per pair whose two tables are BOTH in ``present`` (a table cut by the budget gets no dangling note)."""
        have = {t.lower() for t in present}
        out: list[str] = []
        for copy, primary in self.pairs:
            if copy in have and primary in have:
                out.append(
                    f"- `{copy}` looks like a copy of `{primary}`. Use `{primary}` for the real data, "
                    f"unless the question is explicitly about `{copy}`."
                )
        return out[:MAX_NOTES]


class AuthorityIndex:
    """Copy -> primary, for the decided groups of one database."""

    def __init__(self, primary_of: dict[str, str]) -> None:
        self._primary_of = primary_of

    def __len__(self) -> int:
        return len(self._primary_of)

    def primary_for(self, table: str) -> str | None:
        return self._primary_of.get(table.lower())


# --------------------------------------------------------------------------- loading (cached until the file changes)

_CACHE: dict[str, tuple[tuple[int, int] | None, AuthorityIndex | None]] = {}
_LOCK = threading.Lock()


def clear_authority_cache(db_id: str | None = None) -> None:
    with _LOCK:
        if db_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(db_id, None)


def _intern(value: Any) -> str:
    return sys.intern(str(value).strip().lower())


def _build_index(doc: Any) -> AuthorityIndex | None:
    if not isinstance(doc, dict) or doc.get("generated_by") != GENERATED_BY:
        return None  # missing marker: a hand-written file is curated and is not ours to interpret
    tables = doc.get("tables")
    if not isinstance(tables, dict):
        return None

    # Short names that appear in more than one schema are ambiguous: never guess which one a prompt means.
    seen: dict[str, int] = {}
    entries: dict[str, dict[str, Any]] = {}
    for qualified, entry in tables.items():
        if not isinstance(entry, dict):
            continue
        short = _intern(entry.get("name") or str(qualified).rsplit(".", 1)[-1])
        seen[short] = seen.get(short, 0) + 1
        entries[str(qualified).lower()] = {**entry, "_short": short}
    short_of = {q: e["_short"] for q, e in entries.items()}

    primary_of: dict[str, str] = {}
    for qualified, entry in entries.items():
        if entry.get("authority") != "secondary" or entry.get("confidence") not in TRUSTED_CONFIDENCE:
            continue
        preferred = entry.get("preferred")
        if not isinstance(preferred, str):
            continue
        primary = short_of.get(preferred.lower())
        copy = entry["_short"]
        if primary is None or primary == copy or seen.get(copy, 0) != 1 or seen.get(primary, 0) != 1:
            continue
        # The primary itself must be a decided primary (guards against a stale or hand-edited entry).
        target = entries.get(preferred.lower(), {})
        if target.get("authority") != "primary":
            continue
        primary_of[copy] = primary
    return AuthorityIndex(primary_of) if primary_of else None


def load_authority_index(db_id: str) -> AuthorityIndex | None:
    """The authority index for ``db_id``, or None when there is no usable ``table_profiles.json`` (never raises)."""
    try:
        validate_db_id(db_id)
        if db_id == DEFAULT_DB_ID:
            return None  # pinned database: curated files only
        path: Path = registry.DATABASES_DIR / db_id / PROFILES_REL
        try:
            stat = path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        with _LOCK:
            cached = _CACHE.get(db_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        index: AuthorityIndex | None = None
        if stamp is not None:
            index = _build_index(json.loads(path.read_text(encoding="utf-8")))
        with _LOCK:
            _CACHE[db_id] = (stamp, index)
        return index
    except Exception as exc:  # noqa: BLE001 - an unreadable file must never break a question
        logger.warning("Table authority index for %s unavailable: %s: %s", db_id, type(exc).__name__, str(exc)[:200])
        return None


# --------------------------------------------------------------------------- mode


def hints_mode(db_id: str) -> str:
    """``"apply"``, ``"shadow"`` or ``""`` (off) for this database.

    Needs the Phase 12 prompt flags (``auto_prompt_shadow`` or ``auto_prompt_inject``). Even with ``auto_prompt_inject`` on,
    the hints only *apply* for databases listed in ``PROMPT_HINTS_APPLY_DBS`` (or ``*``); every other flagged database
    runs them in shadow mode, so turning on route bridges never switches these on by itself.
    """
    try:
        from src.core.config import settings
        from src.sql.schema_retrieval import _route_bridge_mode

        base = _route_bridge_mode(db_id)
        if not base:
            return ""
        listed = {x.strip().lower() for x in str(settings.prompt_hints_apply_dbs or "").split(",") if x.strip()}
        if base == "apply" and ("*" in listed or db_id.lower() in listed):
            return "apply"
        return "shadow"
    except Exception as exc:  # noqa: BLE001 - on any doubt the hints stay off
        logger.debug("Table authority mode unreadable for %s: %s", db_id, exc)
        return ""


# --------------------------------------------------------------------------- planning


def plan_authority(
    chosen: Sequence[str],
    db_id: str,
    *,
    available: Iterable[str] | None = None,
    max_add: int = MAX_PRIMARY_ADDS,
    index: AuthorityIndex | None = None,
) -> AuthorityPlan | None:
    """Primaries to offer for the copies retrieval chose.

    ``chosen`` are the tables retrieval picked, most relevant first. ``available`` is the set of tables that really exist in
    the live schema: a primary that is not there is skipped. Returns None when there is no usable profile file (the caller
    then does nothing); otherwise a plan whose ``add`` may be empty.
    """
    idx = index if index is not None else load_authority_index(db_id)
    if idx is None:
        return None

    ordered: list[str] = []
    for table in chosen:
        name = str(table).lower()
        if name and name not in ordered:
            ordered.append(name)
    present = set(ordered)
    exist = {t.lower() for t in available} if available is not None else None

    add: list[str] = []
    pairs: list[tuple[str, str]] = []
    skipped: dict[str, int] = {"missing_table": 0, "over_cap": 0}
    for table in ordered:
        primary = idx.primary_for(table)
        if primary is None:
            continue
        pairs.append((table, primary))
        if primary in present or primary in add:
            continue
        if exist is not None and primary not in exist:
            skipped["missing_table"] += 1
            continue
        if len(add) >= max_add:
            skipped["over_cap"] += 1
            continue
        add.append(primary)
    return AuthorityPlan(tuple(add), tuple(pairs), tuple((k, v) for k, v in skipped.items() if v))


def note_lines_for_prompt(shown_tables: Iterable[str], db_id: str) -> tuple[str, list[str]]:
    """``(mode, lines)`` for the prompt's ``Table notes`` block. Lines are only returned in apply mode (never raises)."""
    try:
        mode = hints_mode(db_id)
        if not mode:
            return "", []
        shown = sorted({str(t).lower() for t in shown_tables})
        plan = plan_authority(shown, db_id, available=None)
        if plan is None:
            return mode, []
        return mode, plan.note_lines(shown)
    except Exception as exc:  # noqa: BLE001 - a hint problem must never break a prompt
        logger.debug("Table notes unavailable for %s: %s", db_id, exc)
        return "", []


__all__ = [
    "MAX_NOTES",
    "MAX_PRIMARY_ADDS",
    "AuthorityIndex",
    "AuthorityPlan",
    "clear_authority_cache",
    "hints_mode",
    "load_authority_index",
    "note_lines_for_prompt",
    "plan_authority",
]
