"""Query-term normalization and a column-level index for schema retrieval (metadata-driven SQL accuracy, Release 1).

Schema retrieval finds tables by vector similarity over one chunk per table. Two gaps are closed here, both from metadata only
(names, types, comments and the generated meanings of ONE database; never a row, a sample value or a query result):

1. **Query terms.** A question that says ``item`` or ``facility`` finds nothing literally called that. ``expand_query_terms`` returns
   a few EXTRA words (``product``, ``material``, ``plant``) taken from a small generic alias list (``lexicon.QUERY_ALIASES``) and
   from the database's own ``semantics/glossary.json``. They are appended to the text the question is searched with; the user's
   words are never replaced and nothing here is shown to the model.
2. **Column records.** ``find_column_tables`` keeps, per database, one small record per column (name words, catalog comment,
   generated meaning and phrases, glossary synonyms) and returns the few tables owning a column whose words the question uses.
   A word that many tables use says nothing about where to look, so a word counts for ``1 / (tables that use it)`` and words used
   by more than ``MAX_TOKEN_TABLES`` tables are ignored. This is a local keyword index, not extra vectors: it adds no points to the
   vector store, no embedding cost and no new chunk type that document search would have to learn to hide.

Safety rules shared by both:

* Everything is read from ``databases/<db_id>/`` for the one ``db_id`` asked about, so nothing can come from another database.
* Only tables the catalog says are real, in the answering schema, not views, not look-alike copies and not test/backup/temp tables,
  and whose bare name is unique, can be returned. The caller also restricts them to tables it can show (``available``).
* A missing, empty or unreadable file means "no help": the result is empty and retrieval behaves exactly as before.
* The pinned database (``erp_main``) always gets nothing. Never raises.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.paths.links import select_nodes
from src.sql.profiles.writer import PROFILES_REL
from src.sql.relationships.model import EVIDENCE_FILE, INVENTORY_FILE, SCHEMA_FILE, load_graph
from src.sql.relationships.names import singular, split_identifier
from src.sql.semantics.dictionary import DICTIONARY_REL, GENERATED_BY
from src.sql.semantics.lexicon import QUERY_ALIASES
from src.sql.semantics.routing import GLOSSARY_REL as SYNONYM_GLOSSARY_REL
from src.sql.semantics.universal import load_universal
from src.sql.semantics.wiring import GENERIC_PHRASES
from src.sql.semantics.wiring import GLOSSARY_REL as COLUMN_GLOSSARY_REL

logger = logging.getLogger(__name__)

MAX_EXTRA_TERMS = 8  # words added to one question's search text
MAX_TOKEN_TABLES = 6  # a word used by more tables than this does not say where to look
MIN_SCORE = 0.3  # one word used by at most three tables, or several words used by a few more
MAX_COLUMN_TABLES = 3  # tables one question may add; the token budget still has the last word

# Words that say nothing about WHERE the data is: question verbs and filler, plus the generic column words the meaning phase
# already refuses to use as a phrase on their own.
_STOP = frozenset(GENERIC_PHRASES) | {
    "a", "an", "the", "is", "are", "was", "were", "of", "for", "in", "on", "at", "to", "by", "with", "and", "or", "our", "my", "your",
    "who", "what", "which", "show", "get", "give", "list", "find", "how", "much", "many", "this", "that", "from", "tell", "each", "per",
    "all", "any", "every", "top", "last", "first", "most", "least", "highest", "lowest", "average", "avg", "sum", "count", "between",
    "than", "where", "when", "whose", "their", "have", "has", "had", "not", "also", "only", "over", "under", "more", "less", "new",
    "old", "wise",
}
_BARE_NAME = re.compile(r"^[a-z0-9_]+$")  # the only table names the retrieval code can look up
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------------- small helpers


def _words(text: Any) -> list[str]:
    """Lower-case, singular words of free text or an identifier (``PartyID`` -> party, id)."""
    return [singular(t) for t in split_identifier(text if isinstance(text, str) else "")]


def _useful(text: Any) -> set[str]:
    return {w for w in _words(text) if len(w) >= 3 and w not in _STOP}


def _contains(tokens: list[str], phrase: list[str]) -> bool:
    """``phrase`` occurs in ``tokens`` as a run of neighbouring words."""
    n = len(phrase)
    return n > 0 and any(tokens[i : i + n] == phrase for i in range(len(tokens) - n + 1))


def _folder(db_id: str) -> Path:
    return registry.DATABASES_DIR / db_id


def _stamp(db_id: str, rels: tuple[str, ...]) -> tuple[Any, ...]:
    """Size and modification time of each file: when they are the same, what was built from them is still right."""
    out: list[Any] = []
    for rel in rels:
        try:
            st = (_folder(db_id) / rel).stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _read_json(db_id: str, rel: str) -> Any:
    try:
        return json.loads((_folder(db_id) / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_retrieval_terms_cache(db_id: str | None = None) -> None:
    with _LOCK:
        if db_id is None:
            _GROUPS_CACHE.clear()
            _INDEX_CACHE.clear()
        else:
            _GROUPS_CACHE.pop(db_id, None)
            _INDEX_CACHE.pop(db_id, None)


def _usable_db(db_id: str) -> bool:
    try:
        validate_db_id(db_id)
    except Exception:  # noqa: BLE001 - an invalid id simply has no files
        return False
    return db_id != DEFAULT_DB_ID  # pinned database: curated files only


# ---------------------------------------------------------------------------------------------------- 1. query terms

_GROUPS_CACHE: dict[str, tuple[tuple[Any, ...], list[tuple[list[str], list[list[str]]]]]] = {}


def _glossary_groups(db_id: str) -> list[tuple[list[str], list[list[str]]]]:
    """``semantics/glossary.json`` as ``[(concept words, [synonym phrase words, ...]), ...]``; values may be a list or a comma text."""
    stamp = _stamp(db_id, (SYNONYM_GLOSSARY_REL,))
    with _LOCK:
        cached = _GROUPS_CACHE.get(db_id)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    groups: list[tuple[list[str], list[list[str]]]] = []
    doc = _read_json(db_id, SYNONYM_GLOSSARY_REL)
    if isinstance(doc, dict):
        for concept, syns in doc.items():
            if isinstance(syns, str):
                syns = syns.split(",")
            if not isinstance(concept, str) or not isinstance(syns, list):
                continue
            concept_words = _words(concept)
            phrases = [w for w in (_words(s) for s in syns if isinstance(s, str)) if w]
            if concept_words and phrases:
                groups.append((concept_words, phrases))
    with _LOCK:
        _GROUPS_CACHE[db_id] = (stamp, groups)
    return groups


def expand_query_terms(query: str, db_id: str) -> list[str]:
    """Extra words for this question: what a word of it may stand for, per this database's glossary, the universal ERP glossary and
    the short generic alias list for words the universal glossary does not know.

    The universal glossary widens only: strong aliases always, conditional aliases only when this database's own table, column or
    comment words use them, never a word of a concept it must not equate with. Only words that are not already in the question
    are returned, at most ``MAX_EXTRA_TERMS``. Empty when nothing applies.
    """
    try:
        if not isinstance(query, str) or not query.strip() or not _usable_db(db_id):
            return []
        q = _words(query)
        extra: list[str] = []

        def add(words: list[str]) -> None:
            if words and not _contains(q, words) and " ".join(words) not in extra:
                extra.append(" ".join(words))

        universal = load_universal()
        for phrase, stands_for in QUERY_ALIASES:
            if universal is not None and universal.recognises(tuple(_words(phrase))):
                continue  # the universal ERP glossary knows this word and decides what it may stand for (conditional words need local support)
            if _contains(q, _words(phrase)):
                for term in stands_for:
                    add(_words(term))
        for concept_words, phrases in _glossary_groups(db_id):
            if any(_contains(q, p) for p in phrases):
                add(concept_words)
        if universal is not None:
            vocabulary = _vocabulary(db_id)
            supported = lambda ws: bool(ws) and all(w in vocabulary for w in ws)
            for phrase in universal.expand(q, supported):
                add(_words(phrase))
        return extra[:MAX_EXTRA_TERMS]
    except Exception as exc:  # noqa: BLE001 - optional help must never break retrieval
        logger.debug("Query terms unavailable for %s: %s", db_id, exc)
        return []


# ---------------------------------------------------------------------------------------------------- 2. column records


@dataclass(frozen=True)
class ColumnIndex:
    columns: tuple[tuple[str, str, frozenset[str]], ...]  # (bare table, column, useful words)
    owners: dict[str, int]  # word -> number of tables that have a column using it
    vocabulary: frozenset[str] = frozenset()  # every word of the real tables' own table, column and comment names


@dataclass(frozen=True)
class ColumnHit:
    table: str  # bare, lower case: how retrieval looks tables up
    column: str
    score: float
    words: tuple[str, ...]


_INDEX_CACHE: dict[str, tuple[tuple[Any, ...], ColumnIndex | None]] = {}
_INDEX_FILES = (SCHEMA_FILE, INVENTORY_FILE, EVIDENCE_FILE, PROFILES_REL, DICTIONARY_REL, COLUMN_GLOSSARY_REL)


def _meaning_words(db_id: str) -> dict[tuple[str, str], set[str]]:
    """``(table lower, column lower) -> words`` from the generated meaning cards and the column glossary, when they exist."""
    out: dict[tuple[str, str], set[str]] = {}
    doc = _read_json(db_id, DICTIONARY_REL)
    if isinstance(doc, dict) and doc.get("generated_by") == GENERATED_BY and isinstance(doc.get("tables"), dict):
        for table, entry in doc["tables"].items():
            cols = entry.get("columns") if isinstance(entry, dict) else None
            if not isinstance(cols, dict):
                continue
            for col, card in cols.items():
                if not isinstance(card, dict):
                    continue
                words = _useful(card.get("full_name"))
                for phrase in card.get("phrases") or []:
                    words |= _useful(phrase)
                if words:
                    out.setdefault((str(table).lower(), str(col).lower()), set()).update(words)
    glossary = _read_json(db_id, COLUMN_GLOSSARY_REL)
    if isinstance(glossary, dict):
        for term, entry in glossary.items():
            target = entry.get("maps_to") if isinstance(entry, dict) else None
            parts = target.split(".") if isinstance(target, str) else []
            if len(parts) < 2:
                continue
            words = _useful(term)
            for syn in entry.get("synonyms") or []:
                words |= _useful(syn)
            if words:
                out.setdefault((parts[-2].lower(), parts[-1].lower()), set()).update(words)
    return out


def _build_index(db_id: str) -> ColumnIndex | None:
    graph = load_graph(db_id)
    if graph is None:
        return None
    profiles = _read_json(db_id, PROFILES_REL)
    nodes = select_nodes(graph, profiles if isinstance(profiles, dict) else None)
    eligible = set(nodes.nodes) - set(nodes.blocked_as_bridge)  # real tables only: no views, copies, test/backup/temp tables
    collisions = graph.name_collisions()
    meanings = _meaning_words(db_id)
    columns: list[tuple[str, str, frozenset[str]]] = []
    owners: dict[str, set[str]] = {}
    vocabulary: set[str] = set()
    for qualified in sorted(eligible):
        t = graph.tables[qualified]
        bare = t.name.lower()
        if not _BARE_NAME.match(bare) or bare in collisions:  # a name two tables share cannot be looked up
            continue
        vocabulary.update(w for w in _words(t.name) if len(w) >= 3)
        for c in t.columns:
            vocabulary.update(w for w in (*_words(c.name), *_words(c.comment)) if len(w) >= 3)
            words = _useful(c.name) | _useful(c.comment)
            words |= meanings.get((qualified.lower(), c.name.lower()), set()) | meanings.get((bare, c.name.lower()), set())
            if not words:
                continue
            columns.append((bare, c.name, frozenset(words)))
            for w in words:
                owners.setdefault(w, set()).add(bare)
    if not columns:
        return None
    return ColumnIndex(tuple(columns), {w: len(tables) for w, tables in owners.items()}, frozenset(vocabulary))


def load_column_index(db_id: str) -> ColumnIndex | None:
    """The column index for ``db_id``, rebuilt only when one of its files changed. None when there is no catalog to build it from."""
    try:
        if not _usable_db(db_id):
            return None
        stamp = _stamp(db_id, _INDEX_FILES)
        with _LOCK:
            cached = _INDEX_CACHE.get(db_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        index = _build_index(db_id)
        with _LOCK:
            _INDEX_CACHE[db_id] = (stamp, index)
        return index
    except Exception as exc:  # noqa: BLE001 - an unreadable file must never break a question
        logger.warning("Column index for %s unavailable: %s: %s", db_id, type(exc).__name__, str(exc)[:200])
        return None


def _vocabulary(db_id: str) -> frozenset[str]:
    """The words this database's own real tables, columns and comments use: what makes a conditional alias count. Empty without a catalog."""
    index = load_column_index(db_id)
    return index.vocabulary if index is not None else frozenset()


