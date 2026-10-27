"""Route-aware bridge tables for the schema prompt (auto-schema Phase 12, part 1).

Phase 8 writes ``databases/<db_id>/schema/join_paths.json``: for tables that do not join directly, the best indirect routes
(2 to 5 joins), scored with hub damping. Nothing read that file, so a question that needs a route such as
``A -> B -> C -> D`` only worked when the retriever happened to pick B and C as well. Schema retrieval picks a few tables by
meaning and then adds only their direct neighbours, which can never reach the middle of a route of four or more joins.

This module closes that gap without changing how anything else works:

* ``load_route_index`` reads the file once per database and keeps it until the file changes (size + mtime).
* ``plan_route_bridges`` takes the tables retrieval already chose and returns the bridge tables that connect pairs of them,
  plus the join conditions of those routes. It never invents a table: every bridge must exist in the live schema.

Safety rules, each tested:

* ``erp_main`` is never read (its curated files are pinned).
* A missing, unreadable, empty or differently shaped file gives ``None`` - the caller then behaves exactly as before.
* A route is added whole or not at all, and the number of added tables is capped, so the prompt cannot grow without limit.
* Routes that only join two tables through a shared parent ("siblings": rows multiply) and weak routes are left out.
* Pure reading: no database access, no writes, no clock.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import DEFAULT_DB_ID, validate_db_id

logger = logging.getLogger(__name__)

PATHS_REL = "schema/join_paths.json"

MAX_BRIDGES = 4  # most tables one question may gain from routes
MAX_SEEDS = 6  # retrieval seeds considered for pairing (anchors first, then vector hits)
MIN_ROUTE_SCORE = 0.45  # Phase 8 floors are 0.30 (near) and 0.40 (deep); a bridge must be convincing
EXCLUDED_SHAPES = frozenset({"siblings"})  # two tables that only share a parent: joining them multiplies rows


@dataclass(frozen=True)
class IndexedRoute:
    tables: tuple[str, ...]  # lower-case, first to last
    score: float
    shape: str
    steps: tuple[tuple[str, str], ...]  # one (``table.column``, ``table.column``) pair per join, in route order


@dataclass(frozen=True)
class PlannedRoute:
    tables: tuple[str, ...]
    score: float
    hint_lines: tuple[str, ...]  # ``- a.col -> b.col``, the same shape the compactor's "Join Hints:" block uses


@dataclass(frozen=True)
class RoutePlan:
    bridges: tuple[str, ...]  # tables to add, in the order they should be offered to the budget
    routes: tuple[PlannedRoute, ...]  # the routes that justify them
    pairs_checked: int  # seed pairs that were looked up
    pairs_routed: int  # of those, pairs that have an indirect route in the file

    def hint_lines(self, present: Iterable[str]) -> list[str]:
        """Join conditions of every planned route whose tables are ALL in ``present`` (a route cut by the budget is not shown)."""
        have = {t.lower() for t in present}
        out: list[str] = []
        for route in self.routes:
            if all(t in have for t in route.tables):
                for line in route.hint_lines:
                    if line not in out:
                        out.append(line)
        return out


class RouteIndex:
    """Best indirect route per table pair, looked up in either direction."""

    def __init__(self, pairs: dict[tuple[str, str], IndexedRoute]) -> None:
        self._pairs = pairs

    def __len__(self) -> int:
        return len(self._pairs)

    def best(self, a: str, b: str) -> IndexedRoute | None:
        a, b = a.lower(), b.lower()
        route = self._pairs.get((a, b))
        if route is not None:
            return route
        reverse = self._pairs.get((b, a))
        if reverse is None:
            return None
        return IndexedRoute(
            tables=tuple(reversed(reverse.tables)),
            score=reverse.score,
            shape=reverse.shape,
            steps=tuple((right, left) for left, right in reversed(reverse.steps)),
        )


# --------------------------------------------------------------------------- loading (cached until the file changes)

_CACHE: dict[str, tuple[tuple[int, int] | None, RouteIndex | None]] = {}
_LOCK = threading.Lock()


def clear_route_index_cache(db_id: str | None = None) -> None:
    with _LOCK:
        if db_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(db_id, None)


def _intern(value: Any) -> str:
    return sys.intern(str(value).strip().lower())


def _parse_pair(entry: Any) -> tuple[tuple[str, str], IndexedRoute] | None:
    """One ``pairs`` entry -> ``((a, b), best route)``, or None when it is not shaped as Phase 8 writes it."""
    if not isinstance(entry, dict):
        return None
    routes = entry.get("routes")
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        return None
    best = routes[0]  # Phase 8 lists a pair's routes best first
    tables = best.get("tables")
    joins = best.get("joins")
    if not isinstance(tables, list) or len(tables) < 3 or not isinstance(joins, list) or len(joins) != len(tables) - 1:
        return None  # a direct join (2 tables) is not a multi-hop route and needs no bridge
    steps: list[tuple[str, str]] = []
    for join in joins:
        if not isinstance(join, dict) or not join.get("from") or not join.get("to"):
            return None
        steps.append((_intern(join["from"]), _intern(join["to"])))
    try:
        score = float(best.get("score", entry.get("score", 0.0)))
    except (TypeError, ValueError):
        return None
    names = tuple(_intern(t) for t in tables)
    return (names[0], names[-1]), IndexedRoute(names, score, str(best.get("shape") or "unknown").lower(), tuple(steps))


def load_route_index(db_id: str) -> RouteIndex | None:
    """The route index for ``db_id``, or None when there is no usable ``join_paths.json`` (never raises)."""
    try:
        validate_db_id(db_id)
        if db_id == DEFAULT_DB_ID:
            return None  # pinned database: curated files only
        path: Path = registry.DATABASES_DIR / db_id / PATHS_REL
        try:
            stat = path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        with _LOCK:
            cached = _CACHE.get(db_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        index: RouteIndex | None = None
        if stamp is not None:
            doc = json.loads(path.read_text(encoding="utf-8"))
            entries = doc.get("pairs") if isinstance(doc, dict) else None
            if isinstance(entries, list):
                pairs: dict[tuple[str, str], IndexedRoute] = {}
                for entry in entries:
                    parsed = _parse_pair(entry)
                    if parsed is not None:
                        pairs[parsed[0]] = parsed[1]
                index = RouteIndex(pairs) if pairs else None
        with _LOCK:
            _CACHE[db_id] = (stamp, index)
        return index
    except Exception as exc:  # noqa: BLE001 - an unreadable file must never break a question
        logger.warning("Route index for %s unavailable: %s: %s", db_id, type(exc).__name__, str(exc)[:200])
        return None


# --------------------------------------------------------------------------- planning


def plan_route_bridges(
    seeds: Sequence[str],
    db_id: str,
    *,
    available: Iterable[str] | None = None,
    max_add: int = MAX_BRIDGES,
    min_score: float = MIN_ROUTE_SCORE,
    max_seeds: int = MAX_SEEDS,
    index: RouteIndex | None = None,
) -> RoutePlan | None:
    """Bridge tables that connect pairs of already-selected tables.

    ``seeds`` are the tables retrieval chose, most relevant first. ``available`` is the set of tables that really exist in
    the live schema: a route through a table that is not there is skipped whole. Returns None when there is no route file
    (the caller then does nothing); otherwise a plan whose ``bridges`` may be empty.
    """
    idx = index if index is not None else load_route_index(db_id)
    if idx is None:
        return None

    ordered: list[str] = []
    for table in seeds:
        name = str(table).lower()
        if name and name not in ordered:
            ordered.append(name)
    ordered = ordered[:max_seeds]
    seed_set = set(ordered)
    exist = {t.lower() for t in available} if available is not None else None

    candidates: list[tuple[float, int, str, str, IndexedRoute]] = []
    checked = routed = 0
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            checked += 1
            route = idx.best(a, b)
            if route is None:
                continue
            routed += 1
            if route.score < min_score or route.shape in EXCLUDED_SHAPES:
                continue
            middle = route.tables[1:-1]
            if exist is not None and any(t not in exist for t in middle):
                continue
            if all(t in seed_set for t in middle):
                continue  # nothing to add: the whole route is already in the prompt
            candidates.append((-route.score, len(route.tables), a, b, route))
    candidates.sort(key=lambda c: (c[0], c[1], c[2], c[3]))  # best score, then fewest joins, then names (deterministic)

    chosen: list[str] = []
    planned: list[PlannedRoute] = []
    for _neg_score, _hops, _a, _b, route in candidates:
        new = [t for t in route.tables[1:-1] if t not in seed_set and t not in chosen]
        if len(chosen) + len(new) > max_add:
            continue  # a route is added whole or not at all
        chosen.extend(new)
        planned.append(PlannedRoute(route.tables, route.score, tuple(f"- {left} -> {right}" for left, right in route.steps)))
    return RoutePlan(tuple(chosen), tuple(planned), checked, routed)


def merge_join_hints(existing: str, extra_lines: Sequence[str]) -> str:
    """Append route join lines to the compactor's ``Join Hints:`` block (creating it when there is none), skipping repeats."""
    if not extra_lines:
        return existing
    lines = existing.splitlines() if existing else ["Join Hints:"]
    seen = set(lines)
    for line in extra_lines:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)


__all__ = [
    "MAX_BRIDGES",
    "MIN_ROUTE_SCORE",
    "IndexedRoute",
    "PlannedRoute",
    "RouteIndex",
    "RoutePlan",
    "clear_route_index_cache",
    "load_route_index",
    "merge_join_hints",
    "plan_route_bridges",
]
