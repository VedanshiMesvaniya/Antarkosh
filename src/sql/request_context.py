"""One immutable object per SQL request that holds every fact the stages of that request need.

Before this, each stage looked its own facts up: the prompt knew the dialect, the repair did not; the result validator once fell
back to ANOTHER database's relationships; soft-delete rules, relationships and column types were each loaded where they were used.
``SQLRequestContext`` is built once in ``SQLRetriever._retrieve_with_delta_repair`` (after the schema is retrieved) and passed
down, so generation, validation, repair and execution all describe the same database.

It composes the cached ``DatabaseContext`` (connection and paths) with what is specific to a request: the dialect profile, the
database's own relationships, its soft-delete specs, and the column types from ``schema/schema.json``. Every loader is
best-effort: a missing file gives an empty value, never an error, so building a context can never fail a query.

``strict`` says whether the extra deterministic checks (SQL normalisation, type validation, syntax repair without a model) apply:
true only for a database other than ``erp_main`` that runs the Phase 12 prompt flags in apply mode, the same switch the earlier
phases used. ``erp_main`` and every database outside apply mode behave exactly as before.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot

from src.sql.dialects import SQLDialectProfile
from src.sql.knowledge.loaders import DEFAULT_DB_ID

logger = logging.getLogger(__name__)

_TYPES_CACHE: dict[str, tuple[float, tuple[dict[str, dict[str, str]], dict[str, dict[str, list[str]]]]]] = {}


@dataclass(frozen=True)
class SQLRequestContext:
    """Everything about the selected database that one SQL request needs (treat the mappings as read-only: they are shared caches)."""

    db_id: str
    dialect: SQLDialectProfile
    relationships: tuple[dict[str, Any], ...] = ()
    soft_delete_specs: dict[str, Any] = field(default_factory=dict)
    column_types: dict[str, dict[str, str]] = field(default_factory=dict)  # {table: {column: "varchar(20)"}}, lower case
    column_enums: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # {table: {column: [allowed values]}}
    strict: bool = False
    database: Any = None  # the cached DatabaseContext (connection, paths, access control) when the database is registered

    @property
    def sqlglot_dialect(self) -> str | None:
        return self.dialect.sqlglot_dialect

    def dialect_contract(self) -> str:
        """The target database and how it writes a row limit, derived with sqlglot (no per-dialect text to maintain).

        Empty for ``erp_main`` (its repair prompts are unchanged) and on any problem.
        """
        try:
            if self.db_id == DEFAULT_DB_ID:
                return ""
            limit = sqlglot.transpile("SELECT x FROM t LIMIT 5", read="mysql", write=self.dialect.sqlglot_dialect)[0]
            return f"Target database: {self.dialect.name}. A row limit is written as: {limit}."
        except Exception:  # noqa: BLE001 - extra context only
            return ""

    def summary(self) -> dict[str, Any]:
        """Small, log-safe description of what was resolved (counts, never data)."""
        return {
            "db_id": self.db_id,
            "dialect": self.dialect.sqlglot_dialect,
            "strict": self.strict,
            "relationships": len(self.relationships),
            "soft_delete_tables": len(self.soft_delete_specs),
            "typed_tables": len(self.column_types),
        }


def _load_column_types(db_id: str, databases_dir: Path | None) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, list[str]]]]:
    """``({table: {column: type}}, {table: {column: enum values}})`` from ``schema/schema.json``; cached until the file changes."""
    from src.sql import registry

    path = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id / "schema" / "schema.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}, {}
    cached = _TYPES_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    types: dict[str, dict[str, str]] = {}
    enums: dict[str, dict[str, list[str]]] = {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for table in doc.get("tables") or []:
            name = str(table.get("name") or "").lower()
            if not name:
                continue
            cols = [c for c in table.get("columns") or [] if isinstance(c, dict) and c.get("name")]
            for key in {name, name.rsplit(".", 1)[-1]}:  # qualified and bare
                types[key] = {str(c["name"]).lower(): str(c.get("type") or "") for c in cols}
                enums[key] = {str(c["name"]).lower(): [str(v) for v in c["enum_values"]] for c in cols if c.get("enum_values")}
    except (OSError, ValueError, AttributeError, TypeError):
        types, enums = {}, {}
    _TYPES_CACHE[str(path)] = (mtime, (types, enums))
    return types, enums


def is_strict(db_id: str) -> bool:
    """Do the extra deterministic checks apply to ``db_id``? Non-ERP database in Phase 12 apply mode; any doubt means no."""
    try:
        from src.sql.schema_retrieval import _route_bridge_mode

        return db_id != DEFAULT_DB_ID and _route_bridge_mode(db_id) == "apply"
    except Exception:  # noqa: BLE001 - on any doubt behave exactly as before
        return False


def build_request_context(db_id: str, dialect: SQLDialectProfile, *, databases_dir: Path | None = None) -> SQLRequestContext:
    """Resolve the context for ``db_id``. Never raises: anything that cannot be loaded is simply empty."""
    db_id = (db_id or DEFAULT_DB_ID).strip()
    relationships: tuple[dict[str, Any], ...] = ()
    specs: dict[str, Any] = {}
    types: dict[str, dict[str, str]] = {}
    enums: dict[str, dict[str, list[str]]] = {}
    database: Any = None
    try:
        from src.sql.prompt_builder import get_raw_relationships

        relationships = tuple(get_raw_relationships(db_id) or ())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Request context: relationships unavailable for %s: %s", db_id, exc)
    try:
        from src.sql.soft_delete_spec import load_specs

        specs = load_specs(db_id, databases_dir=databases_dir)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Request context: soft-delete specs unavailable for %s: %s", db_id, exc)
    try:
        types, enums = _load_column_types(db_id, databases_dir)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Request context: column types unavailable for %s: %s", db_id, exc)
    try:
        from src.sql.context import get_context

        database = get_context(db_id)
    except Exception:  # noqa: BLE001 - an unregistered database (tests, ad-hoc ids) has no DatabaseContext
        database = None
    return SQLRequestContext(db_id, dialect, relationships, specs, types, enums, is_strict(db_id), database)


__all__ = ["SQLRequestContext", "build_request_context", "is_strict"]
