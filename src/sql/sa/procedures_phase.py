"""The stored-procedure phase of a schema sync (auto-schema Phase 4).

Reads the database's stored procedures and functions (source text only) through the database's OWN
saved connection, decides Full or Basic mode, and writes ``schema/procedures.json``: names, kinds,
hashes and counts, never the code text. The definitions themselves are returned in memory for the
mining phase (Phase 5) to use during the same sync.

Safety: only the static catalog SELECTs in ``routines.ROUTINE_SQL`` run. No routine is executed and
no data row is read. Every failure becomes a report, so this can never fail a sync.

Origin: new (no previous file)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.sa.engine_factory import create_sa_engine, resolve_connection
from src.sql.sa.routines import MAX_ROUTINES, PERMISSION_HINT, ROUTINE_SQL, Routine, normalize_rows

logger = logging.getLogger(__name__)

GENERATED_BY = "auto_procedures"
MANIFEST_REL = "schema/procedures.json"
PROC_TIMEOUT_SECONDS = 120


def build_report(engine: str, routines: list[Routine], *, limited: bool = False, warnings: list[str] | None = None) -> dict[str, Any]:
    """Counts and the Full / Basic decision. Contains no code text and no routine names."""
    db = [r for r in routines if r.source == "database"]
    scripts = [r for r in routines if r.source == "upload" and r.definition]
    readable = [r for r in db if r.definition]
    unreadable: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for r in db:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        if r.unreadable:
            unreadable[r.unreadable] = unreadable.get(r.unreadable, 0) + 1
    notes = list(warnings or [])
    if limited:
        notes.append(f"only the first {MAX_ROUTINES} routines were read")
    hint = PERMISSION_HINT.get(engine, "")
    if db and not readable:
        notes.append(f"{len(db)} routines exist but none of their text could be read" + (f"; {hint}" if hint else ""))
    elif readable and unreadable.get("hidden"):
        notes.append(f"{unreadable['hidden']} of {len(db)} routines are hidden from this account" + (f"; {hint}" if hint else ""))
    elif not db and hint and not warnings:
        # An account that cannot see routines looks exactly like a database that has none.
        notes.append("no stored procedures were visible to this account; if the database has some, " + hint)
    return {
        "ok": True,
        "engine": engine,
        "mode": "full" if (readable or scripts) else "basic",
        "found": len(db),
        "readable": len(readable),
        "unreadable": dict(sorted(unreadable.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "uploaded_scripts": len(scripts),
        "uploaded_routines": sum(len(r.members) for r in scripts),
        "warnings": notes,
    }


def manifest_document(db_id: str, engine: str, routines: list[Routine], report: dict[str, Any]) -> dict[str, Any]:
    """The file written to disk: identifying facts only, never the definitions."""
    entries = [
        {
            "schema": r.schema,
            "name": r.name,
            "kind": r.kind,
            "source": r.source,
            "sha256": r.sha256,
            "chars": len(r.definition or ""),
            "truncated": r.truncated,
            "unreadable": r.unreadable,
            **({"members": list(r.members)} if r.members else {}),
        }
        for r in sorted(routines, key=lambda x: (x.source, x.schema, x.name, x.kind))
    ]
    return {
        "generated_by": GENERATED_BY,
        "version": 1,
        "db_id": db_id,
        "engine": engine,
        "mode": report["mode"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": {k: report[k] for k in ("found", "readable", "unreadable", "by_kind", "uploaded_scripts", "uploaded_routines")},
        "routines": entries,
    }


def write_manifest(db_id: str, doc: dict[str, Any], databases_dir: Path | None = None) -> dict[str, Any]:
    """Write the manifest into an EXISTING database folder. Never for erp_main; never over a foreign file."""
    validate_db_id(db_id)
    result: dict[str, Any] = {"written": [], "skipped": {}}
    if db_id == DEFAULT_DB_ID:
        result["skipped"]["all"] = "pinned database: curated files are never replaced"
        return result
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        result["skipped"]["all"] = "database folder does not exist"
        return result
    path = folder / MANIFEST_REL
    if path.exists():
        import json

        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if not (isinstance(current, dict) and current.get("generated_by") == GENERATED_BY):
            result["skipped"][MANIFEST_REL] = "a file not generated by auto_procedures is present; left untouched"
            return result
    path.parent.mkdir(parents=True, exist_ok=True)
    registry.atomic_write_json(path, doc, mode=0o644)
    result["written"].append(MANIFEST_REL)
    return result


async def run_procedures_phase(
    *,
    db_id: str,
    databases_dir: Path | None = None,
    timeout: float = PROC_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], list[Routine]]:
    """Read, classify and record ``db_id``'s stored routines. Returns (report, routines in memory)."""
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:  # pinned: erp_main keeps its curated files and is never read by this phase
        return {"ok": True, "mode": "basic", "found": 0, "readable": 0, "skipped": "pinned database"}, []
    engine_key, _cfg = resolve_connection(db_id)
    sql = ROUTINE_SQL.get(engine_key)
    routines: list[Routine] = []
    warnings: list[str] = []
    limited = False

    if sql is None:
        warnings.append(f"{engine_key} has no stored procedures")
    else:
        engine = create_sa_engine(db_id)
        try:
            async with engine.connect() as conn:
                result = await asyncio.wait_for(conn.execute(text(sql)), timeout=timeout)
                rows = [dict(r._mapping) for r in result]
            routines, limited = normalize_rows(engine_key, rows)
        except Exception as exc:  # noqa: BLE001 - a failed read is a Basic-mode report, never a failed sync
            warnings.append(f"routine query failed: {type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("Procedure read failed for %s: %s", db_id, exc)
        finally:
            await engine.dispose()

    # Scripts an admin uploaded for this database (the other source). Both sources feed Phase 5.
    from src.sql.procedure_upload import load_uploaded_routines

    routines = routines + load_uploaded_routines(db_id, databases_dir)

    report = build_report(engine_key, routines, limited=limited, warnings=warnings)
    report["files"] = write_manifest(db_id, manifest_document(db_id, engine_key, routines, report), databases_dir)
    return report, routines


__all__ = ["GENERATED_BY", "MANIFEST_REL", "build_report", "manifest_document", "run_procedures_phase", "write_manifest"]
