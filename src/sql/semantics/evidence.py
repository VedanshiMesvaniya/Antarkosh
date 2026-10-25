"""Column evidence (auto-schema Phase 9a): everything already known about each column, as small citable items. No LLM.

For every column this gathers, from files earlier phases already wrote:

* ``comment``  the column's comment in the database catalog;
* ``fk``       the column it references (declared keys, ``relationships.json``, trusted inferred edges), with the target
               table's comment;
* ``alias``    names procedures and scripts give the column (``AS PurchaseOrderNumber``);
* ``param``    parameters it is compared with (``= @PONumber``), as words;
* ``case``     what stored codes mean (``1 = Open``);
* ``values``   short string values procedures compare it with;
* ``abbrev``   the generic abbreviations in its name (``po = purchase order``, ``no = number``).

Each item has an id (``comment``, ``alias1`` ...) so a meaning can cite it and the citation can be checked. The first five
(comment, fk, alias, param, case) are NAMING evidence: somebody wrote down what the column is or means, so they can justify
a ``high`` confidence. ``values`` is context (a literal such as 'A1' says nothing about what ``apq`` stands for) and ``abbrev``
is only a reading of the name; both are shown to the model and may be cited, but neither can justify ``high`` on its own.

Also per table: the names of its other columns, because ``po_no`` next to ``po_dt`` and ``po_vendor`` is a purchase order.
Only metadata is read. Nothing here reads a data row or contacts a database.

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.sql.paths.links import collect_joins, select_nodes
from src.sql.relationships.model import CatalogGraph, Table
from src.sql.relationships.names import learn_common_columns, normalized_column_name
from src.sql.semantics.abbreviations import abbreviation_notes, self_explanatory
from src.sql.semantics.hints import parameter_words

EVIDENCE_VERSION = 1
HARD_KINDS = frozenset({"comment", "fk", "alias", "param", "case"})  # evidence that names or explains the column
MAX_TEXT = 200
MAX_NEIGHBORS = 60
MAX_CASE_SHOWN = 8
MAX_VALUES_SHOWN = 8


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    text: str


@dataclass(frozen=True)
class ColumnEvidence:
    name: str
    type: str
    is_primary_key: bool
    is_foreign_key: bool
    items: tuple[Item, ...]
    common: bool  # a name many tables share (id, name, created_at, company_id): its meaning is not specific to one table
    explains_itself: bool  # every word of the name is an ordinary word or a listed abbreviation

    @property
    def hard(self) -> bool:
        return any(i.kind in HARD_KINDS for i in self.items)

    def item_ids(self) -> set[str]:
        return {i.id for i in self.items}


@dataclass(frozen=True)
class TableEvidence:
    table: str  # the catalog's qualified name
    name: str
    kind: str
    comment: str | None
    neighbors: tuple[str, ...]
    columns: tuple[ColumnEvidence, ...]
    hash: str  # changes only when something that would change the meaning changes


def _short(text: Any, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _by_table_column(graph: CatalogGraph, rows: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Index a list of ``{"table", "column", ...}`` rows by (qualified table, lower-case column); rows that do not resolve are dropped."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        table = graph.table(str(row.get("table") or ""))
        column = str(row.get("column") or "").lower()
        if table is not None and column and table.column(column) is not None:
            out[(table.qualified, column)] = row
    return out


def _fk_targets(
    graph: CatalogGraph,
    relationships_doc: dict[str, Any] | None,
    inferred_doc: dict[str, Any] | None,
    profiles_doc: dict[str, Any] | None,
) -> dict[tuple[str, str], tuple[str, str]]:
    """(table, lower column) -> (target table, target column), from the same trusted edges Phase 8 uses."""
    inputs = select_nodes(graph, profiles_doc)
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for j in collect_joins(graph, inputs, relationships_doc=relationships_doc, inferred_doc=inferred_doc):
        if j.child is None:
            continue
        parent = j.b if j.child == j.a else j.a
        out.setdefault((j.child, j.column_of(j.child).lower()), (parent, j.column_of(parent)))
    return out


def _column_items(
    table: Table,
    col_name: str,
    comment: str | None,
    fk: tuple[str, str] | None,
    graph: CatalogGraph,
    hint: dict[str, Any] | None,
    values_row: dict[str, Any] | None,
) -> list[Item]:
    items: list[Item] = []
    if comment and comment.strip():
        items.append(Item("comment", "comment", _short(comment)))
    if fk is not None:
        target = graph.tables.get(fk[0])
        note = f" ({_short(target.comment, 80)})" if target is not None and target.comment else ""
        items.append(Item("fk", "fk", f"references {fk[0]}.{fk[1]}{note}"))
    if hint:
        for n, a in enumerate(hint.get("aliases") or [], 1):
            if isinstance(a, dict) and a.get("alias"):
                items.append(Item(f"alias{n}", "alias", _short(a["alias"], 60)))
        for n, p in enumerate(hint.get("params") or [], 1):
            words = parameter_words(str(p.get("name") or "")) if isinstance(p, dict) else ""
            if words:
                items.append(Item(f"param{n}", "param", words))
        pairs = [(str(c.get("value")), str(c.get("label"))) for c in hint.get("case_labels") or [] if isinstance(c, dict) and c.get("label") is not None]
        if pairs:
            items.append(Item("case", "case", "; ".join(f"{v} = {lab}" for v, lab in pairs[:MAX_CASE_SHOWN])))
    if values_row:
        vals = [str(v.get("value")) for v in values_row.get("values") or [] if isinstance(v, dict) and v.get("value") is not None]
        if vals:
            items.append(Item("values", "values", ", ".join(f"'{_short(v, 30)}'" for v in vals[:MAX_VALUES_SHOWN])))
    notes = abbreviation_notes(col_name)
    if notes:
        items.append(Item("abbrev", "abbrev", "; ".join(notes)))
    return items


def collect_table_evidence(
    graph: CatalogGraph,
    *,
    hints_doc: dict[str, Any] | None = None,
    evidence_doc: dict[str, Any] | None = None,
    relationships_doc: dict[str, Any] | None = None,
    inferred_doc: dict[str, Any] | None = None,
    profiles_doc: dict[str, Any] | None = None,
    include_views: bool = False,
) -> dict[str, TableEvidence]:
    """Evidence for every table (views only on request), keyed by qualified table name."""
    hints = _by_table_column(graph, (hints_doc or {}).get("columns"))
    values = _by_table_column(graph, (evidence_doc or {}).get("enum_values"))
    fks = _fk_targets(graph, relationships_doc, inferred_doc, profiles_doc)
    common = learn_common_columns(graph.tables.values())

    out: dict[str, TableEvidence] = {}
    for q in sorted(graph.tables):
        table = graph.tables[q]
        if table.is_view and not include_views:
            continue
        neighbors = tuple(c.name for c in table.columns)[:MAX_NEIGHBORS]
        cols: list[ColumnEvidence] = []
        for c in table.columns:
            key = (q, c.name.lower())
            items = _column_items(table, c.name, c.comment, fks.get(key), graph, hints.get(key), values.get(key))
            cols.append(ColumnEvidence(
                name=c.name, type=c.type, is_primary_key=c.is_primary_key, is_foreign_key=c.is_foreign_key,
                items=tuple(items), common=normalized_column_name(c.name) in common, explains_itself=self_explanatory(c.name),
            ))
        canon = json.dumps(
            [EVIDENCE_VERSION, q, table.comment, list(neighbors), [[c.name, c.type, c.is_primary_key, [[i.id, i.text] for i in c.items]] for c in cols]],
            sort_keys=True,
        )
        out[q] = TableEvidence(
            table=q, name=table.name, kind=table.kind, comment=table.comment, neighbors=neighbors, columns=tuple(cols),
            hash=hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16],
        )
    return out


def evidence_summary(tables: dict[str, TableEvidence]) -> dict[str, Any]:
    """Counts only."""
    cols = [c for t in tables.values() for c in t.columns]
    kinds: dict[str, int] = {}
    for c in cols:
        for k in {i.kind for i in c.items}:
            kinds[k] = kinds.get(k, 0) + 1
    return {
        "tables": len(tables),
        "columns": len(cols),
        "with_hard_evidence": sum(1 for c in cols if c.hard),
        "self_explanatory_only": sum(1 for c in cols if not c.hard and c.explains_itself),
        "no_evidence": sum(1 for c in cols if not c.hard and not c.explains_itself),
        "by_kind": {k: kinds[k] for k in sorted(kinds)},
    }


__all__ = [
    "EVIDENCE_VERSION", "HARD_KINDS", "ColumnEvidence", "Item", "TableEvidence", "collect_table_evidence", "evidence_summary",
]
