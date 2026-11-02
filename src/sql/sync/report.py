"""The sync report and the record kept in ``db.yaml`` (auto-schema Phase 11).

``sync_live_schema`` already returned one report per phase. This puts them side by side: which mode ran (``full`` when the
database's stored procedures were readable, ``basic`` otherwise), what each phase did, the counts that matter, and the
unknowns - everything the sync could not work out - so a person can see at a glance what a sync did and what it left open.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import validate_db_id
from src.sql.sync.validate import Validation

logger = logging.getLogger(__name__)

MAX_UNKNOWNS = 20
ROUTE_FLOOR = 0.45  # the bridge planner's floor (route_index.MIN_ROUTE_SCORE); informational here

PHASES: tuple[tuple[str, str], ...] = (
    ("catalog", "catalog"),
    ("procedures", "procedures"),
    ("sp_evidence", "mining"),
    ("relationships", "relationships"),
    ("table_profiles", "profiles"),
    ("join_paths", "join_paths"),
    ("column_meaning", "meaning"),
)


def schema_fingerprint(table_schemas: dict[str, str]) -> str:
    """Changes when a table, column or type changes. Built from the per-table DDL the sync already holds."""
    raw = json.dumps(sorted((str(k).lower(), str(v)) for k, v in table_schemas.items()), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def phase_status(report: dict[str, Any] | None) -> tuple[str, str]:
    """``(status, detail)``: off | ok | skipped | failed."""
    if report is None:
        return "off", ""
    if report.get("ok") is False:
        return "failed", str(report.get("error") or report.get("skipped") or "")[:160]
    if report.get("skipped"):
        return "skipped", str(report["skipped"])[:160]
    return "ok", ""


def _num(report: dict[str, Any] | None, *keys: str) -> int | None:
    for key in keys:
        value = (report or {}).get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, (dict, list)):
            return len(value)
    return None


def _orphan_tables(db_id: str, databases_dir: Path | None) -> tuple[int, list[str]] | None:
    """Tables that appear in no applied relationship (cheap: two small files). None when it cannot be told."""
    base = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    try:
        schema = json.loads((base / "schema/schema.json").read_text(encoding="utf-8"))
        rels = json.loads((base / "schema/relationships.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    names = {str(t.get("name", "")).lower() for t in schema.get("tables", []) if isinstance(t, dict) and t.get("name")}
    entries = rels.get("relationships") if isinstance(rels, dict) else rels
    linked: set[str] = set()
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict):
            linked.update(str(e.get(k, "")).lower() for k in ("from_table", "to_table"))
    orphans = sorted(names - linked)
    return len(orphans), orphans[:5]


def _route_floor(db_id: str, databases_dir: Path | None) -> dict[str, Any] | None:
    """How many stored multi-join routes clear the planner's floor (uses the Phase 10 tool when it is present)."""
    try:
        from src.sql.paths.benchmark import live_floor_table
    except ImportError:
        return None
    base = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    try:
        doc = json.loads((base / "schema/join_paths.json").read_text(encoding="utf-8"))
        table = live_floor_table(doc, (0.30, ROUTE_FLOOR))
    except (OSError, ValueError):
        return None
    total = sum(r["routes"] for r in table["rows"])
    kept = sum(r["kept"][f"{ROUTE_FLOOR:.2f}"] for r in table["rows"])
    return {"routes": total, "at_or_above_floor": kept, "floor": ROUTE_FLOOR} if total else None


