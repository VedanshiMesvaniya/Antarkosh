"""Routing hints and the synonym glossary, generated at sync time for any database (auto-schema Phase 9d).

Before this, ``semantics/routing_hints.json`` and ``semantics/glossary.json`` were empty seeds copied from the template and
nothing ever filled them for a database other than the hand-written ERP one. Now a sync fills them from what it already knows.

What is generated, and why it is safe to generate for any database:

* **Only gaps are filled.** A rule or glossary group is made only when the database's own table or column names contain exactly
  ONE word of a generic synonym group (``lexicon.py``): the other words of the group are then what people may say instead
  ("supplier" for a ``vendors`` table). The keywords of a rule never include a word that already appears in the schema's
  names, because retrieval finds those by name anyway. If two words of a group both appear in the names (``customers`` and
  ``clients``) the schema distinguishes them and the group is skipped.
* **Narrow, never broad.** A rule routes to at most four tables (anchors) or eight (fallback); a concept that matches more tables
  is too broad to route on and gets no rule. Test, backup and temp tables and tables outside the answering schema never appear.
  Table names are written the way the retrieval code looks them up (bare, lower case) and only when they can be found that way.
* **Never overwrites a person.** Rules carry ``"auto": "auto_routing_hints"``; a rule without that marker is never touched, and a
  generated keyword that a curated rule already handles is left to that rule. A glossary group a person edited or wrote is theirs:
  the groups this phase wrote are remembered in ``semantics/auto_hints_state.json`` with their exact words, and a group whose
  words no longer match (somebody edited it) or that was never recorded is treated as curated.
* **Follows the schema.** Every sync rebuilds the generated part, so a dropped table or a renamed column removes its rules.
  With ``auto_prompt_inject`` off the generated part is removed again, exactly like the generated column glossary.
* **Same files, same format.** The loaders and the router read these files exactly as before (the extra ``auto`` key on a rule
  is ignored by them). ``erp_main`` is never touched.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.relationships.model import CatalogGraph
from src.sql.relationships.names import singular, split_identifier
from src.sql.semantics.lexicon import SYNONYM_GROUPS

GENERATED_BY = "auto_routing_hints"
GLOSSARY_REL = "semantics/glossary.json"
ROUTING_REL = "semantics/routing_hints.json"
STATE_REL = "semantics/auto_hints_state.json"
SECTIONS = ("fallback_rules", "anchor_rules")

MIN_KEYWORD_CHARS = 4  # the router matches a keyword anywhere inside the question: a shorter one would fire inside other words
MAX_ANCHOR_TABLES = 4  # anchors are added to every matching question's schema; keep them few
MAX_FALLBACK_TABLES = 8  # more matching tables than this: the concept is too broad to route on
MAX_RULES = 60

_BARE_NAME = re.compile(r"^[a-z0-9_]+$")  # the only table names the retrieval code can look up
_WORD = re.compile(r"^[a-z]+$")


@dataclass
class Hints:
    glossary: dict[str, list[str]] = field(default_factory=dict)
    fallback_rules: list[dict[str, Any]] = field(default_factory=list)
    anchor_rules: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def _stems(name: str) -> set[str]:
    return {singular(t) for t in split_identifier(name)}


# ============================================================================================ build (pure)


def build_hints(graph: CatalogGraph, eligible: set[str]) -> Hints:
    """Glossary groups and routing rules for the ``eligible`` tables (qualified names). Pure: no file is read or written."""
    tables = [graph.tables[q] for q in sorted(eligible) if q in graph.tables]
    stats = {"tables": len(tables), "groups_present": 0, "groups_ambiguous": 0, "too_broad": 0, "no_table_named": 0}
    present: set[str] = set()
    by_name: dict[str, list[str]] = {}  # bare lower-case table name -> the name stems; a name two tables share is not routable
    seen_names: dict[str, int] = {}
    stems_of: dict[str, set[str]] = {}
    for t in tables:
        bare = t.name.lower()
        seen_names[bare] = seen_names.get(bare, 0) + 1
        stems_of[bare] = _stems(t.name)
        present |= stems_of[bare]
        for c in t.columns:
            present |= _stems(c.name)
    for bare, n in seen_names.items():
        if n == 1 and _BARE_NAME.match(bare):
            by_name[bare] = sorted(stems_of[bare])

    hints = Hints(stats=stats)
    for group in SYNONYM_GROUPS:
        found = [w for w in group if singular(w) in present]
        if not found:
            continue
        stats["groups_present"] += 1
        if len(found) > 1:  # the schema itself tells these words apart
            stats["groups_ambiguous"] += 1
            continue
        anchor = found[0]
        synonyms = [w for w in group if w != anchor]
        hints.glossary[anchor] = synonyms

        keywords = [w for w in synonyms if len(w) >= MIN_KEYWORD_CHARS and _WORD.match(w)]
        a = singular(anchor)
        matched = sorted((n for n, stems in by_name.items() if a in stems), key=lambda n: (len(split_identifier(n)), n))
        if not matched:
            stats["no_table_named"] += 1
            continue
        if not keywords:
            continue
        if len(matched) > MAX_FALLBACK_TABLES:
            stats["too_broad"] += 1
            continue
        if len(hints.fallback_rules) >= MAX_RULES:
            continue
        hints.fallback_rules.append({"keywords": list(keywords), "tables": matched[:MAX_FALLBACK_TABLES], "auto": GENERATED_BY})
        if len(matched) <= MAX_ANCHOR_TABLES:
            hints.anchor_rules.append({"keywords": list(keywords), "tables": list(matched), "auto": GENERATED_BY})
    stats["groups_written"] = len(hints.glossary)
    stats["fallback_rules"] = len(hints.fallback_rules)
    stats["anchor_rules"] = len(hints.anchor_rules)
    return hints


# ============================================================================================ files


def _folder(db_id: str, databases_dir: Path | None) -> Path:
    return (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id


def _read_object(path: Path) -> tuple[str, dict[str, Any]]:
    """``("missing" | "ok" | "invalid", object)``. Anything that is not a JSON object is "invalid" and never rewritten."""
    if not path.exists():
        return "missing", {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "invalid", {}
    return ("ok", doc) if isinstance(doc, dict) else ("invalid", {})


def _guard(db_id: str, databases_dir: Path | None) -> tuple[dict[str, Any] | None, Path | None]:
    out: dict[str, Any] = {"written": False, "unchanged": False, "skipped": None}
    validate_db_id(db_id)
    if db_id == DEFAULT_DB_ID:
        out["skipped"] = "pinned database: curated files are never replaced"
        return out, None
    folder = _folder(db_id, databases_dir)
    if not folder.is_dir():
        out["skipped"] = "database folder does not exist"
        return out, None
    return out, folder


def merge_synonym_glossary(db_id: str, groups: dict[str, list[str]], databases_dir: Path | None = None) -> dict[str, Any]:
    """Make the generated groups of ``glossary.json`` exactly ``groups``, touching nothing a person wrote.

    ``groups={}`` removes every generated group. Returns ``{"written", "unchanged", "skipped", "groups", "removed"}``.
    """
    out, folder = _guard(db_id, databases_dir)
    out.update({"groups": 0, "removed": 0})
    if folder is None:
        return out
    path, state_path = folder / GLOSSARY_REL, folder / STATE_REL
    status, existing = _read_object(path)
    if status == "invalid":
        out["skipped"] = "glossary.json is not a JSON object; left untouched"
        return out
    s_status, state = _read_object(state_path)
    owned_raw = state.get("glossary") if s_status == "ok" and state.get("generated_by") == GENERATED_BY else None
    owned: dict[str, Any] = owned_raw if isinstance(owned_raw, dict) else {}

    # a group is ours only while it is exactly what we wrote; anything else (a person's group, or one they edited) is theirs
    curated = {k: v for k, v in existing.items() if not (k in owned and v == owned[k])}
    added = {k: list(v) for k, v in sorted(groups.items()) if k not in curated}
    result = {**curated, **added}
    out["groups"] = len(added)
    out["removed"] = sum(1 for k in owned if k in existing and existing[k] == owned[k] and k not in added)

    if result == existing and (owned == added or (not owned and not added)):
        out["unchanged"] = True
        return out
    if status == "missing" and not added:
        out["unchanged"] = True
        return out
    if result != existing:
        registry.atomic_write_json(path, result, mode=0o644)
        out["written"] = True
    if owned != added:
        if added:
            registry.atomic_write_json(state_path, {"generated_by": GENERATED_BY, "glossary": added}, mode=0o644)
        elif state_path.exists():
            state_path.unlink()
    return out


def merge_routing_hints(
    db_id: str, fallback_rules: list[dict[str, Any]], anchor_rules: list[dict[str, Any]], databases_dir: Path | None = None
) -> dict[str, Any]:
    """Make the generated rules of ``routing_hints.json`` exactly the given ones, touching nothing a person wrote.

    Generated rules are the ones marked ``"auto": "auto_routing_hints"``. A keyword a curated rule of the same section already
    uses is dropped from the generated rule (the curated rule decides where it routes); other top-level keys are kept.
    """
    out, folder = _guard(db_id, databases_dir)
    out.update({"rules": 0, "removed": 0})
    if folder is None:
        return out
    path = folder / ROUTING_REL
    status, existing = _read_object(path)
    if status == "invalid":
        out["skipped"] = "routing_hints.json is not a JSON object; left untouched"
        return out

    result = dict(existing)
    wanted = {"fallback_rules": fallback_rules, "anchor_rules": anchor_rules}
    added_total = removed_total = 0
    for section in SECTIONS:
        current = existing.get(section)
        if current is not None and not isinstance(current, list):
            continue  # not the shape we know: leave the whole section alone
        rules = list(current or [])
        curated = [r for r in rules if not (isinstance(r, dict) and r.get("auto") == GENERATED_BY)]
        old_generated = [r for r in rules if isinstance(r, dict) and r.get("auto") == GENERATED_BY]
        taken = {
            str(k).lower()
            for r in curated
            if isinstance(r, dict)
            for k in (r.get("keywords") or [])
            if isinstance(k, str)
        }
        fresh: list[dict[str, Any]] = []
        for rule in wanted[section]:
            kws = [k for k in rule["keywords"] if k not in taken]
            if kws:
                fresh.append({**rule, "keywords": kws})
        added_total += len(fresh)
        removed_total += len([r for r in old_generated if r not in fresh])
        if fresh or current is not None:
            result[section] = curated + fresh
    out["rules"], out["removed"] = added_total, removed_total

    if result == existing:
        out["unchanged"] = True
        return out
    if status == "missing" and not added_total:
        out["unchanged"] = True
        return out
    registry.atomic_write_json(path, result, mode=0o644)
    out["written"] = True
    return out


__all__ = [
    "GENERATED_BY", "GLOSSARY_REL", "ROUTING_REL", "STATE_REL", "Hints", "build_hints", "merge_routing_hints", "merge_synonym_glossary",
]
