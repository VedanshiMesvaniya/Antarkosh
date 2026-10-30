"""Check the generated files a sync leaves behind (auto-schema Phase 11).

Two kinds of finding, and only one of them can roll a sync back:

* **errors** - a file this sync wrote is unreadable, or names a table or column the catalog does not have. The sync made it,
  so the sync undoes it (``SyncGuard.rollback``).
* **warnings** - a file that was NOT rewritten this sync is stale (built from an older catalog), the glossary mentions a column
  that is gone, a phase did not run. These are reported, never rolled back: rolling back cannot fix them, and a catalog that
  was rolled back on every sync could never move forward.

Reuses the existing ``drift_validator`` checks, pointed at this database's files instead of the default database's.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import validate_db_id
from src.sql.sync.guard import GENERATED_FILES, is_generated

logger = logging.getLogger(__name__)

MAX_LISTED = 10  # findings listed per file; the rest are counted

CATALOG_REL = "schema/schema.json"
RELATIONSHIP_FILES = ("schema/relationships.json", "schema/inferred_relationships.json")
PATHS_REL = "schema/join_paths.json"
HASHED_FILES = ("schema/inferred_relationships.json", "schema/table_profiles.json", PATHS_REL)  # carry the catalog's inputs_hash
GLOSSARY_REL = "semantics/column_glossary.json"


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # generated files built from an older catalog than the one on disk
    checked: list[str] = field(default_factory=list)
    coherent: bool | None = None  # every hashed file matches the current catalog; None when it could not be checked

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "stale": self.stale, "checked": self.checked, "coherent": self.coherent}


def _cap(findings: list[str], label: str) -> list[str]:
    if len(findings) <= MAX_LISTED:
        return [f"{label}: {f}" for f in findings]
    return [f"{label}: {f}" for f in findings[:MAX_LISTED]] + [f"{label}: ... and {len(findings) - MAX_LISTED} more"]


def _unqualified(name: str) -> str:
    return str(name).rsplit(".", 1)[-1].lower()


def validate_generation(db_id: str, *, changed: set[str] | None = None, databases_dir: Path | None = None) -> Validation:
    """Validate ``databases/<db_id>``. ``changed`` = files this sync wrote (None: treat every generated file as written now)."""
    validate_db_id(db_id)
    root = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    out = Validation()
    docs: dict[str, dict[str, Any]] = {}

    # 1. every generated file we wrote is readable JSON
    for rel in GENERATED_FILES:
        path = root / rel
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            out.warnings.append(f"{rel}: cannot be read ({exc.__class__.__name__})")
            continue
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            if changed is None or rel in changed:
                out.errors.append(f"{rel}: written by this sync but is not valid JSON ({exc.__class__.__name__})")
            else:
                out.warnings.append(f"{rel}: not valid JSON (left over from an earlier sync)")
            continue
        if is_generated(rel, raw):
            docs[rel] = doc
            out.checked.append(rel)

    # 2. cross-checks need the catalog
    catalog = root / CATALOG_REL
    tables: dict[str, set[str]] = {}
    if catalog.is_file() and CATALOG_REL in docs:
        try:
            from src.sql.safety.drift_validator import load_schema_tables_and_columns

            tables = load_schema_tables_and_columns(catalog)
        except Exception as exc:  # noqa: BLE001
            out.warnings.append(f"catalog could not be read for cross-checks ({exc.__class__.__name__})")
    if tables:
        from src.sql.safety.drift_validator import validate_relationships_drift

        for rel in RELATIONSHIP_FILES:
            if rel not in docs:
                continue
            findings = validate_relationships_drift(tables, root / rel)
            target = out.errors if (changed is None or rel in changed) else out.warnings
            target.extend(_cap(findings, rel))
        if PATHS_REL in docs:
            unknown = sorted({_unqualified(t) for entry in docs[PATHS_REL].get("pairs") or [] if isinstance(entry, dict)
                              for route in entry.get("routes") or [] if isinstance(route, dict)
                              for t in route.get("tables") or [] if _unqualified(t) not in tables})
            if unknown:
                target = out.errors if (changed is None or PATHS_REL in changed) else out.warnings
                target.extend(_cap([f"route names table {t!r} that is not in the catalog" for t in unknown], PATHS_REL))
        glossary = root / GLOSSARY_REL
        if glossary.is_file():
            try:
                from src.sql.safety.drift_validator import validate_glossary_drift

                out.warnings.extend(_cap(validate_glossary_drift(tables, glossary), GLOSSARY_REL))
            except Exception as exc:  # noqa: BLE001
                out.warnings.append(f"{GLOSSARY_REL}: could not be checked ({exc.__class__.__name__})")

    # 3. do the files come from the same catalog? (a stale file is a warning: the next sync rebuilds it)
    try:
        from src.sql.relationships.model import load_graph

        graph = load_graph(db_id, databases_dir=databases_dir) if CATALOG_REL in docs else None
    except Exception as exc:  # noqa: BLE001
        graph = None
        out.warnings.append(f"catalog hash could not be computed ({exc.__class__.__name__})")
    if graph is not None:
        for rel in HASHED_FILES:
            if rel in docs and docs[rel].get("inputs_hash") != graph.inputs_hash:
                out.stale.append(rel)
                out.warnings.append(f"{rel}: built from an older catalog than the one on disk; the next sync rebuilds it")
        out.coherent = not out.stale
    return out


__all__ = ["Validation", "validate_generation"]
