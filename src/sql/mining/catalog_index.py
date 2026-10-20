"""A small in-memory index of a database's generated ``schema/schema.json`` (auto-schema Phase 5).

Used to (1) turn the table names found in procedures into the catalog's own names, (2) check that a
column really exists, and (3) recognise a join that is already a declared foreign key. Everything is
matched case-insensitively because engines differ (Oracle upper-case, PostgreSQL lower-case).

Origin: new (no previous file)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CatalogIndex:
    def __init__(
        self,
        tables: dict[str, set[str]],
        *,
        default_schema: str | None = None,
        fk_pairs: frozenset[frozenset[tuple[str, str]]] = frozenset(),
    ) -> None:
        self._columns = {name: {c.lower() for c in cols} for name, cols in tables.items()}
        self._by_lower = {name.lower(): name for name in tables}
        self._bare: dict[str, list[str]] = {}
        for name in tables:
            self._bare.setdefault(name.lower().rsplit(".", 1)[-1], []).append(name)
        self._default_schema = (default_schema or "").lower() or None
        self._fk_pairs = fk_pairs

    def __len__(self) -> int:
        return len(self._columns)

    @classmethod
    def from_schema_json(cls, path: Path) -> CatalogIndex | None:
        """Build from a ``schema.json`` file. None if it is missing or unusable (mining then runs unvalidated)."""
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls.from_document(doc)
        except (OSError, ValueError, TypeError):
            return None

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> CatalogIndex | None:
        tables: dict[str, set[str]] = {}
        for t in doc.get("tables") or []:
            name = t.get("qualified_name") or t.get("name")
            if not name:
                continue
            tables[str(name)] = {str(c.get("name")) for c in t.get("columns") or [] if c.get("name")}
        if not tables:
            return None
        pairs: set[frozenset[tuple[str, str]]] = set()
        for fk in doc.get("relationships") or []:
            try:
                a = ((fk.get("from_schema") + "." if fk.get("from_schema") else "") + fk["from_table"], fk["from_column"].lower())
                b = ((fk.get("to_schema") + "." if fk.get("to_schema") else "") + fk["to_table"], fk["to_column"].lower())
            except (KeyError, TypeError, AttributeError):
                continue
            pairs.add(frozenset({(a[0].lower(), a[1]), (b[0].lower(), b[1])}))
        return cls(tables, default_schema=doc.get("default_schema"), fk_pairs=frozenset(pairs))

    def resolve_table(self, name: str) -> str | None:
        """The catalog's name for a table written in a procedure (``po_head`` / ``dbo.PO_Head``), or None if unknown."""
        low = name.lower()
        if low in self._by_lower:
            return self._by_lower[low]
        bare = self._bare.get(low.rsplit(".", 1)[-1], [])
        if len(bare) == 1:
            return bare[0]
        if len(bare) > 1 and "." not in low and self._default_schema:
            wanted = f"{self._default_schema}.{low}"
            return self._by_lower.get(wanted)
        return None

    def has_column(self, table: str, column: str) -> bool:
        return column.lower() in self._columns.get(table, ())

    def owners(self, tables: list[str], column: str) -> list[str]:
        """Which of ``tables`` have this column."""
        return [t for t in tables if self.has_column(t, column)]

    def is_declared_fk(self, a: tuple[str, str], b: tuple[str, str]) -> bool:
        return frozenset({(a[0].lower(), a[1].lower()), (b[0].lower(), b[1].lower())}) in self._fk_pairs


__all__ = ["CatalogIndex"]