def find_column_tables(
    query: str,
    extra_terms: list[str],
    db_id: str,
    *,
    available: set[str],
    exclude: set[str] | frozenset[str] = frozenset(),
    limit: int = MAX_COLUMN_TABLES,
    index: ColumnIndex | None = None,
) -> list[ColumnHit]:
    """Tables (best first, at most ``limit``) with a column whose words the question, or its extra terms, use.

    ``available`` are the tables the caller can show; ``exclude`` are tables it already has. Never raises.
    """
    try:
        idx = index if index is not None else load_column_index(db_id)
        if idx is None or limit <= 0 or not available:
            return []
        asked = _useful(query)
        for term in extra_terms:
            asked |= _useful(term)
        asked = {w for w in asked if 0 < idx.owners.get(w, 0) <= MAX_TOKEN_TABLES}
        if not asked:
            return []
        best: dict[str, ColumnHit] = {}
        matches: dict[str, int] = {}
        for table, column, words in idx.columns:
            if table in exclude or table not in available:
                continue
            hit = sorted(words & asked)
            if not hit:
                continue
            score = sum(1.0 / idx.owners[w] for w in hit)
            matches[table] = matches.get(table, 0) + 1
            if score >= MIN_SCORE and (table not in best or score > best[table].score):
                best[table] = ColumnHit(table, column, round(score, 4), tuple(hit))
        ranked = sorted(best.values(), key=lambda h: (-h.score, -matches[h.table], h.table))
        return ranked[:limit]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Column tables unavailable for %s: %s", db_id, exc)
        return []


__all__ = [
    "ColumnHit",
    "ColumnIndex",
    "clear_retrieval_terms_cache",
    "expand_query_terms",
    "find_column_tables",
    "load_column_index",
]
