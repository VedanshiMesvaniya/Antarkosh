"""Wiring meanings into retrieval (auto-schema Phase 9c): the text that is embedded, and the glossary entries.

Two channels, deliberately different:

1. **Retrieval (embedding text).** A table's embedded text gets a short block of its column meanings, so the question
   "purchase order number" finds the table whose column is ``po_no``. ``high`` and ``medium`` meanings are used. The block
   is added only to the text that is EMBEDDED; the stored chunk content (what the token budget counts and what prompts are
   built from) is not changed in any way.
2. **Prompts (glossary).** Only ``high`` confidence meanings become entries of ``semantics/column_glossary.json``, the file
   the prompt builder already matches against the question. Each entry maps the meaning to the REAL column
   (``"purchase order number" -> po_head.po_no``), so the model sees the meaning next to the column name, never instead of it.

Glossary safety: an entry this phase writes carries ``"auto": "auto_column_meaning"``. Entries without that mark (curated)
are never changed, never reordered and never replaced; an auto entry whose key is already used by a curated one is skipped;
auto entries that are no longer justified are removed. The file is rewritten atomically and only if its content changes.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.relationships.model import CatalogGraph
from src.sql.relationships.names import learn_common_columns, normalized_column_name
from src.sql.semantics.dictionary import GENERATED_BY, read_dictionary

GLOSSARY_REL = "semantics/column_glossary.json"
EMBED_EXTRA_MAX_CHARS = 1500
MAX_PHRASES_IN_EMBEDDING = 3
MAX_GLOSSARY_ENTRIES = 1200
MAX_NOTE_CHARS = 120
MAX_OWNERS_PER_PHRASE = 2  # a phrase that names more columns than this is ambiguous and is not used
GENERIC_PHRASES = frozenset(
    ["name", "number", "date", "status", "type", "code", "id", "amount", "value", "quantity", "description", "total", "time", "day", "month", "year"]
)
_LEVEL = {"high": 2, "medium": 1, "low": 0}


# ---------------------------------------------------------------- 1. the text that is embedded


def _embedding_block(columns: dict[str, dict[str, Any]]) -> str:
    ranked = sorted(
        ((c, e) for c, e in columns.items() if _LEVEL.get(e.get("confidence"), 0) >= 1 and e.get("full_name")),
        key=lambda ce: (-_LEVEL[ce[1]["confidence"]], ce[0].lower()),
    )
    lines = ["-- Column meanings:"]
    size = len(lines[0])
    for name, e in ranked:
        phrases = [p for p in e.get("phrases") or [] if p != e["full_name"]][:MAX_PHRASES_IN_EMBEDDING]
        line = f"-- {name}: {e['full_name']}" + (f" (also: {', '.join(phrases)})" if phrases else "")
        if size + len(line) + 1 > EMBED_EXTRA_MAX_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


def embedding_extras_from(doc: dict[str, Any]) -> dict[str, str]:
    """Table (lower-case, qualified and bare when unambiguous) -> the block appended to its embedded text."""
    out: dict[str, str] = {}
    bare: dict[str, list[str]] = {}
    for table, entry in (doc.get("tables") or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("columns"), dict):
            continue
        block = _embedding_block(entry["columns"])
        if block:
            out[table.lower()] = block
            bare.setdefault(table.lower().rsplit(".", 1)[-1], []).append(table.lower())
    for name, owners in bare.items():
        if len(owners) == 1 and name not in out:
            out[name] = out[owners[0]]
    return out


def load_embedding_extras(db_id: str, databases_dir: Path | None = None) -> dict[str, str]:
    """The extras for ``db_id`` from its dictionary file. Empty for erp_main, a missing, foreign or unreadable file."""
    try:
        validate_db_id(db_id)
        if db_id == DEFAULT_DB_ID:
            return {}
        state, doc = read_dictionary(db_id, databases_dir)
        return embedding_extras_from(doc) if state == "ours" else {}
    except Exception:  # noqa: BLE001 - retrieval must never fail because of this optional text
        return {}


def changed_tables(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Tables whose embedded text differs between two extras maps (qualified names only, sorted)."""
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


