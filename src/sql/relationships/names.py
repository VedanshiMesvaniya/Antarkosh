"""Name handling for relationship inference (auto-schema Phase 6, step 6b). Pure functions, no I/O.

Real databases name things in many styles: ``customer_id``, ``CustomerID``, ``custId``, ``tbl_customers``,
``PartyMaster``. These helpers turn all of them into comparable tokens so a rule can ask "does this column
name point at that table?" without caring about style. Everything is learned from the database's own
names where possible; the only fixed lists are small and generic (plural exceptions, id-like suffixes).

Origin: new (no previous file)
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from src.sql.relationships.model import Table

_TOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")

# Words that mark a column as a key / reference to something else.
ID_SUFFIXES = frozenset({"id", "no", "num", "number", "code", "key", "ref", "fk"})
ID_PREFIXES = frozenset({"id", "fk"})

# Column names that say nothing about WHAT they reference. Used (with the learned list) to stop two tables
# from being joined only because both have a column called "name" or "status".
SEED_COMMON_COLUMNS = frozenset({
    "id", "name", "code", "status", "type", "description", "remarks", "remark", "notes", "note", "title", "comment",
    "comments", "created_at", "created_on", "created_date", "updated_at", "updated_on", "updated_date", "deleted_at",
    "deleted_on", "modified_at", "modified_on", "is_active", "active", "is_deleted", "deleted", "flag", "sort_order",
    "seq", "sequence", "version", "date", "value", "amount", "qty", "quantity",
})

_IRREGULAR_SINGULAR = {
    "people": "person", "children": "child", "men": "man", "women": "woman", "feet": "foot", "teeth": "tooth",
    "mice": "mouse", "criteria": "criterion", "analyses": "analysis", "indices": "index", "matrices": "matrix",
    "statuses": "status", "addresses": "address", "buses": "bus", "aliases": "alias", "data": "data", "media": "media",
    "series": "series", "species": "species", "news": "news",
}


def split_identifier(name: str) -> list[str]:
    """Lower-case tokens of an identifier: ``PartyID`` -> party, id; ``po_head`` -> po, head; ``POHead`` -> po, head."""
    out: list[str] = []
    for chunk in re.sub(r"[^0-9A-Za-z]+", " ", name or "").split():
        out.extend(t.lower() for t in _TOKEN.findall(chunk))
    return out


def singular(token: str) -> str:
    """A simple English singular. Words ending in ss / us / is are left alone (status, address, analysis)."""
    t = token.lower()
    if t in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[t]
    if len(t) <= 3:
        return t
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith(("sses", "shes", "ches", "xes", "zes")):
        return t[:-2]
    if t.endswith(("ss", "us", "is")):
        return t
    if t.endswith("s"):
        return t[:-1]
    return t


def stem_tokens(tokens: Iterable[str]) -> tuple[str, ...]:
    """Singularise the last token (the head noun): po, details -> po, detail."""
    toks = list(tokens)
    if toks:
        toks[-1] = singular(toks[-1])
    return tuple(toks)


# ---------------------------------------------------------------- learned table prefixes


def learn_table_prefixes(table_names: Iterable[str], *, min_tables: int = 5, min_share: float = 0.30, max_len: int = 4) -> frozenset[str]:
    """Short leading tokens that most tables share (``tbl``, ``m``, ``mst``, ``trn``): learned, never configured.

    A token counts only if it is at most ``max_len`` characters, starts at least ``min_tables`` tables, and starts at
    least ``min_share`` of all tables. Stripping it never empties a name.
    """
    names = [split_identifier(n) for n in table_names]
    names = [n for n in names if len(n) >= 2]
    if not names:
        return frozenset()
    counts = Counter(n[0] for n in names)
    total = len(names)
    return frozenset(tok for tok, c in counts.items() if len(tok) <= max_len and c >= min_tables and c / total >= min_share)


def table_stem(name: str, prefixes: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """A table name reduced to comparable tokens: prefix removed, head noun singular. ``tbl_customers`` -> (customer,)."""
    toks = split_identifier(name)
    if len(toks) >= 2 and toks[0] in prefixes:
        toks = toks[1:]
    return stem_tokens(toks)


# ---------------------------------------------------------------- id-like columns


@dataclass(frozen=True)
class IdColumn:
    """A column name read as "<stem> <suffix>": ``customer_id`` -> stem (customer,), suffix id."""

    stem: tuple[str, ...]  # empty for a bare ``id`` / ``code``
    suffix: str
    glued: bool = False  # the suffix was glued on without a separator (``custid``); less certain


def _has_word(stem: tuple[str, ...]) -> bool:
    """A stem must hold a real word (letters, two or more), so ``id3x`` or ``x_id`` are not read as references."""
    return any(tok.isalpha() and len(tok) >= 2 for tok in stem)


def parse_id_column(name: str) -> IdColumn | None:
    """Read an id-like column name. None if the name does not look like a key or reference."""
    toks = split_identifier(name)
    if not toks:
        return None
    if len(toks) == 1:
        tok = toks[0]
        if tok in ID_SUFFIXES:
            return IdColumn((), tok)
        if len(tok) > 4 and tok.endswith("id") and not tok.endswith(("paid", "valid", "void", "rapid", "solid", "fluid")):
            stem = stem_tokens([tok[:-2]])
            return IdColumn(stem, "id", glued=True) if _has_word(stem) else None
        return None
    if toks[-1] in ID_SUFFIXES:
        stem = stem_tokens(toks[:-1])
        return IdColumn(stem, toks[-1]) if _has_word(stem) else None
    if toks[0] in ID_PREFIXES:
        stem = stem_tokens(toks[1:])
        return IdColumn(stem, toks[0]) if _has_word(stem) else None
    return None


def normalized_column_name(name: str) -> str:
    """``PartyID`` and ``party_id`` both become ``party_id``."""
    return "_".join(split_identifier(name))


def learn_common_columns(tables: Iterable[Table], *, min_tables: int = 5, min_share: float = 0.30) -> frozenset[str]:
    """Column names that appear in many tables (audit columns, ``company_id`` in a multi-tenant schema, ``name``).

    Two tables sharing such a name proves nothing, so a "same column name" rule must skip them. The list is
    returned as data; whether a common ``<x>_id`` column is still a real reference to table ``x`` is decided by
    the rule that matches it to ``x``, not here. The fixed seed list is always included.
    """
    tbls = [t for t in tables if not t.is_view]
    counts: Counter[str] = Counter()
    for t in tbls:
        counts.update({normalized_column_name(c.name) for c in t.columns})
    total = len(tbls)
    learned = {n for n, c in counts.items() if total and c >= min_tables and c / total >= min_share}
    return frozenset(SEED_COMMON_COLUMNS | learned)


# ---------------------------------------------------------------- finding tables by name


class NameIndex:
    """Look tables up by the way a column name refers to them. Views are never offered as targets."""

    def __init__(self, tables: Iterable[Table], prefixes: frozenset[str] | None = None) -> None:
        tbls = [t for t in tables if not t.is_view]
        self.prefixes = prefixes if prefixes is not None else learn_table_prefixes(t.name for t in tbls)
        self._by_stem: dict[tuple[str, ...], list[Table]] = {}
        self._by_head: dict[str, list[Table]] = {}
        self._by_first: dict[str, list[Table]] = {}
        for t in tbls:
            stem = table_stem(t.name, self.prefixes)
            if not stem:
                continue
            self._by_stem.setdefault(stem, []).append(t)
            self._by_head.setdefault(stem[-1], []).append(t)
            self._by_first.setdefault(singular(stem[0]), []).append(t)

    def exact(self, stem: tuple[str, ...]) -> list[Table]:
        """Tables whose whole name is this stem: (customer,) -> customers, tbl_customer, Customer."""
        return list(self._by_stem.get(stem, ()))

    def by_head(self, token: str) -> list[Table]:
        """Tables whose last word is this word: party -> party, erp_party, vendor_party."""
        return list(self._by_head.get(singular(token), ()))

    def by_abbreviation(self, token: str, min_len: int = 3) -> list[Table]:
        """Tables whose first word starts with this short word: cust -> customer, customer_group. Needs >= min_len letters."""
        tok = token.lower()
        if len(tok) < min_len:
            return []
        seen: dict[str, Table] = {}
        for first, tbls in self._by_first.items():
            if first.startswith(tok) and first != tok:
                for t in tbls:
                    seen[t.qualified] = t
        return list(seen.values())


__all__ = [
    "ID_PREFIXES",
    "ID_SUFFIXES",
    "SEED_COMMON_COLUMNS",
    "IdColumn",
    "NameIndex",
    "learn_common_columns",
    "learn_table_prefixes",
    "normalized_column_name",
    "parse_id_column",
    "singular",
    "split_identifier",
    "stem_tokens",
    "table_stem",
]
