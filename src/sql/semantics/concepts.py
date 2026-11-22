"""Local concept mapping: what a universal ERP concept actually means in ONE connected database.

The universal glossary (``universal.py``) says which words people use for "supplier" or "product". It says nothing about any
database. This module links it to the real schema, from metadata only and only through the column meaning cards the earlier phase
already wrote and checked (``semantics/column_dictionary.json``) plus the catalog's own structure:

    concept + real column + role + confidence + evidence + safe_for_prompt

* **Role** of the column for the concept: ``entity_name | identifier | amount | date | status | foreign_key``, from the words left
  of the card's name once the concept is taken out, the column's key structure and its type. A role the column's type cannot hold
  (an ``amount`` that is text) is refused.
* **Confidence.** ``high`` needs a high-confidence card AND something beyond the name: a catalog comment, procedure/view evidence,
  or key structure (primary, unique or foreign key). Name-only readings are NOT high; they stay in the review list. Only ``high``
  mappings become glossary entries; nothing weaker ever reaches a prompt or a route.
* **Never merged.** A card that names two concepts the glossary declares separate is left to the review list. A concept spread over
  more than ``MAX_CONCEPT_TABLES`` tables is too ambiguous to map. A word the glossary does not know maps nothing.
* **No codes.** A phrase that is a code (``y``, ``0``, ``+``) can never become a synonym.

Entries keep the shape the prompt builder and the router already read (``maps_to``, ``note``, ``synonyms``, ``auto``) and add the
concept fields: ``concept_id``, ``aliases_seen``, ``mappings`` (the full list, in the master spec's shape), ``confidence`` and
``safe_for_prompt``. A reader that ignores the new keys sees the same file as before.

Pure functions: no file is read or written here, no model is called, no data row is touched.

Origin: new (no previous file)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.sql.relationships.model import CatalogGraph, Table
from src.sql.relationships.names import learn_common_columns, normalized_column_name
from src.sql.semantics.evidence import HARD_KINDS
from src.sql.semantics.universal import NON_EXPANDING_KINDS, Match, UniversalGlossary, words_of

MAX_MAPPINGS = 6  # per concept entry
MAX_CONCEPT_TABLES = 3  # a concept whose subject columns sit in more tables than this is too ambiguous to map
MAX_CONCEPT_ENTRIES = 200
MAX_REVIEW_PER_SECTION = 300
MAX_NOTE_CHARS = 120

GENERIC_MEASURES = frozenset({"quantity", "amount", "rate", "unit price", "total amount"})  # a role of the column, not its subject
_NAME_WORDS = frozenset({"name", "title", "description", "desc", "label"})
_ID_WORDS = frozenset({"number", "no", "code", "id", "identifier", "key", "ref", "reference"})
_AMOUNT_WORDS = frozenset({"amount", "total", "value", "price", "cost", "balance", "rate", "quantity", "qty"})
_DATE_WORDS = frozenset({"date", "time", "datetime", "timestamp", "period", "day", "month", "year"})
_STATUS_WORDS = frozenset({"status", "state", "flag", "stage"})
_ROLE_RANK = {"entity_name": 0, "identifier": 1, "amount": 2, "date": 3, "status": 4, "foreign_key": 5}
_FAMILIES_FOR_ROLE = {
    "amount": {"integer", "numeric"},
    "date": {"date", "datetime", "time"},
    "entity_name": {"string", "other"},
}
_CODE_PHRASE = re.compile(r"^[^a-z]*$|^[a-z]$")  # no letters at all, or one letter: y, n, 0, 1, +, -
_EVIDENCE_NAMES = {
    "comment": "column comment",
    "fk": "foreign key",
    "alias": "procedure/view metadata",
    "param": "procedure/view metadata",
    "case": "procedure/view metadata",
}


@dataclass
class ConceptLayer:
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    needs_stronger_evidence: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def is_code_phrase(phrase: str) -> bool:
    """A phrase that is a code or a symbol, never a word a business person says (``y``, ``0``, ``+``)."""
    return bool(_CODE_PHRASE.match(phrase.strip().lower()))


def _evidence_kind(item_id: str) -> str:
    return re.sub(r"\d+$", "", item_id)


def _declared_fk_columns(graph: CatalogGraph) -> set[tuple[str, str]]:
    """``(qualified table, column)`` pairs (lower case) that the catalog declares as foreign keys."""
    return {(f.from_table.lower(), f.from_column.lower()) for f in graph.declared_fks}


def _is_fk(table: Table, col_name: str, declared: set[tuple[str, str]]) -> bool:
    col = table.column(col_name)
    return bool(col is not None and col.is_foreign_key) or (table.qualified.lower(), col_name.lower()) in declared


def _role(table: Table, col_name: str, leftover: list[str], generic: str | None, declared: set[tuple[str, str]]) -> str:
    col = table.column(col_name)
    if col is not None and col.is_primary_key:
        return "identifier"
    if _is_fk(table, col_name, declared):
        return "foreign_key"
    last = leftover[-1] if leftover else ""
    if generic in ("amount", "total amount", "unit price", "rate", "quantity") or last in _AMOUNT_WORDS:
        return "amount"
    if last in _ID_WORDS:
        return "identifier"
    if last in _DATE_WORDS:
        return "date"
    if last in _STATUS_WORDS:
        return "status"
    return "entity_name"


def _structure(table: Table, col_name: str, declared: set[tuple[str, str]]) -> list[str]:
    col = table.column(col_name)
    out: list[str] = []
    if col is not None and col.is_primary_key:
        out.append("primary key")
    strength = table.key_strength(col_name)
    if strength in ("unique", "unique_index"):
        out.append("unique key")
    if _is_fk(table, col_name, declared):
        out.append("foreign key")
    return out


def _subject(matches: list[Match]) -> tuple[Match | None, str | None, str]:
    """``(the concept the column is about, the generic measure it names, why none)``. Generic measures (quantity, amount ...) are a
    role, not a subject; non-expanding kinds (intent, state) are never a subject."""
    generic: str | None = None
    subjects: list[Match] = []
    for m in matches:
        if m.concept.kind in NON_EXPANDING_KINDS:
            continue
        if m.concept.label in GENERIC_MEASURES:
            generic = generic or m.concept.label
        else:
            subjects.append(m)
    distinct = {m.concept.concept_id for m in subjects}
    if len(distinct) == 1:
        return subjects[0], generic, ""
    return None, generic, "two concepts" if distinct else "no concept"


def build_concept_layer(doc: dict[str, Any], graph: CatalogGraph, glossary: UniversalGlossary, taken_terms: set[str]) -> ConceptLayer:
    """Concept-linked glossary entries (high confidence only) and the review items for everything else. Pure."""
    layer = ConceptLayer()
    stats = {"cards_seen": 0, "linked": 0, "high": 0, "not_high": 0, "ambiguous": 0}
    common = learn_common_columns(graph.tables.values())
    declared = _declared_fk_columns(graph)
    per_concept: dict[str, list[dict[str, Any]]] = {}

    for table_name, entry in sorted((doc.get("tables") or {}).items()):
        table = graph.tables.get(table_name)
        if table is None or not isinstance(entry, dict):
            continue
        for col_name, card in sorted((entry.get("columns") or {}).items()):
            col = table.column(col_name)
            if col is None or not isinstance(card, dict) or not card.get("full_name"):
                continue
            if normalized_column_name(col.name) in common or card.get("confidence") not in ("high", "medium"):
                continue
            stats["cards_seen"] += 1
            words = list(words_of(card["full_name"]))
            matches = glossary.match(words)
            if not matches:
                continue
            subject, generic, why = _subject(matches)
            where = f"{table.qualified}.{col.name}"
            if subject is None:
                if why == "two concepts":
                    stats["ambiguous"] += 1
                    layer.ambiguous.append({"kind": "two_concepts", "column": where, "full_name": card["full_name"],
                                            "concepts": sorted({m.concept.concept_id for m in matches if m.concept.kind not in NON_EXPANDING_KINDS})})
                continue
            stats["linked"] += 1
            covered = {i for m in matches for i in range(m.start, m.end)}
            leftover = [w for i, w in enumerate(words) if i not in covered]
            role = _role(table, col.name, leftover, generic, declared)
            allowed = _FAMILIES_FOR_ROLE.get(role)
            if allowed is not None and col.family not in allowed:
                continue  # a role the column's type cannot hold: not a mapping at all
            kinds = {_evidence_kind(i) for i in card.get("evidence") or []}
            structure = _structure(table, col.name, declared)
            hard = bool(kinds & HARD_KINDS) or bool(col.comment and col.comment.strip())
            evidence = ["column name", *structure]
            evidence += sorted({_EVIDENCE_NAMES[k] for k in kinds if k in _EVIDENCE_NAMES})
            if col.comment and col.comment.strip() and "column comment" not in evidence:
                evidence.append("column comment")
            mapping = {
                "table": table.qualified, "column": col.name, "role": role, "evidence": evidence, "meaning": str(card.get("meaning") or ""),
            }
            if card.get("confidence") == "high" and (hard or structure):
                mapping |= {"confidence": "high", "safe_for_prompt": True}
                per_concept.setdefault(subject.concept.concept_id, []).append(mapping)
                stats["high"] += 1
            else:
                stats["not_high"] += 1
                layer.needs_stronger_evidence.append({
                    "concept_id": subject.concept.concept_id, "table": table.qualified, "column": col.name, "role": role,
                    "confidence": card.get("confidence"), "reason": "the name is the only evidence" if card.get("confidence") == "high" else "medium confidence card",
                })

    for concept_id, mappings in sorted(per_concept.items()):
        concept = glossary.by_id[concept_id]
        mappings.sort(key=lambda m: (_ROLE_RANK[m["role"]], m["table"], m["column"]))
        tables = {m["table"] for m in mappings if m["role"] in ("entity_name", "identifier")}
        if len(tables) > MAX_CONCEPT_TABLES:
            stats["ambiguous"] += 1
            layer.ambiguous.append({"kind": "spread_over_tables", "concept_id": concept_id, "tables": len(tables)})
            continue
        if concept.label in taken_terms or len(layer.entries) >= MAX_CONCEPT_ENTRIES:
            continue  # a card entry or a person already owns the word
        best = next((m for m in mappings if m["role"] != "foreign_key"), mappings[0])
        shown = mappings[:MAX_MAPPINGS]
        synonyms = [a for a in concept.aliases if a != concept.label and not is_code_phrase(a)]
        seen_words = {w for m in shown for w in words_of(f"{m['column']} {m['meaning']}")}
        aliases_seen = [a for a in concept.aliases if all(w in seen_words for w in words_of(a))]
        layer.entries[concept.label] = {
            "maps_to": f"{best['table']}.{best['column']}",
            "note": f"{concept.label} ({best['role'].replace('_', ' ')}): {best['meaning']}"[:MAX_NOTE_CHARS],
            "synonyms": synonyms,
            "concept_id": concept_id,
            "aliases_seen": aliases_seen,
            "mappings": [{k: v for k, v in m.items() if k != "meaning"} for m in shown],
            "confidence": "high",
            "safe_for_prompt": True,
        }
    layer.stats = {**stats, "entries": len(layer.entries)}
    return layer


__all__ = ["ConceptLayer", "build_concept_layer", "is_code_phrase"]
