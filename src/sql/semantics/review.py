"""``semantics/semantic_review.json``: what the semantic layer is NOT sure about, for a person to look at whenever they like.

The database works without this file and without anyone reading it. It only lists:

* ``uncertain_mappings``: column meanings of medium or low confidence, or flagged as inconsistent across tables. None of them reaches
  a prompt (only high-confidence meanings become glossary entries).
* ``needs_stronger_evidence``: concept readings that were not made high because the name is the only evidence (no comment, no
  procedure/view metadata, no key structure), or the card was medium.
* ``ambiguous_aliases``: words that name too many columns, concepts spread over too many tables, and columns that name two concepts the
  universal glossary says must never be equated.
* ``code_values``: stored codes that procedures compare a column with. A code's meaning needs two independent metadata signals (for
  example a procedure's CASE plus a column comment, or plus a lookup-table foreign key); with fewer the status is ``unknown`` and the
  code is never used. A code is never read from its character alone.

Safety: metadata only; this file is read by nobody at question time, so it cannot change an answer. Same rules as every generated
file: ``erp_main`` is never touched, a file this phase did not write is never replaced, the write is atomic and skipped when the
content is unchanged, and every section is capped.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id
from src.sql.relationships.model import CatalogGraph
from src.sql.semantics.concepts import MAX_REVIEW_PER_SECTION, ConceptLayer
from src.sql.semantics.evidence import TableEvidence
from src.sql.semantics.wiring import MAX_OWNERS_PER_PHRASE

GENERATED_BY = "auto_semantic_review"
REVIEW_REL = "semantics/semantic_review.json"
VERSION = 1
MAX_OBSERVED = 3
SIGNAL_KINDS = frozenset({"case", "comment", "fk", "param", "alias"})  # independent kinds of metadata that can explain a stored code


def _kind(item_id: str) -> str:
    return re.sub(r"\d+$", "", item_id)


def build_review(
    doc: dict[str, Any],
    graph: CatalogGraph,
    evidence: dict[str, TableEvidence],
    layer: ConceptLayer | None,
) -> dict[str, Any]:
    """The review document's sections. Pure."""
    uncertain: list[dict[str, Any]] = []
    owners: dict[str, set[tuple[str, str]]] = {}
    for table_name, entry in sorted((doc.get("tables") or {}).items()):
        if table_name not in graph.tables or not isinstance(entry, dict):
            continue
        for col, card in sorted((entry.get("columns") or {}).items()):
            if not isinstance(card, dict):
                continue
            level = card.get("confidence")
            flags = [f for f in card.get("flags") or [] if isinstance(f, str)]
            if level == "high" and not flags:
                for p in card.get("phrases") or []:
                    owners.setdefault(str(p), set()).add((table_name, col))
                continue
            uncertain.append({
                "table": table_name, "column": col, "full_name": str(card.get("full_name") or ""), "confidence": level, "flags": flags,
                "reason": "; ".join(flags) if flags else ("low confidence" if level == "low" else "medium confidence"),
            })
    ambiguous: list[dict[str, Any]] = [
        {"kind": "alias_names_many_columns", "alias": p, "columns": len(cols)}
        for p, cols in sorted(owners.items()) if len(cols) > MAX_OWNERS_PER_PHRASE
    ]
    needs: list[dict[str, Any]] = []
    if layer is not None:
        needs = list(layer.needs_stronger_evidence)
        ambiguous += layer.ambiguous
    codes = _code_values(graph, evidence)

    def cap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return rows[:MAX_REVIEW_PER_SECTION]

    return {
        "summary": {
            "uncertain_mappings": len(uncertain), "needs_stronger_evidence": len(needs), "ambiguous_aliases": len(ambiguous),
            "code_values": len(codes), "code_values_unknown": sum(1 for c in codes if c["status"] == "unknown"),
            "shown_per_section_at_most": MAX_REVIEW_PER_SECTION,
        },
        "uncertain_mappings": cap(uncertain),
        "needs_stronger_evidence": cap(needs),
        "ambiguous_aliases": cap(ambiguous),
        "code_values": cap(codes),
    }


def _code_values(graph: CatalogGraph, evidence: dict[str, TableEvidence]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for q, te in sorted(evidence.items()):
        if q not in graph.tables:
            continue
        for ce in te.columns:
            cases = [i for i in ce.items if i.kind == "case"]
            if not cases:
                continue
            signals = {i.kind for i in ce.items if i.kind in SIGNAL_KINDS}
            if ce.is_foreign_key:
                signals.add("fk")  # a lookup table behind the code
            status = "supported" if len(signals) >= 2 else "unknown"
            out.append({
                "table": q, "column": ce.name, "status": status, "signals": sorted(signals),
                "observed": [c.text for c in cases][:MAX_OBSERVED],
                "reason": "two independent metadata signals" if status == "supported" else "fewer than two independent signals: the code is not used",
            })
    return out


# ---------------------------------------------------------------------------------------------------- the file


def _content_key(review: dict[str, Any]) -> str:
    return json.dumps(review, sort_keys=True)


def write_review(db_id: str, review: dict[str, Any], databases_dir: Path | None = None) -> dict[str, Any]:
    """Returns ``{"written": bool, "unchanged": bool, "skipped": str | None}`` plus the summary counts."""
    validate_db_id(db_id)
    out: dict[str, Any] = {"written": False, "unchanged": False, "skipped": None, "summary": review.get("summary", {})}
    if db_id == DEFAULT_DB_ID:
        out["skipped"] = "pinned database: curated files are never replaced"
        return out
    folder = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    if not folder.is_dir():
        out["skipped"] = "database folder does not exist"
        return out
    path = folder / REVIEW_REL
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if not (isinstance(current, dict) and current.get("generated_by") == GENERATED_BY):
            out["skipped"] = "a file not generated by auto_semantic_review is present; left untouched"
            return out
        if current.get("review") == review:
            out["unchanged"] = True
            return out
    doc = {
        "generated_by": GENERATED_BY,
        "version": VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "For a person to review whenever they like. Nothing here is read at question time; the database works without it.",
        "review": review,
    }
    registry.atomic_write_json(path, doc, mode=0o644)
    out["written"] = True
    return out


__all__ = ["GENERATED_BY", "REVIEW_REL", "build_review", "write_review"]
