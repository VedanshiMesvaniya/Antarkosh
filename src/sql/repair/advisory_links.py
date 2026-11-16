"""Unverified link suggestions for a join that found nothing (used only by the Delta Repair prompt).

Why this exists: the relationship inference publishes an edge to ``relationships.json`` only at high confidence (0.70 and up).
A link seen only by column name (``sqts_cost_recovery.qn_number`` and ``sqts_main_lear.qn_number``) scores about 0.55-0.60, so it
is kept in ``inferred_relationships.json`` as "medium" and never reaches a prompt. When a question needs two tables that no
published path connects, the model then invents the join (a ticket number against a plant code) and the query returns no rows.

The published graph is deliberately NOT loosened (a wrong published join misleads every question). Instead, once a query has
already failed or come back empty AND the validator found tables with no defined relationship, the repair prompt is shown the
medium-confidence links that would connect them, directly or through one bridge table, labelled as unverified.

Safety rules:

* Only reached after a failure or an empty result of a query that joined disconnected tables; a query that returns rows is never
  touched and a published relationship is never changed.
* Off unless the database runs the Phase 12 prompt flags in apply mode (``auto_prompt_inject``); never for ``erp_main``.
* At most ``MAX_LINES`` lines; edges must be medium or high confidence, direction known, inside the answering scope and not
  ambiguous. Missing or unreadable files give an empty string and the repair prompt is exactly what it was.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_LINES = 6
MAX_PAIRS = 6
HEADER = (
    "Possible links between tables that have no declared relationship (inferred from column names, NOT verified; "
    "use them only where they fit the question, and prefer them over joining unrelated keys):"
)

_CACHE: dict[str, tuple[float, dict[str, list[tuple[str, str, str, float]]]]] = {}


def _load_adjacency(db_id: str, databases_dir: Path | None) -> dict[str, list[tuple[str, str, str, float]]]:
    """``table -> [(neighbour, own column, neighbour column, confidence)]`` from the medium/high inferred edges. Cached by mtime."""
    from src.sql import registry
    from src.sql.relationships.writer import GENERATED_BY, INFERRED_REL, MEDIUM_THRESHOLD

    base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
    path = base / db_id / INFERRED_REL
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = f"{path}"
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    adjacency: dict[str, list[tuple[str, str, str, float]]] = defaultdict(list)
    try:
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(doc, dict) and doc.get("generated_by") == GENERATED_BY:
        for e in doc.get("relationships") or []:
            if not isinstance(e, dict) or e.get("band") not in ("high", "medium"):
                continue
            if not e.get("direction_known") or e.get("ambiguous") or not e.get("in_answering_scope", True):
                continue
            try:
                conf = float(e.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            ft, fc, tt, tc = (str(e.get(k) or "") for k in ("from_table", "from_column", "to_table", "to_column"))
            if conf < MEDIUM_THRESHOLD or not (ft and fc and tt and tc) or ft.lower() == tt.lower():
                continue
            adjacency[ft.lower()].append((tt.lower(), fc, tc, conf))
            adjacency[tt.lower()].append((ft.lower(), tc, fc, conf))
    result = {k: sorted(v, key=lambda x: (-x[3], x[0], x[1])) for k, v in adjacency.items()}
    _CACHE[key] = (mtime, result)
    return result


def _best_path(adj: dict[str, list[tuple[str, str, str, float]]], a: str, b: str) -> list[tuple[str, str, str, str]] | None:
    """The link from ``a`` to ``b``: the strongest DIRECT one if any exists, else the strongest through exactly one bridge table.

    A direct link always wins, even a weaker one: every extra table in a join is one more place for a wrong key to hide.
    Steps are ``(t1, c1, t2, c2)``.
    """
    direct: tuple[float, list[tuple[str, str, str, str]]] | None = None
    bridged: tuple[float, list[tuple[str, str, str, str]]] | None = None
    for n1, c_a, c_n1, s1 in adj.get(a, ()):
        if n1 == b:
            if direct is None or s1 > direct[0]:
                direct = (s1, [(a, c_a, b, c_n1)])
            continue
        for n2, c_n1b, c_b, s2 in adj.get(n1, ()):
            if n2 != b or n1 == a:
                continue
            score = min(s1, s2)
            if bridged is None or score > bridged[0]:
                bridged = (score, [(a, c_a, n1, c_n1), (n1, c_n1b, b, c_b)])
    chosen = direct or bridged
    return chosen[1] if chosen else None


def build_advisory_links(
    db_id: str,
    disconnected_tables: Iterable[str],
    sql_tables: Iterable[str],
    *,
    max_lines: int = MAX_LINES,
    databases_dir: Path | None = None,
) -> str:
    """Suggested links for the repair prompt, or ``""`` when there is nothing safe to add. Never raises."""
    try:
        from src.sql.schema_retrieval import _route_bridge_mode

        if not db_id or db_id == "erp_main" or _route_bridge_mode(db_id) != "apply":
            return ""
        lone = [t.lower() for t in dict.fromkeys(str(t).lower() for t in disconnected_tables if t)]
        used = [t.lower() for t in dict.fromkeys(str(t).lower() for t in sql_tables if t)]
        if not lone or len(used) < 2:
            return ""
        adj = _load_adjacency(db_id, databases_dir)
        if not adj:
            return ""
        lines: list[str] = []
        pairs = 0
        for a in lone:
            for b in used:
                if b == a or (b in lone and used.index(b) < used.index(a)):
                    continue  # each unordered pair once
                pairs += 1
                if pairs > MAX_PAIRS:
                    break
                path = _best_path(adj, a, b)
                for t1, c1, t2, c2 in path or ():
                    line = f"- {t1}.{c1} = {t2}.{c2}"
                    if line not in lines and f"- {t2}.{c2} = {t1}.{c1}" not in lines and len(lines) < max_lines:
                        lines.append(line)
        return "\n".join([HEADER, *lines]) if lines else ""
    except Exception as exc:  # noqa: BLE001 - extra repair context only
        logger.debug("Advisory links skipped: %s", exc)
        return ""


def clear_cache() -> None:
    _CACHE.clear()


__all__ = ["HEADER", "build_advisory_links", "clear_cache"]
