"""Prepared indexes for the detectors and gates (auto-schema Phase 6, steps 1 to 3 of the 23).

  1. type families   already on every ``Column.family`` (6b); nothing to build.
  2. parent keys     which columns identify a row on their own, looked up by normalised name.
  3. names           table stems (learned prefix removed, head noun singular), the name index, the learned list of
                     generic columns, and a "twin key" that puts ``product``, ``products`` and ``product_bak`` together.

Steps 12 and 13 of the plan (learned prefix stripping, case and plural variants) are not separate detectors:
they are the way every stem is built here, so every name rule gets them.

Pure: reads a :class:`CatalogGraph`, never touches a database.

Origin: new (no previous file)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TypeVar

from src.sql.relationships.model import CatalogGraph, Column, Table
from src.sql.relationships.names import (
    NameIndex,
    learn_common_columns,
    normalized_column_name,
    table_stem,
)

T = TypeVar("T")

# Words that mark a table as a copy, backup or scratch version of another. Generic English, not database names.
COPY_MARKERS = frozenset({"bak", "backup", "bkp", "old", "tmp", "temp", "test", "copy", "archive", "archived", "dup", "dupe", "sample", "demo", "trash"})
_VERSION_WORDS = frozenset({"v", "ver", "version"})


def twin_key(stem: tuple[str, ...], markers: frozenset[str] = COPY_MARKERS) -> tuple[tuple[str, ...], bool]:
    """A stem without copy markers, digits and version words, and whether anything was removed.

    ``(product, bak)`` -> ``((product,), True)``; ``(product,)`` -> ``((product,), False)``.
    ``markers`` defaults to Phase 6's list; Phase 7 passes a slightly wider one.
    """
    kept: list[str] = []
    removed = False
    for i, tok in enumerate(stem):
        nxt = stem[i + 1] if i + 1 < len(stem) else ""
        if tok in markers or tok.isdigit() or (tok in _VERSION_WORDS and nxt.isdigit()):
            removed = True
            continue
        kept.append(tok)
    if not kept:  # a table called just "test" or "2023": leave it alone
        return stem, False
    return tuple(kept), removed


@dataclass
class Context:
    graph: CatalogGraph
    tables: list[Table]  # tables only (views are never a side of an edge), sorted by qualified name
    names: NameIndex
    common: frozenset[str]  # normalised column names that say nothing about a reference
    stems: dict[str, tuple[str, ...]]  # qualified name -> stem
    twin: dict[str, tuple[tuple[str, ...], bool]]  # qualified name -> (twin key, has a copy marker)
    keyed: dict[str, list[tuple[Table, Column]]]  # normalised name -> single-column keys with that name
    shapes: dict[tuple[int, str], list[Table]] = field(default_factory=dict)  # (token count, first letter) -> tables

    def sole_pk(self, table: Table) -> Column | None:
        """The primary-key column when the key is one column; None for a composite or missing key."""
        return table.column(table.primary_key[0]) if len(table.primary_key) == 1 else None

    def parent_column(self, parent: Table, child_column: str) -> Column | None:
        """The column of ``parent`` a column called ``child_column`` most likely points at.

        The single primary key first; else a single-column key with the same name; else a column with the same
        name; else ``id``. Whether that column is a real key is for the parent-key gate (6d step 17) to judge.
        """
        pk = self.sole_pk(parent)
        if pk is not None:
            return pk
        norm = normalized_column_name(child_column)
        same = [c for c in parent.columns if normalized_column_name(c.name) == norm]
        for c in same:
            if parent.key_strength(c.name):
                return c
        if same:
            return same[0]
        return parent.column("id")

    def prefer_same_schema(self, child: Table, items: Iterable[T], table_of: Callable[[T], Table] | None = None) -> list[T]:
        """When some targets live in the child's own schema, ignore the ones in other schemas."""
        found = list(items)
        get = table_of or (lambda x: x)  # type: ignore[return-value]
        own = (child.schema or "").lower()
        same = [x for x in found if (get(x).schema or "").lower() == own]
        return same or found

    def rows(self, table: Table) -> int | None:
        """A usable row estimate. Zero and negative values (never analysed) count as unknown."""
        n = table.row_estimate
        return n if isinstance(n, int) and n > 0 else None


def prepare(graph: CatalogGraph) -> Context:
    tables = sorted((t for t in graph.tables.values() if not t.is_view), key=lambda t: t.qualified)
    names = NameIndex(tables)
    stems = {t.qualified: table_stem(t.name, names.prefixes) for t in tables}
    keyed: dict[str, list[tuple[Table, Column]]] = {}
    shapes: dict[tuple[int, str], list[Table]] = {}
    for t in tables:
        for c in t.columns:
            if t.key_strength(c.name):
                keyed.setdefault(normalized_column_name(c.name), []).append((t, c))
        stem = stems[t.qualified]
        if stem and stem[0]:
            shapes.setdefault((len(stem), stem[0][0]), []).append(t)
    return Context(
        graph=graph,
        tables=tables,
        names=names,
        common=learn_common_columns(tables),
        stems=stems,
        twin={q: twin_key(s) for q, s in stems.items()},
        keyed=keyed,
        shapes=shapes,
    )


__all__ = ["COPY_MARKERS", "Context", "prepare", "twin_key"]