def build_sync_report(
    result: dict[str, Any],
    *,
    db_id: str,
    duration_s: float,
    validation: Validation | None,
    guard: dict[str, Any] | None = None,
    sync_info: dict[str, Any] | None = None,
    databases_dir: Path | None = None,
) -> dict[str, Any]:
    """Combine the per-phase reports already in ``result`` into one summary. Never raises."""
    guard = guard or {}
    sync_info = sync_info or {}
    phases: dict[str, dict[str, str]] = {}
    for name, key in PHASES:
        status, detail = phase_status(result.get(key))
        phases[name] = {"status": status, **({"detail": detail} if detail else {})}

    procedures = result.get("procedures") or {}
    mode = "full" if procedures.get("ok") and procedures.get("mode") == "full" else "basic"

    catalog, rel, prof = result.get("catalog"), result.get("relationships"), result.get("table_profiles")
    counts = {
        "tables": result.get("tables_synced"),
        "views": _num(catalog, "views"),
        "declared_foreign_keys": _num(catalog, "foreign_keys"),
        "relationships_applied": _num(rel, "applied_edges"),
        "relationships_inferred": _num(rel, "edges"),
        "profiled_tables": _num(prof, "tables"),
        "join_path_pairs": _num(result.get("join_paths"), "pairs", "pairs_stored", "pairs_total"),
        "procedures_readable": _num(procedures, "readable"),
        "reembedded_for_relationships": sync_info.get("reembedded_for_relationships"),
        "reembedded_for_meanings": _num(result.get("column_meaning"), "reembedded_tables"),
    }
    counts = {k: v for k, v in counts.items() if v is not None}

    unknowns: list[str] = []
    for name, info in phases.items():
        detail = info.get("detail", "")
        if info["status"] in ("failed", "skipped") and "pinned" not in detail:
            unknowns.append(f"{name}: {info['status']}" + (f" ({detail})" if detail else ""))
    if catalog:
        for schema, why in (catalog.get("skipped_schemas") or {}).items():
            unknowns.append(f"schema {schema!r} was not read: {why}")
        if catalog.get("problems"):
            unknowns.append(f"catalog: {len(catalog['problems'])} consistency problem(s)")
    if (procedures.get("unreadable") or 0) > 0:
        unknowns.append(f"procedures: {procedures['unreadable']} could not be read (encrypted or no permission)")
    if rel:
        held = rel.get("held_back")
        for label, value in (("direction unknown", rel.get("direction_unknown")), ("ambiguous", rel.get("ambiguous")), ("held back", sum(held.values()) if isinstance(held, dict) else held)):
            if value:
                unknowns.append(f"relationships: {value} edge(s) {label}")
    if prof and prof.get("undecided"):
        unknowns.append(f"table profiles: {prof['undecided']} group(s) of similar tables with no clear primary")
    orphans = _orphan_tables(db_id, databases_dir)
    if orphans and orphans[0]:
        sample = ", ".join(orphans[1])
        unknowns.append(f"{orphans[0]} table(s) appear in no applied relationship (for example {sample})")
    floor = _route_floor(db_id, databases_dir)
    if floor and floor["at_or_above_floor"] < floor["routes"]:
        unknowns.append(f"join paths: {floor['routes'] - floor['at_or_above_floor']} of {floor['routes']} multi-join route(s) score below {floor['floor']:.2f}, so the bridge planner skips them")
    if validation is not None:
        if validation.errors:
            unknowns.append(f"validation: {len(validation.errors)} error(s)")
        if validation.warnings:
            unknowns.append(f"validation: {len(validation.warnings)} warning(s)")
    if len(unknowns) > MAX_UNKNOWNS:
        unknowns = unknowns[:MAX_UNKNOWNS] + [f"... and {len(unknowns) - MAX_UNKNOWNS} more"]

    rolled_back = bool(guard.get("rolled_back"))
    failed = any(p["status"] == "failed" for p in phases.values())
    status = "rolled_back" if rolled_back else "ok_with_warnings" if (failed or (validation and validation.warnings)) else "ok"
    report: dict[str, Any] = {
        "status": status,
        "mode": mode,
        "duration_s": round(duration_s, 1),
        "counts": counts,
        "phases": phases,
        "unknowns": unknowns,
        "guard": {k: v for k, v in guard.items() if v not in (None, [], {}, False)},
    }
    if floor:
        report["route_floor"] = floor
    if validation is not None:
        report["validation"] = validation.as_dict()
    if sync_info.get("schema_hash"):
        report["schema_hash"] = sync_info["schema_hash"]
    return report


def record_sync_report(db_id: str, report: dict[str, Any], *, committed: bool, schema_hash: str | None) -> bool:
    """Write a compact summary under ``schema_sync`` in ``databases/<db_id>/db.yaml``. Never raises.

    ``schema_hash`` is stored only for a committed sync: after a rollback the knowledge files describe the OLD schema, so the
    stored hash must stay old and the next sync must not believe nothing changed.
    """
    try:
        import yaml

        validate_db_id(db_id)
        path = registry.DATABASES_DIR / db_id / "db.yaml"
        if not path.is_file():
            return False
        with open(path, encoding="utf-8") as fh:
            meta = yaml.safe_load(fh) or {}
        if not isinstance(meta, dict):
            return False
        sync_meta = meta.get("schema_sync")
        sync_meta = sync_meta if isinstance(sync_meta, dict) else {}
        if committed and schema_hash:
            sync_meta["schema_hash"] = schema_hash
        sync_meta["mode"] = report.get("mode")
        sync_meta["last_sync"] = {
            "status": report.get("status"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "duration_s": report.get("duration_s"),
            "tables": (report.get("counts") or {}).get("tables"),
            "phases": {name: info.get("status") for name, info in (report.get("phases") or {}).items()},
            "unknowns": len(report.get("unknowns") or []),
            "validation_errors": len((report.get("validation") or {}).get("errors") or []),
            "rolled_back": bool((report.get("guard") or {}).get("rolled_back")),
        }
        meta["schema_sync"] = sync_meta
        registry.atomic_write_yaml(path, meta)
        return True
    except Exception as exc:  # noqa: BLE001 - a report must never fail a sync
        logger.warning("Could not record the sync report for %s: %s", db_id, exc)
        return False


__all__ = ["PHASES", "build_sync_report", "phase_status", "record_sync_report", "schema_fingerprint"]
