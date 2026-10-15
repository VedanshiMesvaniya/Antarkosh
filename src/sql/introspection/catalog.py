"""Catalog extraction and table inventory (auto-schema Phase 3).

Turns what the database's own catalog says (columns, keys, comments, views, row estimates)
into two files for a database folder:

* ``schema/schema.json``          same shape as the curated ``erp_main`` file
* ``schema/table_inventory.json`` every table and view found, hiding nothing

Only catalog queries are run (the profile's ``table_query``, ``key_query``,
``row_estimate_query`` and, for SQLite, ``column_query``) on top of the rows a schema sync
already fetched. No data rows are read and nothing is executed on the database.
Every extra query is optional: if an engine does not offer it or the account may not run it,
that part is reported as unavailable and the rest still works.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.sql import registry
from src.sql.dialects.base import SQLDialectProfile
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)

GENERATED_BY = "antarkosh-auto-catalog"
SCHEMA_REL = "schema/schema.json"
INVENTORY_REL = "schema/table_inventory.json"
_PINNED_DB_IDS = frozenset({DEFAULT_DB_ID})

QueryRunner = Callable[..., Awaitable[list[dict[str, Any]]]]


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class CatalogTable:
    name: str
    kind: str = "table"  # "table" | "view"
    comment: str | None = None
    row_estimate: int | None = None
    columns: list[dict[str, Any]] = field(default_factory=list)  # {"name", "type", "comment"}
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)


@dataclass
class Catalog:
    db_id: str
    engine: str
    tables: dict[str, CatalogTable] = field(default_factory=dict)
    foreign_keys: list[dict[str, str]] = field(default_factory=list)  # one entry per key COLUMN pair
    warnings: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # optional parts that were not obtained


def _lower_keys(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Engines differ in the case of result column names (MySQL 8 upper-cases, Oracle too)."""
    return [{str(k).lower(): v for k, v in row.items()} for row in rows or []]


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    """Row estimates: engines report unknown as NULL or a negative number (PostgreSQL -1)."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _ordinal(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class _NameResolver:
    """Match a table name from one catalog query to the spelling another query used."""

    def __init__(self, names: Any) -> None:
        self._exact = set(names)
        self._lower: dict[str, list[str]] = {}
        for n in self._exact:
            self._lower.setdefault(n.lower(), []).append(n)

    def add(self, name: str) -> None:
        self._exact.add(name)
        self._lower.setdefault(name.lower(), []).append(name)

    def resolve(self, name: Any) -> str | None:
        if name is None:
            return None
        name = str(name)
        if name in self._exact:
            return name
        candidates = self._lower.get(name.lower(), [])
        return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# Building (pure: rows in, Catalog out)
# ---------------------------------------------------------------------------

def build_catalog(
    db_id: str,
    dialect: SQLDialectProfile,
    *,
    column_rows: list[dict[str, Any]],
    fk_rows: list[dict[str, Any]] | None = None,
    table_rows: list[dict[str, Any]] | None = None,
    key_rows: list[dict[str, Any]] | None = None,
    estimate_rows: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    unavailable: list[str] | None = None,
) -> Catalog:
    cat = Catalog(db_id=db_id, engine=dialect.key, warnings=list(warnings or []), unavailable=list(unavailable or []))
    column_rows, fk_rows = _lower_keys(column_rows), _lower_keys(fk_rows)

    # 1. The table list is the source of truth for kind and comment when we have it.
    if table_rows is not None:
        for row in _lower_keys(table_rows):
            name = _clean_text(row.get("table_name"))
            if not name:
                continue
            kind = "view" if "VIEW" in str(row.get("table_type") or "").upper() else "table"
            comment = _clean_text(row.get("table_comment"))
            if kind == "view" and comment and comment.upper() == "VIEW":
                comment = None  # MySQL stores the literal word VIEW as a view's "comment"
            cat.tables[name] = CatalogTable(name=name, kind=kind, comment=comment)
    resolver = _NameResolver(cat.tables)

    # 2. Columns.
    unlisted: set[str] = set()
    for row in column_rows:
        tname = _clean_text(row.get("table_name") or row.get("name"))
        cname = _clean_text(row.get("column_name"))
        if not tname or not cname:
            continue  # e.g. SQLite CREATE-statement rows carry no column detail
        actual = resolver.resolve(tname)
        if actual is None:
            actual = tname
            cat.tables[actual] = CatalogTable(name=actual)
            resolver.add(actual)
            if table_rows is not None:
                unlisted.add(actual)
        cat.tables[actual].columns.append(
            {"name": cname, "type": _clean_text(row.get("data_type")) or "", "comment": _clean_text(row.get("column_comment"))}
        )
    if unlisted:
        cat.warnings.append(f"{len(unlisted)} table(s) have columns but were missing from the table list: {sorted(unlisted)[:5]}")

    # 3. Primary and unique keys, keeping composite column order.
    grouped: dict[tuple[str, str, str], list[tuple[int, str]]] = {}
    for row in _lower_keys(key_rows):
        actual = resolver.resolve(row.get("table_name"))
        column = _clean_text(row.get("column_name"))
        ctype = str(row.get("constraint_type") or "").upper()
        if actual is None or not column or ctype not in ("PRIMARY KEY", "UNIQUE"):
            continue
        grouped.setdefault((actual, ctype, str(row.get("constraint_name") or "")), []).append(
            (_ordinal(row.get("ordinal_position")), column)
        )
    for (tname, ctype, _cname), cols in sorted(grouped.items()):
        ordered = [c for _, c in sorted(cols, key=lambda x: x[0])]
        if ctype == "PRIMARY KEY":
            cat.tables[tname].primary_key = ordered
        else:
            cat.tables[tname].unique_keys.append(ordered)

    # 4. Declared foreign keys (one entry per key column pair, as the engines report them).
    for row in fk_rows:
        from_t = resolver.resolve(row.get("table_name")) or _clean_text(row.get("table_name"))
        from_c = _clean_text(row.get("column_name"))
        to_t = resolver.resolve(row.get("referenced_table_name")) or _clean_text(row.get("referenced_table_name"))
        to_c = _clean_text(row.get("referenced_column_name"))
        if from_t and from_c and to_t and to_c:
            cat.foreign_keys.append({"from_table": from_t, "from_column": from_c, "to_table": to_t, "to_column": to_c})

    # 5. Row estimates (the engine's statistics, never a count over the data).
    for row in _lower_keys(estimate_rows):
        actual = resolver.resolve(row.get("table_name"))
        if actual is not None:
            cat.tables[actual].row_estimate = _to_int(row.get("row_estimate"))
    return cat


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def schema_document(cat: Catalog) -> dict[str, Any]:
    """Same top-level shape as the curated erp_main schema.json (extra keys are additive)."""
    fk_cols: dict[str, set[str]] = {}
    for fk in cat.foreign_keys:
        fk_cols.setdefault(fk["from_table"], set()).add(fk["from_column"])
    tables = []
    for name in sorted(cat.tables):
        t = cat.tables[name]
        tables.append(
            {
                "name": t.name,
                "domain": None,
                "is_view": t.kind == "view",
                "comment": t.comment,
                "primary_key": list(t.primary_key),
                "unique_keys": [list(u) for u in t.unique_keys],
                "columns": [
                    {
                        "name": c["name"],
                        "type": c["type"],
                        "nullable": None,  # not read by the catalog queries
                        "auto_increment": None,
                        "comment": c["comment"],
                        "is_primary_key": c["name"] in t.primary_key,
                        "is_foreign_key": c["name"] in fk_cols.get(t.name, set()),
                    }
                    for c in t.columns
                ],
            }
        )
    relationships = [{**fk, "type": "fk"} for fk in cat.foreign_keys]
    return {
        "generated_by": GENERATED_BY,
        "database": cat.db_id,
        "engine": cat.engine,
        "generated_at": _now(),
        "generated_note": "Generated from the live database catalog (catalog queries only; no data rows were read).",
        "table_count": len(tables),
        "relationship_count": len(relationships),
        "domains": {},
        "tables": tables,
        "relationships": relationships,
    }


def inventory_document(cat: Catalog) -> dict[str, Any]:
    fk_count: dict[str, int] = {}
    for fk in cat.foreign_keys:
        fk_count[fk["from_table"]] = fk_count.get(fk["from_table"], 0) + 1
    entries = [
        {
            "name": t.name,
            "kind": t.kind,
            "column_count": len(t.columns),
            "primary_key": list(t.primary_key),
            "foreign_key_count": fk_count.get(t.name, 0),
            "row_estimate": t.row_estimate,
            "comment": t.comment,
        }
        for t in (cat.tables[n] for n in sorted(cat.tables))
    ]
    views = sum(1 for e in entries if e["kind"] == "view")
    return {
        "generated_by": GENERATED_BY,
        "db_id": cat.db_id,
        "engine": cat.engine,
        "generated_at": _now(),
        "counts": {"tables": len(entries) - views, "views": views, "total": len(entries)},
        "unavailable": sorted(set(cat.unavailable)),
        "tables": entries,
    }


def check_consistency(
    cat: Catalog,
    schema_doc: dict[str, Any],
    inventory_doc: dict[str, Any],
    chunk_names: set[str] | None = None,
) -> dict[str, Any]:
    """The Phase 3 gate: catalog, inventory, schema.json and the embedded schema chunks list the same tables."""
    sets: dict[str, set[str]] = {
        "catalog": set(cat.tables),
        "inventory": {t["name"] for t in inventory_doc.get("tables", [])},
        "schema.json": {t["name"] for t in schema_doc.get("tables", [])},
    }
    if chunk_names is not None:
        sets["schema chunks"] = set(chunk_names)

    problems: list[str] = []
    base = sets["catalog"]
    for label, names in sets.items():
        if label == "catalog":
            continue
        missing, extra = sorted(base - names), sorted(names - base)
        if missing:
            problems.append(f"in catalog but not in {label}: {missing[:10]}{' ...' if len(missing) > 10 else ''}")
        if extra:
            problems.append(f"in {label} but not in catalog: {extra[:10]}{' ...' if len(extra) > 10 else ''}")
    counts = inventory_doc.get("counts", {})
    if counts.get("total") != len(inventory_doc.get("tables", [])):
        problems.append("inventory counts.total does not match its table list")
    return {"ok": not problems, "problems": problems, "counts": {label: len(names) for label, names in sets.items()}}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_catalog_files(
    db_id: str,
    schema_doc: dict[str, Any],
    inventory_doc: dict[str, Any],
    databases_dir: Path | None = None,
) -> dict[str, Any]:
    """Write the two files into an EXISTING ``databases/<db_id>/schema/`` folder, atomically.

    Never writes for a pinned database. Never replaces a hand-curated ``schema.json``: it is
    replaced only if it is missing, still the empty template, or was generated by this module.
    """
    validate_db_id(db_id)
    result: dict[str, Any] = {"written": [], "skipped": {}}
    if db_id in _PINNED_DB_IDS:
        result["skipped"]["all"] = "pinned database: curated files are never replaced"
        return result
    base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
    folder = base / db_id
    if not folder.is_dir():
        result["skipped"]["all"] = "database folder does not exist (turn on auto_scaffold or create it)"
        return result

    schema_path, inventory_path = folder / SCHEMA_REL, folder / INVENTORY_REL

    existing = _read_json(schema_path) if schema_path.exists() else None
    if not schema_path.exists() or (
        isinstance(existing, dict) and (existing.get("generated_by") == GENERATED_BY or not existing.get("tables"))
    ):
        registry.atomic_write_json(schema_path, schema_doc, mode=0o644)
        result["written"].append(SCHEMA_REL)
    else:
        result["skipped"][SCHEMA_REL] = "a curated or unreadable schema.json is present; left untouched"

    existing_inv = _read_json(inventory_path) if inventory_path.exists() else None
    if not inventory_path.exists() or (isinstance(existing_inv, dict) and existing_inv.get("generated_by") == GENERATED_BY):
        registry.atomic_write_json(inventory_path, inventory_doc, mode=0o644)
        result["written"].append(INVENTORY_REL)
    else:
        result["skipped"][INVENTORY_REL] = "a file not generated by auto-catalog is present; left untouched"
    return result


# ---------------------------------------------------------------------------
# Orchestration for a schema sync
# ---------------------------------------------------------------------------

async def _optional(
    runner: QueryRunner, sql: str, db_id: str, label: str, warnings: list[str], unavailable: list[str]
) -> list[dict[str, Any]] | None:
    if not sql:
        unavailable.append(label)  # this engine does not offer it
        return None
    try:
        return _lower_keys(await runner(sql, max_rows=20000, db_id=db_id))
    except Exception as exc:  # noqa: BLE001 - an optional catalog query must never fail the sync
        unavailable.append(label)
        warnings.append(f"{label} query failed: {type(exc).__name__}: {str(exc)[:160]}")
        return None


async def run_catalog_phase(
    *,
    db_id: str,
    dialect: SQLDialectProfile,
    runner: QueryRunner,
    schema_rows: list[dict[str, Any]],
    fk_rows: list[dict[str, Any]],
    chunk_names: set[str] | None = None,
    databases_dir: Path | None = None,
) -> dict[str, Any]:
    """Extract, write and check the catalog for ``db_id``; returns a report dict."""
    warnings: list[str] = []
    unavailable: list[str] = []

    column_rows = schema_rows
    if dialect.column_query:
        got = await _optional(runner, dialect.column_query, db_id, "columns", warnings, unavailable)
        if got is not None:
            column_rows = got
    table_rows = await _optional(runner, dialect.table_query, db_id, "tables", warnings, unavailable)
    key_rows = await _optional(runner, dialect.key_query, db_id, "keys", warnings, unavailable)
    estimate_rows = await _optional(runner, dialect.row_estimate_query, db_id, "row_estimates", warnings, unavailable)

    cat = build_catalog(
        db_id,
        dialect,
        column_rows=column_rows,
        fk_rows=fk_rows,
        table_rows=table_rows,
        key_rows=key_rows,
        estimate_rows=estimate_rows,
        warnings=warnings,
        unavailable=unavailable,
    )
    schema_doc, inventory_doc = schema_document(cat), inventory_document(cat)
    check = check_consistency(cat, schema_doc, inventory_doc, chunk_names)
    files = write_catalog_files(db_id, schema_doc, inventory_doc, databases_dir=databases_dir)
    counts = inventory_doc["counts"]
    return {
        "ok": check["ok"],
        "tables": counts["tables"],
        "views": counts["views"],
        "foreign_keys": len(cat.foreign_keys),
        "problems": check["problems"],
        "warnings": cat.warnings,
        "unavailable": sorted(set(cat.unavailable)),
        "files": files,
    }
