"""The universal ERP glossary: a database-independent dictionary of business concepts (metadata-driven SQL accuracy).

``config/universal_erp_glossary.json`` says which words people use for which business concept ("item" for a product). It is an
ERP VOCABULARY layer, not a schema map: it knows nothing about any table, column, code, join, filter or SQL dialect. What a
concept actually means in a connected database is decided only by that database's own metadata (see ``concepts.py``).

Each concept::

    {"concept_id": "inventory.product", "label": "product", "domain": "inventory", "aliases": ["product", "item", "goods"],
     "conditional_aliases": ["material", "sku"], "do_not_equate_with": ["raw material", "finished good"], "kind": "entity"}

* ``aliases`` (strong, the label included) may widen retrieval whatever the database looks like.
* ``conditional_aliases`` count only where the database's own metadata uses that word (``supported``): "material" widens a search
  for "item" in a database that has material columns, and does nothing in one that does not.
* ``do_not_equate_with`` concepts are never merged: an expansion never adds one of their words, and the validator rejects a
  glossary where a word belongs to both sides.
* ``query_intent`` concepts (count, top, monthly ...) and ``state`` concepts (open, cancelled ...) shape a question's wording
  only. They are never expanded and are never evidence for a table, a column, a join or a code value.

``validate_document`` enforces the rules of the glossary itself (no table or column names, no code values, no SQL words, no
ambiguous abbreviations, unique strong aliases, symmetric do-not-equate lists), so a hand edit cannot quietly break them. A
glossary that fails validation is not used at all: ``load_universal`` returns None and callers behave as without it.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.config import PROJECT_ROOT
from src.sql.relationships.names import singular, split_identifier

logger = logging.getLogger(__name__)

UNIVERSAL_REL = "config/universal_erp_glossary.json"
KINDS = frozenset({"entity", "document", "measure", "attribute", "event", "process", "role", "period", "query_intent", "state"})
NON_EXPANDING_KINDS = frozenset({"query_intent", "state"})  # shape a question; never evidence for a table, column, join or code
MAX_TARGETS_PER_CONCEPT = 4  # words one matched concept may add to a search
MAX_EXPANSION = 8

_PHRASE = re.compile(r"^[a-z][a-z -]*[a-z]$")  # lower-case words, spaces and hyphens: no underscores, dots, digits, quotes, symbols
_MIN_CHARS = 3
# Database-specific abbreviations that mean different things in different databases: never a word of this glossary.
AMBIGUOUS_ABBREVIATIONS = frozenset({"pi", "dc", "po", "so", "rm"})
# Words that are values, UI strings, SQL or schema words: never part of a concept phrase.
CODE_AND_UI_WORDS = frozenset({"create", "button", "yes", "no"})
SQL_WORDS = frozenset({
    "select", "insert", "update", "delete", "drop", "truncate", "alter", "join", "where", "union", "having", "limit", "offset", "fetch",
    "getdate", "curdate", "sysdate", "dateadd", "interval", "null", "distinct", "table", "column", "schema", "database",
})
_INTENT_ONLY_WORDS = frozenset({"top", "by"})  # SQL-shaped words that are legitimate only as query-intent terms
_ID_KEYS = ("concept_id", "label", "domain", "aliases", "conditional_aliases", "do_not_equate_with", "kind")
_LOCK = threading.Lock()


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def words_of(text: str) -> tuple[str, ...]:
    """A phrase as singular lower-case words ("Purchase Orders" -> ("purchase", "order")). Hyphens split words."""
    return tuple(singular(t) for t in split_identifier(text if isinstance(text, str) else ""))


# ---------------------------------------------------------------------------------------------------- validation


def validate_document(doc: Any) -> list[str]:
    """Every way the document breaks the glossary's rules, as short sentences. Empty means it is valid."""
    problems: list[str] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("concepts"), list) or not doc["concepts"]:
        return ["the document must be an object with a non-empty 'concepts' list"]
    concepts = [c for c in doc["concepts"] if isinstance(c, dict)]
    if len(concepts) != len(doc["concepts"]):
        problems.append("every concept must be an object")
    ids: dict[str, dict] = {}
    labels: dict[str, dict] = {}
    strong: dict[str, str] = {}  # word -> concept id (labels and strong aliases)
    for c in concepts:
        cid = c.get("concept_id")
        if set(c) != set(_ID_KEYS):
            problems.append(f"{cid}: keys must be exactly {', '.join(_ID_KEYS)}")
            continue
        label, domain, kind = c["label"], c["domain"], c["kind"]
        lists_ok = all(isinstance(c[k], list) and all(isinstance(x, str) for x in c[k]) for k in ("aliases", "conditional_aliases", "do_not_equate_with"))
        if not (isinstance(cid, str) and isinstance(label, str) and isinstance(domain, str) and lists_ok):
            problems.append(f"{cid}: wrong field types")
            continue
        if cid != f"{_slug(domain)}.{_slug(label)}":
            problems.append(f"{cid}: concept_id must be '<domain>.<label as a slug>'")
        if kind not in KINDS:
            problems.append(f"{cid}: unknown kind {kind!r}")
        if (kind == "query_intent") != (domain == "query_intent"):
            problems.append(f"{cid}: the query_intent kind and domain go together")
        if cid in ids or label in labels:
            problems.append(f"{cid}: duplicate concept id or label")
        ids[cid], labels[label] = c, c
        if label not in c["aliases"]:
            problems.append(f"{cid}: the label must be one of its aliases")
        if kind == "state" and c["aliases"] != [label]:
            problems.append(f"{cid}: a state concept has no aliases besides its label")
        for field in ("aliases", "conditional_aliases", "do_not_equate_with"):
            if len(set(c[field])) != len(c[field]):
                problems.append(f"{cid}: repeated entry in {field}")
        for phrase in (label, *c["aliases"], *c["conditional_aliases"], *c["do_not_equate_with"]):
            problems.extend(_phrase_problems(cid, phrase, kind))
        for phrase in c["aliases"]:
            if phrase in strong and strong[phrase] != cid:
                problems.append(f"{cid}: strong alias {phrase!r} is also used by {strong[phrase]}")
            strong[phrase] = cid
        if set(c["aliases"]) & set(c["conditional_aliases"]):
            problems.append(f"{cid}: a conditional alias repeats a strong alias")
        for phrase in c["aliases"]:
            if phrase == "sku" and label != "sku":
                problems.append(f"{cid}: 'sku' is ambiguous and may only be a conditional alias")
    # intent words are never entity words
    intent = {p for c in concepts if c.get("kind") == "query_intent" for p in c.get("aliases", [])}
    for c in concepts:
        if c.get("kind") != "query_intent" and intent & {*c.get("aliases", []), *c.get("conditional_aliases", [])}:
            problems.append(f"{c.get('concept_id')}: uses a query-intent word as an alias")
    # do-not-equate: real, symmetric, and never a word of both sides
    for c in concepts:
        cid = c.get("concept_id")
        for other_label in c.get("do_not_equate_with", []) if isinstance(c.get("do_not_equate_with"), list) else []:
            other = labels.get(other_label)
            if other is None:
                problems.append(f"{cid}: do_not_equate_with {other_label!r} is not a concept label")
            elif other is c:
                problems.append(f"{cid}: do_not_equate_with itself")
            elif c.get("label") not in other.get("do_not_equate_with", []):
                problems.append(f"{cid}: do_not_equate_with {other_label!r} is not symmetric")
            elif isinstance(other.get("aliases"), list):
                shared = {*c["aliases"], *c["conditional_aliases"]} & {*other["aliases"], *other["conditional_aliases"]}
                if shared:
                    problems.append(f"{cid}: shares {sorted(shared)} with {other_label!r}, which it must not equal")
    return problems


