"""Column hints from stored procedures and scripts (auto-schema Phase 9a): select aliases, parameter names, CASE labels.

Phase 5 deliberately keeps facts only and drops statement text. This module adds three more FACTS to that same single
pass over the routines that were already read (so no database is touched again), each tied to a real catalog column:

* ``aliases``: ``SELECT h.po_no AS PurchaseOrderNumber``, the name a developer gave the column when presenting it
  (also the way a view or an uploaded ``CREATE VIEW`` script renames columns);
* ``params``: ``WHERE h.po_no = @PONumber``, the parameter a column is compared with;
* ``case_labels``: ``CASE WHEN h.status_cd = 1 THEN 'Open'``, what a stored code means.

Only columns that exist in the catalog are recorded, so a temp table, a CTE or a variable can never create a hint. The
result goes to ``schema/column_hints.json``, a separate file: ``sp_evidence.json`` is not changed, so nothing that
already depends on it is touched. A failure while collecting hints never affects the Phase 5 facts.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlglot import exp

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.mining.catalog_index import CatalogIndex
from src.sql.relationships.names import normalized_column_name

GENERATED_BY = "auto_sp_mining"
HINTS_REL = "schema/column_hints.json"
MAX_COLUMNS = 6000
MAX_ALIASES = 8
MAX_PARAMS = 6
MAX_CASE_PAIRS = 20
MAX_TEXT = 60
# Routine parameters in MySQL / Oracle / PL-pgSQL are plain identifiers; these prefixes mark them. ``po_`` is NOT here:
# it is a purchase order, not a parameter.
PARAM_PREFIXES = ("p_", "in_", "i_", "pi_", "v_", "par_", "param_", "arg_")

Key = tuple[str, str]  # (canonical table, lower-case column)


class ColumnHints:
    """The hints of one routine: counts of how often each was seen in it."""

    def __init__(self) -> None:
        self.aliases: dict[Key, Counter[str]] = {}
        self.params: dict[Key, Counter[str]] = {}
        self.case_labels: dict[Key, dict[tuple[str, str], int]] = {}

    def __bool__(self) -> bool:
        return bool(self.aliases or self.params or self.case_labels)


def _clean(text: str) -> str | None:
    text = " ".join(str(text).split())
    return text if text and len(text) <= MAX_TEXT and "\n" not in text else None


def _statement_scope(tree: exp.Expression, catalog: CatalogIndex) -> tuple[dict[str, str], list[str]]:
    """alias or bare name -> canonical table, and the tables in the statement. CTEs and temp tables are never tables here."""
    skip = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    alias_map: dict[str, str] = {}
    tables: list[str] = []
    for tb in tree.find_all(exp.Table):
        name = tb.name.lower()
        if not name or name in skip or name.startswith(("#", "@")):
            continue
        canon = catalog.resolve_table(f"{tb.db}.{tb.name}".lower() if tb.db else name)
        if canon is None:
            continue
        if canon not in tables:
            tables.append(canon)
        alias_map[(tb.alias or tb.name).lower()] = canon
        alias_map.setdefault(name, canon)
    return alias_map, tables


def _resolve(col: exp.Expression, alias_map: dict[str, str], tables: list[str], catalog: CatalogIndex) -> Key | None:
    if not isinstance(col, exp.Column) or not col.name:
        return None
    name = col.name.lower()
    if col.table:
        table = alias_map.get(col.table.lower())
        return (table, name) if table and catalog.has_column(table, name) else None
    owners = catalog.owners(tables, name)
    return (owners[0], name) if len(owners) == 1 else None


def _param_name(node: exp.Expression, tables: list[str], catalog: CatalogIndex) -> str | None:
    """The name of a routine parameter used as a value, or None. ``@x`` and ``:x`` always count; a bare ``p_x`` counts only
    if it is not a column of any table in the statement."""
    if isinstance(node, (exp.Parameter, exp.Placeholder)):
        name = (node.name or "").strip()
        return name if name and not name.isdigit() and name != "?" else None
    if isinstance(node, exp.Column) and not node.table:
        name = node.name
        low = name.lower()
        if low.startswith(PARAM_PREFIXES) and not catalog.owners(tables, low):
            return name
    return None


def extract_column_hints(trees: list[exp.Expression], catalog: CatalogIndex | None) -> ColumnHints:
    """Hints from every statement of one routine. Needs a catalog: without one nothing can be tied to a real column."""
    hints = ColumnHints()
    if catalog is None:
        return hints
    for tree in trees:
        alias_map, tables = _statement_scope(tree, catalog)
        if not tables:
            continue

        for alias in tree.find_all(exp.Alias):
            if not isinstance(alias.parent, exp.Select) or not isinstance(alias.this, exp.Column):
                continue
            key = _resolve(alias.this, alias_map, tables, catalog)
            text = _clean(alias.alias)
            if key and text and normalized_column_name(text) != normalized_column_name(key[1]):
                hints.aliases.setdefault(key, Counter())[text] += 1

        for cmp in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like):
            for col, other in ((cmp.left, cmp.right), (cmp.right, cmp.left)):
                key = _resolve(col, alias_map, tables, catalog)
                param = _param_name(other, tables, catalog) if key else None
                if key and param and (text := _clean(param)):
                    hints.params.setdefault(key, Counter())[text] += 1

        for case in tree.find_all(exp.Case):
            for branch in case.args.get("ifs") or []:
                label = branch.args.get("true")
                if not (isinstance(label, exp.Literal) and label.is_string and (text := _clean(label.this))):
                    continue
                cond = branch.this
                if case.this is not None and isinstance(cond, exp.Literal):  # CASE col WHEN 1 THEN 'Open'
                    key, value = _resolve(case.this, alias_map, tables, catalog), cond.this
                elif isinstance(cond, exp.EQ) and isinstance(cond.right, exp.Literal):  # CASE WHEN col = 1 THEN 'Open'
                    key, value = _resolve(cond.left, alias_map, tables, catalog), cond.right.this
                else:
                    continue
                if key and (v := _clean(value)):
                    pair = (v, text)
                    hints.case_labels.setdefault(key, {})
                    hints.case_labels[key][pair] = hints.case_labels[key].get(pair, 0) + 1
    return hints


class HintsCollector:
    """Adds up the hints of all routines. ``add`` never raises: a routine that cannot be read for hints is only counted."""

    def __init__(self) -> None:
        self.aliases: dict[Key, Counter[str]] = {}
        self.params: dict[Key, Counter[str]] = {}
        self.case_labels: dict[Key, dict[tuple[str, str], int]] = {}
        self.routines = 0
        self.failed = 0

    def add(self, trees: list[exp.Expression], catalog: CatalogIndex | None) -> None:
        try:
            hints = extract_column_hints(trees, catalog)
        except Exception:  # noqa: BLE001 - hints are optional; the Phase 5 facts must never depend on them
            self.failed += 1
            return
        self.routines += 1
        for target, source in ((self.aliases, hints.aliases), (self.params, hints.params)):
            for key, counts in source.items():
                target.setdefault(key, Counter()).update(counts.keys())  # per routine: seen once or more
        for key, pairs in hints.case_labels.items():
            slot = self.case_labels.setdefault(key, {})
            for pair in pairs:
                slot[pair] = slot.get(pair, 0) + 1

    def document(self, db_id: str) -> dict[str, Any]:
        keys = sorted(set(self.aliases) | set(self.params) | set(self.case_labels))
        rows: list[dict[str, Any]] = []
        for table, column in keys[:MAX_COLUMNS]:
            key = (table, column)
            rows.append({
                "table": table,
                "column": column,
                "aliases": [{"alias": a, "routines": n} for a, n in sorted(self.aliases.get(key, {}).items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_ALIASES]],
                "params": [{"name": a, "routines": n} for a, n in sorted(self.params.get(key, {}).items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_PARAMS]],
                "case_labels": [
                    {"value": v, "label": lab, "routines": n}
                    for (v, lab), n in sorted(self.case_labels.get(key, {}).items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_CASE_PAIRS]
                ],
            })
        return {
            "generated_by": GENERATED_BY,
            "kind": "column_hints",
            "version": 1,
            "db_id": db_id,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "stats": {"routines": self.routines, "routines_failed": self.failed, "columns": len(rows)},
            "columns": rows,
        }


def write_hints(db_id: str, doc: dict[str, Any], databases_dir: Path | None = None) -> dict[str, Any]:
    """Write into an EXISTING database folder. Never for erp_main; never over a file this phase did not generate."""
    validate_db_id(db_id)
    result: dict[str, Any] = {"written": [], "skipped": {}}
    if db_id == DEFAULT_DB_ID:
        result["skipped"]["all"] = "pinned database: curated files are never replaced"
        return result
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        result["skipped"]["all"] = "database folder does not exist"
        return result
    path = folder / HINTS_REL
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if not (isinstance(current, dict) and current.get("generated_by") == GENERATED_BY):
            result["skipped"][HINTS_REL] = "a file not generated by auto_sp_mining is present; left untouched"
            return result
    registry.atomic_write_json(path, doc, mode=0o644)
    result["written"].append(HINTS_REL)
    return result


def parameter_words(name: str) -> str:
    """``@PONumber`` -> ``po number``, ``p_vendor_id`` -> ``vendor id``: a parameter name without its prefix, as words."""
    text = name.lstrip("@:")
    low = text.lower()
    for prefix in PARAM_PREFIXES:
        if low.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    return re.sub(r"\s+", " ", " ".join(normalized_column_name(text).split("_"))).strip()


__all__ = [
    "GENERATED_BY", "HINTS_REL", "ColumnHints", "HintsCollector", "extract_column_hints", "parameter_words", "write_hints",
]