# ---------------------------------------------------------------- 2. the glossary


def build_glossary_entries(doc: dict[str, Any], graph: CatalogGraph) -> dict[str, dict[str, Any]]:
    """Glossary entries for the ``high`` confidence meanings of columns that are not generic. Pure."""
    common = learn_common_columns(graph.tables.values())
    cards: list[tuple[str, str, dict[str, Any]]] = []
    for table, entry in sorted((doc.get("tables") or {}).items()):
        t = graph.tables.get(table)
        if t is None or not isinstance(entry, dict):
            continue
        for col, e in sorted((entry.get("columns") or {}).items()):
            c = t.column(col)
            if c is None or e.get("confidence") != "high" or not e.get("full_name"):
                continue
            if normalized_column_name(c.name) in common:
                continue
            cards.append((table, c.name, e))

    owners: dict[str, set[tuple[str, str]]] = {}
    for table, col, e in cards:
        for p in e.get("phrases") or []:
            owners.setdefault(p, set()).add((table, col))

    def usable(p: str) -> bool:
        words = p.split()
        return len(owners.get(p, ())) <= MAX_OWNERS_PER_PHRASE and not (len(words) == 1 and words[0] in GENERIC_PHRASES)

    # meanings backed by something somebody wrote down come first, so the cap never drops them for a name-only reading
    cards.sort(key=lambda c: (0 if any(i != "abbrev" for i in c[2].get("evidence", [])) else 1, c[0], c[1]))
    entries: dict[str, dict[str, Any]] = {}
    used_terms: set[str] = set()
    for table, col, e in cards[:MAX_GLOSSARY_ENTRIES]:
        term = e["full_name"]
        if term in used_terms:
            term = f"{term} ({t_bare(table)})"
        if term in used_terms:
            continue
        used_terms.add(term)
        syn = [p for p in e.get("phrases") or [] if usable(p) and p != term]
        entries[term] = {
            "maps_to": f"{table}.{col}",
            "note": (e.get("meaning") or "")[:MAX_NOTE_CHARS],
            "synonyms": syn,
            "auto": GENERATED_BY,
        }
    return entries


def t_bare(table: str) -> str:
    return table.rsplit(".", 1)[-1]


def merge_glossary(db_id: str, entries: dict[str, dict[str, Any]], databases_dir: Path | None = None) -> dict[str, Any]:
    """Make the auto entries of ``column_glossary.json`` exactly ``entries``, touching nothing else.

    ``entries={}`` removes every auto entry (for example when prompt injection is switched off).
    Returns ``{"written": bool, "unchanged": bool, "skipped": str | None, "entries": n, "removed": n}``."""
    validate_db_id(db_id)
    out: dict[str, Any] = {"written": False, "unchanged": False, "skipped": None, "entries": 0, "removed": 0}
    if db_id == DEFAULT_DB_ID:
        out["skipped"] = "pinned database: curated files are never replaced"
        return out
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        out["skipped"] = "database folder does not exist"
        return out
    path = folder / GLOSSARY_REL
    existing: Any = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if not isinstance(existing, dict):
            out["skipped"] = "column_glossary.json is not a JSON object; left untouched"
            return out
    curated = {k: v for k, v in existing.items() if not (isinstance(v, dict) and v.get("auto") == GENERATED_BY)}
    added = {k: v for k, v in entries.items() if k not in curated}
    result = {**curated, **added}
    out["entries"] = len(added)
    out["removed"] = sum(1 for k, v in existing.items() if isinstance(v, dict) and v.get("auto") == GENERATED_BY and k not in added)
    if result == existing:
        out["unchanged"] = True
        return out
    if not path.exists() and not added:
        out["unchanged"] = True
        return out
    registry.atomic_write_json(path, result, mode=0o644)
    out["written"] = True
    return out



__all__ = [
    "EMBED_EXTRA_MAX_CHARS", "GLOSSARY_REL", "MAX_GLOSSARY_ENTRIES", "build_glossary_entries", "changed_tables",
    "embedding_extras_from", "load_embedding_extras", "merge_glossary",
]