def _phrase_problems(cid: str, phrase: str, kind: str) -> list[str]:
    if not _PHRASE.match(phrase) or len(phrase) < _MIN_CHARS:
        return [f"{cid}: {phrase!r} is not a plain lower-case business phrase (no symbols, digits, underscores or single letters)"]
    words = set(re.split(r"[ -]", phrase))
    out: list[str] = []
    if words & AMBIGUOUS_ABBREVIATIONS:
        out.append(f"{cid}: {phrase!r} is an ambiguous database abbreviation")
    if words & CODE_AND_UI_WORDS:
        out.append(f"{cid}: {phrase!r} is a code or interface word")
    sql = words & SQL_WORDS
    if kind != "query_intent":
        sql |= words & _INTENT_ONLY_WORDS
    if sql:
        out.append(f"{cid}: {phrase!r} contains SQL or schema words {sorted(sql)}")
    return out


# ---------------------------------------------------------------------------------------------------- the glossary


@dataclass(frozen=True)
class Concept:
    concept_id: str
    label: str
    domain: str
    aliases: tuple[str, ...]  # strong, the label included
    conditional_aliases: tuple[str, ...]
    do_not_equate_with: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class Match:
    concept: Concept
    start: int
    end: int  # exclusive: words[start:end] is the matched phrase
    via: str  # "strong" | "conditional"


Supported = Callable[[Sequence[str]], bool]  # are all these (singular) words used by the connected database's own metadata?


