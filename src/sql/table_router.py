"""Database-agnostic table routing and keyword map matching."""

from __future__ import annotations

import logging
from typing import Any

from src.sql.knowledge.loaders import DEFAULT_DB_ID, load_knowledge_json

logger = logging.getLogger(__name__)

_ROUTING_HINTS_CACHE: dict[str, dict[str, Any]] = {}


def load_routing_hints(db_id: str = DEFAULT_DB_ID) -> dict[str, Any]:
    """Load semantic routing hints for a specific database."""
    norm_id = (db_id or DEFAULT_DB_ID).strip()
    if norm_id in _ROUTING_HINTS_CACHE:
        return _ROUTING_HINTS_CACHE[norm_id]

    try:
        hints = load_knowledge_json("routing_hints", db_id=norm_id) or {}
    except Exception:  # noqa: BLE001
        hints = {}
    _ROUTING_HINTS_CACHE[norm_id] = hints
    return hints


def _clear_routing_hints_cache(db_id: str | None = None) -> None:
    """Clear cached routing hints globally or for a specific db_id."""
    if db_id is None:
        _ROUTING_HINTS_CACHE.clear()
    else:
        _ROUTING_HINTS_CACHE.pop(db_id, None)


load_routing_hints.cache_clear = _clear_routing_hints_cache  # type: ignore[attr-defined]


def route_tables_for_query(
    query: str,
    db_id: str = DEFAULT_DB_ID,
    section: str = "fallback_rules",
) -> list[str]:
    """Match query keywords against semantic routing rules and return candidate tables."""
    hints = load_routing_hints(db_id)
    rules: list[dict[str, Any]] = hints.get(section, [])
    if not isinstance(rules, list):
        return []

    query_lower = query.lower()
    matched_tables: list[str] = []

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        keywords: list[str] = rule.get("keywords", [])
        tables: list[str] = rule.get("tables", [])
        conditional: dict[str, Any] | None = rule.get("conditional")

        if any(k in query_lower for k in keywords):
            for table in tables:
                if table not in matched_tables:
                    matched_tables.append(table)

            if isinstance(conditional, dict):
                cond_keywords: list[str] = conditional.get("keywords", [])
                cond_tables: list[str] = conditional.get("tables", [])
                if any(ck in query_lower for ck in cond_keywords):
                    for ct in cond_tables:
                        if ct not in matched_tables:
                            matched_tables.append(ct)

    return matched_tables


def route_anchor_tables(
    query: str,
    db_id: str = DEFAULT_DB_ID,
) -> set[str]:
    """Match query keywords against anchor routing rules and return candidate table set."""
    matched = route_tables_for_query(query, db_id=db_id, section="anchor_rules")
    return set(matched)


__all__ = [
    "_ROUTING_HINTS_CACHE",
    "_clear_routing_hints_cache",
    "load_routing_hints",
    "route_anchor_tables",
    "route_tables_for_query",
]
