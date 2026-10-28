"""Known join conditions for the Delta Repair prompt.

The repair prompt is about 350 tokens: the error, the failed SQL and the columns of the tables the SQL uses. It carried no
join information, so when a join was the problem (a missing table, a column that does not exist, a type clash such as joining a
varchar plant code to an integer number) the repair model could only guess: it replaced the table with a look-alike and
invented the join columns.

This builds a short block of join conditions the pipeline already knows - declared and inferred relationships plus the Phase 8
multi-hop routes - restricted to the tables the failed SQL uses and the tables the repair is shown:

    Known joins (use only these ON conditions):
    - a.col = b.col

Safety rules:

* Off unless the database runs the Phase 12 prompt flags in apply mode (``auto_prompt_inject``); never for ``erp_main``.
* Only conditions between tables of this question; at most ``MAX_LINES`` lines, so the block stays small.
* Missing or unreadable knowledge files give an empty string, and the repair prompt is then exactly what it was.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from itertools import combinations

logger = logging.getLogger(__name__)

MAX_LINES = 8
MAX_FOCUS_TABLES = 12  # tables considered: the ones the SQL uses first, then the ones the repair is shown
HEADER = "Known joins (use only these ON conditions):"


def _edge_line(left: str, right: str) -> str:
    return f"- {left} = {right}"


def build_repair_join_context(
    db_id: str,
    sql_tables: Iterable[str],
    shown_tables: Iterable[str] = (),
    *,
    max_lines: int = MAX_LINES,
) -> str:
    """Join conditions for ``sql_tables`` (and the extra ``shown_tables``), or ``""`` when there is nothing to add."""
    try:
        from src.sql.schema_retrieval import _route_bridge_mode

        if not db_id or _route_bridge_mode(db_id) != "apply":
            return ""

        used = [t.lower() for t in dict.fromkeys(str(t).lower() for t in sql_tables if t)]
        extra = [t.lower() for t in dict.fromkeys(str(t).lower() for t in shown_tables if t) if t.lower() not in used]
        focus = (used + extra)[:MAX_FOCUS_TABLES]
        if len(focus) < 2:
            return ""
        in_focus = set(focus)
        used_set = set(used)

        lines: list[str] = []

        def add(line: str) -> None:
            if line not in lines and len(lines) < max_lines:
                lines.append(line)

        # 1. Declared / inferred relationships. Conditions between two tables of the failed SQL come first.
        from src.sql.prompt_builder import get_raw_relationships

        edges: list[tuple[int, str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        for rel in get_raw_relationships(db_id) or []:
            ft, fc = str(rel.get("from_table", "")).lower(), str(rel.get("from_column", ""))
            tt, tc = str(rel.get("to_table", "")).lower(), str(rel.get("to_column", ""))
            if not (ft and fc and tt and tc) or ft == tt or ft not in in_focus or tt not in in_focus:
                continue
            key = (f"{ft}.{fc}".lower(), f"{tt}.{tc}".lower())
            if key in seen_edges:
                continue
            seen_edges.add(key)
            priority = 0 if (ft in used_set and tt in used_set) else 1
            edges.append((priority, f"{ft}.{fc}", f"{tt}.{tc}"))
        for _priority, left, right in sorted(edges):
            add(_edge_line(left, right))

        # 2. Multi-hop routes (Phase 8) between pairs of tables the SQL uses that have no direct condition above.
        from src.sql.paths.route_index import MIN_ROUTE_SCORE, load_route_index

        index = load_route_index(db_id)
        if index is not None:
            direct = {frozenset((a.split(".")[0], b.split(".")[0])) for _p, a, b in edges}
            for a, b in combinations(used[:6], 2):
                if len(lines) >= max_lines:
                    break
                if frozenset((a, b)) in direct:
                    continue
                route = index.best(a, b)
                if route is None or route.score < MIN_ROUTE_SCORE or route.shape == "siblings":
                    continue
                for left, right in route.steps:
                    add(_edge_line(left, right))

        return HEADER + "\n" + "\n".join(lines) if lines else ""
    except Exception as exc:  # noqa: BLE001 - extra context only; the repair prompt is unchanged on any failure
        logger.debug("Repair join context unavailable: %s", exc)
        return ""


__all__ = ["HEADER", "MAX_LINES", "build_repair_join_context"]