class UniversalGlossary:
    def __init__(self, concepts: Iterable[Concept]) -> None:
        self.concepts: tuple[Concept, ...] = tuple(concepts)
        self.by_id = {c.concept_id: c for c in self.concepts}
        self.by_label = {c.label: c for c in self.concepts}
        self._strong: dict[tuple[str, ...], list[Concept]] = {}
        self._conditional: dict[tuple[str, ...], list[Concept]] = {}
        for c in self.concepts:
            for a in c.aliases:
                self._strong.setdefault(words_of(a), []).append(c)
            for a in c.conditional_aliases:
                self._conditional.setdefault(words_of(a), []).append(c)
        self._longest = max((len(k) for k in (*self._strong, *self._conditional)), default=1)

    def __len__(self) -> int:
        return len(self.concepts)

    def recognises(self, phrase: Sequence[str]) -> bool:
        """The phrase is a label or an alias (strong or conditional) of some concept."""
        key = tuple(phrase)
        return key in self._strong or key in self._conditional

    def match(self, words: Sequence[str], supported: Supported | None = None) -> list[Match]:
        """Concepts named in ``words``: longest phrase first, no word used twice. A strong alias always counts; a conditional
        alias only where ``supported`` says the connected database uses it. A phrase several concepts share is ambiguous: skipped."""
        taken = [False] * len(words)
        found: list[Match] = []
        for size in range(min(self._longest, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                if any(taken[start : start + size]):
                    continue
                key = tuple(words[start : start + size])
                owners, via = self._strong.get(key), "strong"
                if owners is None and supported is not None and key in self._conditional and supported(key):
                    owners, via = self._conditional[key], "conditional"
                if owners is None:
                    continue
                for i in range(start, start + size):
                    taken[i] = True  # the phrase is spoken for even when it is ambiguous, so a shorter word inside it cannot match
                if len(owners) == 1:
                    found.append(Match(owners[0], start, start + size, via))
        return sorted(found, key=lambda m: m.start)

    def expand(self, words: Sequence[str], supported: Supported | None = None, *, limit: int = MAX_EXPANSION) -> list[str]:
        """Extra words for a question: for each concept it names, the concept's label, the words this database supports and its other
        strong aliases, never a word of a do-not-equate concept, never what the question already says. Capped."""
        spoken = list(words)

        def said(phrase: tuple[str, ...]) -> bool:
            n = len(phrase)
            return any(tuple(spoken[i : i + n]) == phrase for i in range(len(spoken) - n + 1))

        out: list[str] = []
        for m in self.match(words, supported):
            c = m.concept
            if c.kind in NON_EXPANDING_KINDS:
                continue
            blocked = {words_of(p) for label in c.do_not_equate_with for p in self._phrases_of(label)}
            local = [a for a in c.conditional_aliases if supported is not None and supported(words_of(a))]
            added = 0
            for phrase in (c.label, *local, *c.aliases):
                key = words_of(phrase)
                if key == tuple(words[m.start : m.end]) or said(key) or key in blocked or phrase in out:
                    continue
                out.append(phrase)
                added += 1
                if added >= MAX_TARGETS_PER_CONCEPT or len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        return out[:limit]

    def _phrases_of(self, label: str) -> tuple[str, ...]:
        c = self.by_label.get(label)
        return (*c.aliases, *c.conditional_aliases) if c is not None else ()

    def are_separate(self, a: str, b: str) -> bool:
        """The two concepts (by id) are declared never to be equated."""
        ca, cb = self.by_id.get(a), self.by_id.get(b)
        return bool(ca and cb and (cb.label in ca.do_not_equate_with or ca.label in cb.do_not_equate_with))


def build(doc: dict[str, Any]) -> UniversalGlossary:
    return UniversalGlossary(
        Concept(c["concept_id"], c["label"], c["domain"], tuple(c["aliases"]), tuple(c["conditional_aliases"]), tuple(c["do_not_equate_with"]), c["kind"])
        for c in doc["concepts"]
    )


# ---------------------------------------------------------------------------------------------------- loading (cached until the file changes)

_CACHE: dict[str, tuple[tuple[int, int] | None, UniversalGlossary | None]] = {}


def clear_universal_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def load_universal(path: Path | None = None) -> UniversalGlossary | None:
    """The glossary, or None when the file is missing, unreadable or breaks the rules (never raises). Cached until it changes."""
    target = Path(path) if path is not None else PROJECT_ROOT / UNIVERSAL_REL
    key = str(target)
    try:
        st = target.stat()
        stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    glossary: UniversalGlossary | None = None
    if stamp is not None:
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
            problems = validate_document(doc)
            if problems:
                logger.warning("Universal ERP glossary not used: %d problem(s), first: %s", len(problems), problems[0])
            else:
                glossary = build(doc)
        except (OSError, ValueError) as exc:
            logger.warning("Universal ERP glossary unreadable: %s", type(exc).__name__)
    with _LOCK:
        _CACHE[key] = (stamp, glossary)
    return glossary


__all__ = [
    "AMBIGUOUS_ABBREVIATIONS",
    "KINDS",
    "NON_EXPANDING_KINDS",
    "UNIVERSAL_REL",
    "Concept",
    "Match",
    "UniversalGlossary",
    "build",
    "clear_universal_cache",
    "load_universal",
    "validate_document",
    "words_of",
]
