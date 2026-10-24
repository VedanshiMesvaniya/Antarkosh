"""Weighted indirect join paths with hub damping (auto-schema Phase 8).

From each table, a best-first search finds the best routes to tables it does not join to directly, in two tiers.

The near tier (2 to ``max_depth`` joins, default 3) is the original search and is never changed by the deep tier. The
deep tier (``max_depth + 1`` to ``deep_depth`` joins, default 4 to 5) only adds pairs that are more than ``max_depth`` joins
apart, so a pair that has a short route never gets a long one, and it is stricter: a higher score floor, at most one
turn (a route that zigzags between many-to-one and one-to-many joins multiplies rows more with every turn), one route per
pair and fewer targets per table. A route's score is a product, so it can only fall as the route grows, which is what makes best-first correct
and lets weak routes be cut early:

    score = product of link confidences
            x hub damping of every table the route passes through
            x 0.90 per join beyond the first
            x a shape penalty (see below)

Hub damping: a table that links to many others (a "hub" such as a status, user or company table) connects things that
are not really related, so passing through it costs ``sqrt(HUB_FREE / links)`` once it has more than ``HUB_FREE`` links.

Shape: a join result does not depend on the direction we read it, so the penalty looks at the shape only.
A chain of many-to-one joins in one direction (order_item -> order -> customer) is clean. A bridge (film <- film_actor ->
actor, two parents of one child) is the normal many-to-many and costs 10%. Two tables that share a parent
(A -> P <- B read as siblings) multiply each other's rows and cost 30%. A join of unknown direction costs 5%.

Pure and deterministic: ties are broken by table names, and nothing here touches a database or the clock.

Origin: new (no previous file)
"""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from src.sql.paths.links import Link

RULESET_VERSION = 2  # bump when a rule or weight changes: it forces a rewrite even if the catalog did not


@dataclass(frozen=True)
class Params:
    max_depth: int = 3  # joins per route; 4 is allowed, the cost grows quickly with hubs
    min_score: float = 0.30
    depth_decay: float = 0.90
    hub_free: int = 8
    sibling_penalty: float = 0.70  # two tables that only share a parent
    bridge_penalty: float = 0.90  # many-to-many through a junction
    unknown_direction_penalty: float = 0.95
    max_routes_per_pair: int = 2
    max_targets_per_table: int = 25
    max_pops_per_table: int = 6000  # hard bound on the work for one starting table
    max_pairs: int = 50000  # hard bound on the file
    deep_depth: int = 5  # the deep tier covers max_depth+1 .. deep_depth joins; set it <= max_depth to switch the tier off
    deep_min_score: float = 0.40  # stricter than min_score: a long route must be strong all the way
    deep_max_turns: int = 1  # a route may change direction (many-to-one <-> one-to-many) at most this often
    deep_targets_per_table: int = 10
    max_deep_pairs: int = 20000


@dataclass(frozen=True)
class Route:
    tables: tuple[str, ...]
    score: float
    shape: str  # chain | bridge | siblings | mixed | unknown


@dataclass
class SearchStats:
    sources: int = 0
    work_capped: int = 0  # tables whose search hit max_pops_per_table
    target_capped: int = 0  # tables that had more targets than max_targets_per_table
    pairs_dropped: int = 0  # pairs cut by max_pairs
    deep_work_capped: int = 0  # deep tier: tables whose search hit max_pops_per_table
    deep_target_capped: int = 0  # deep tier: tables that had more targets than deep_targets_per_table
    deep_pairs_dropped: int = 0  # deep tier: pairs cut by max_deep_pairs


def hub_factor(links: int, free: int) -> float:
    return 1.0 if links <= free else (free / links) ** 0.5


def route_shape(dirs: list[str | None]) -> str:
    """Name the shape of a route from its step directions."""
    known = [d for d in dirs if d is not None]
    if not known:
        return "unknown"
    turns = [(x, y) for x, y in pairwise(known) if x != y]
    if not turns:
        return "chain"
    if len(turns) == 1:
        return "siblings" if turns[0] == ("up", "down") else "bridge"
    return "mixed"


def _turn_penalty(prev: str | None, step: str | None, p: Params) -> float:
    if step is None:
        return p.unknown_direction_penalty
    if prev is None or prev == step:
        return 1.0
    return p.sibling_penalty if (prev, step) == ("up", "down") else p.bridge_penalty


def _routes_from(
    source: str,
    adj: dict[str, dict[str, Link]],
    damp: dict[str, float],
    blocked_as_bridge: set[str],
    p: Params,
    stats: SearchStats,
) -> dict[str, list[Route]]:
    found: dict[str, list[Route]] = {}
    popped: Counter[str] = Counter()
    heap: list[tuple[float, tuple[str, ...], str | None]] = []
    for n, link in adj[source].items():
        heapq.heappush(heap, (-link.confidence, (source, n), link.step(source)))
    pops = 0
    direct = adj[source]
    while heap:
        neg, nodes, last = heapq.heappop(heap)
        pops += 1
        if pops > p.max_pops_per_table:
            stats.work_capped += 1
            break
        target = nodes[-1]
        popped[target] += 1
        if popped[target] > p.max_routes_per_pair + 1:  # the k-best-paths bound: a node needs only its few best arrivals
            continue
        score = -neg
        hops = len(nodes) - 1
        if hops >= 2 and target not in direct:
            have = found.get(target)
            if have is None:
                if len(found) >= p.max_targets_per_table:
                    stats.target_capped += 1
                    break  # everything left scores no higher than this new target
                found[target] = [Route(nodes, round(score, 4), "")]
            elif len(have) < p.max_routes_per_pair:
                have.append(Route(nodes, round(score, 4), ""))
        if hops >= p.max_depth or target in blocked_as_bridge:
            continue
        base = score * damp.get(target, 1.0) * p.depth_decay  # every other factor is <= 1, so this is the best any extension can reach
        if base < p.min_score:
            continue  # typically a hub: its whole neighbour list is skipped without being read
        for v, link in adj[target].items():
            if v in nodes:
                continue
            step = link.step(target)
            ns = base * link.confidence * _turn_penalty(last, step, p)
            if ns < p.min_score:
                continue
            heapq.heappush(heap, (-ns, (*nodes, v), step or last))
    return found


def _reach(adj: dict[str, dict[str, Link]], source: str, depth: int) -> set[str]:
    """Every table within ``depth`` joins of ``source`` (plain graph distance, so it ignores damping and blocking)."""
    seen = {source}
    frontier = [source]
    for _ in range(depth):
        nxt: list[str] = []
        for u in frontier:
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    nxt.append(v)
        frontier = nxt
    return seen


def _deep_routes_from(
    source: str,
    adj: dict[str, dict[str, Link]],
    damp: dict[str, float],
    blocked_as_bridge: set[str],
    p: Params,
    stats: SearchStats,
) -> dict[str, Route]:
    """The deep tier for one starting table: the single best route (``max_depth + 1`` to ``deep_depth`` joins) to each table
    that is farther than ``max_depth`` joins away. Stricter than the near tier on purpose; see the module docstring."""
    near = _reach(adj, source, p.max_depth)
    found: dict[str, Route] = {}
    popped: Counter[str] = Counter()
    heap: list[tuple[float, tuple[str, ...], str | None, int]] = []
    for n, link in adj[source].items():
        heapq.heappush(heap, (-link.confidence, (source, n), link.step(source), 0))
    pops = 0
    while heap:
        neg, nodes, last, turns = heapq.heappop(heap)
        pops += 1
        if pops > p.max_pops_per_table:
            stats.deep_work_capped += 1
            break
        target = nodes[-1]
        popped[target] += 1
        if popped[target] > 2:  # the k-best-paths bound, with k = 2: a node needs only its two best arrivals
            continue
        score = -neg
        hops = len(nodes) - 1
        if hops > p.max_depth and target not in near and target not in found:
            if len(found) >= p.deep_targets_per_table:
                stats.deep_target_capped += 1
                break  # everything left scores no higher than this new target
            found[target] = Route(nodes, round(score, 4), "")
        if hops >= p.deep_depth or target in blocked_as_bridge:
            continue
        base = score * damp.get(target, 1.0) * p.depth_decay
        if base < p.deep_min_score:
            continue
        for v, link in adj[target].items():
            if v in nodes:
                continue
            step = link.step(target)
            nturns = turns + (1 if last is not None and step is not None and step != last else 0)
            if nturns > p.deep_max_turns:
                continue
            ns = base * link.confidence * _turn_penalty(last, step, p)
            if ns < p.deep_min_score:
                continue
            heapq.heappush(heap, (-ns, (*nodes, v), step or last, nturns))
    return found


def find_paths(
    adj: dict[str, dict[str, Link]],
    blocked_as_bridge: set[str],
    p: Params,
    *,
    should_stop: Callable[[], None] | None = None,
) -> tuple[dict[tuple[str, str], list[Route]], SearchStats]:
    """Best routes for every unordered pair of tables that do not join directly. Keys are ``(a, b)`` with ``a < b``,
    and a route's tables run from ``a`` to ``b``."""
    stats = SearchStats()
    damp = {t: hub_factor(len(n), p.hub_free) for t, n in adj.items()}
    pairs: dict[tuple[str, str], dict[tuple[str, ...], float]] = {}
    for source in sorted(adj):
        if should_stop is not None:
            should_stop()
        stats.sources += 1
        for target, routes in _routes_from(source, adj, damp, blocked_as_bridge, p, stats).items():
            for r in routes:
                tables = r.tables if source < target else tuple(reversed(r.tables))
                key = (tables[0], tables[-1])
                best = pairs.setdefault(key, {})
                best[tables] = max(best.get(tables, 0.0), r.score)  # the same route found from both ends
    out: dict[tuple[str, str], list[Route]] = {}
    for key, routes in pairs.items():
        ranked = sorted(routes.items(), key=lambda kv: (-kv[1], kv[0]))[: p.max_routes_per_pair]
        out[key] = [Route(t, s, _shape_of(t, adj)) for t, s in ranked]
    if len(out) > p.max_pairs:
        keep = sorted(out, key=lambda k: (-out[k][0].score, k))[: p.max_pairs]
        stats.pairs_dropped = len(out) - len(keep)
        out = {k: out[k] for k in sorted(keep)}
    if p.deep_depth > p.max_depth:
        for key, route in _deep_pairs(adj, damp, blocked_as_bridge, p, stats, should_stop).items():
            out.setdefault(key, [route])  # a pair already in the near tier is never touched (it is within max_depth joins anyway)
    return dict(sorted(out.items())), stats


def _deep_pairs(
    adj: dict[str, dict[str, Link]],
    damp: dict[str, float],
    blocked_as_bridge: set[str],
    p: Params,
    stats: SearchStats,
    should_stop: Callable[[], None] | None,
) -> dict[tuple[str, str], Route]:
    best: dict[tuple[str, str], Route] = {}
    for source in sorted(adj):
        if should_stop is not None:
            should_stop()
        for target, r in _deep_routes_from(source, adj, damp, blocked_as_bridge, p, stats).items():
            tables = r.tables if source < target else tuple(reversed(r.tables))
            key = (tables[0], tables[-1])
            old = best.get(key)
            if old is None or (r.score, tables) > (old.score, old.tables):  # the same pair found from both ends
                best[key] = Route(tables, r.score, _shape_of(tables, adj))
    if len(best) > p.max_deep_pairs:
        keep = sorted(best, key=lambda k: (-best[k].score, k))[: p.max_deep_pairs]
        stats.deep_pairs_dropped = len(best) - len(keep)
        best = {k: best[k] for k in keep}
    return best


def route_dirs(tables: tuple[str, ...], adj: dict[str, dict[str, Link]]) -> list[str | None]:
    return [adj[x][y].step(x) for x, y in pairwise(tables)]


def _shape_of(tables: tuple[str, ...], adj: dict[str, dict[str, Link]]) -> str:
    return route_shape(route_dirs(tables, adj))


def check_routes(
    pairs: dict[tuple[str, str], list[Route]],
    adj: dict[str, dict[str, Link]],
    nodes: set[str],
    p: Params,
) -> list[str]:
    """The safety gate: every route must be real. Returns a list of problems; the writer refuses to write if any."""
    problems: list[str] = []
    longest = max(p.max_depth, p.deep_depth)
    for (a, b), routes in pairs.items():
        if not routes or len(routes) > p.max_routes_per_pair:
            problems.append(f"{a}|{b}: wrong number of routes")
            continue
        if b in adj.get(a, {}):
            problems.append(f"{a}|{b}: the pair joins directly, it is not an indirect jump")
        for r in routes:
            t = r.tables
            hops = len(t) - 1
            if t[0] != a or t[-1] != b or len(set(t)) != len(t) or not 2 <= hops <= longest:
                problems.append(f"{a}|{b}: a route has the wrong ends, repeats a table or is too long")
            elif any(x not in nodes for x in t) or any(y not in adj.get(x, {}) for x, y in pairwise(t)):
                problems.append(f"{a}|{b}: a route uses a table or join that is not in the trusted graph")
            elif hops > p.max_depth and (len(routes) != 1 or sum(1 for d in _dirs_turns(t, adj)) > p.deep_max_turns):
                problems.append(f"{a}|{b}: a deep route breaks the deep tier's rules")
            if not (p.min_score if hops <= p.max_depth else p.deep_min_score) <= r.score <= 1.0:
                problems.append(f"{a}|{b}: a route score is out of range")
    return problems


def _dirs_turns(tables: tuple[str, ...], adj: dict[str, dict[str, Link]]) -> list[tuple[str, str]]:
    known = [d for d in route_dirs(tables, adj) if d is not None]
    return [(x, y) for x, y in pairwise(known) if x != y]


def summary_counts(adj: dict[str, dict[str, Link]], pairs: dict[tuple[str, str], list[Route]], stats: SearchStats, p: Params) -> dict[str, Any]:
    shapes: Counter[str] = Counter(r.shape for routes in pairs.values() for r in routes)
    degrees = [len(n) for n in adj.values()]
    return {
        "tables_in_graph": len(adj),
        "links": sum(degrees) // 2,
        "hubs": sum(1 for d in degrees if d > p.hub_free),
        "pairs": len(pairs),
        "deep_pairs": sum(1 for routes in pairs.values() if len(routes[0].tables) - 1 > p.max_depth),
        "longest_route": max((len(routes[0].tables) - 1 for routes in pairs.values()), default=0),
        "routes": sum(len(r) for r in pairs.values()),
        "shapes": {k: shapes[k] for k in sorted(shapes)},
        "work_capped_tables": stats.work_capped,
        "target_capped_tables": stats.target_capped,
        "pairs_dropped": stats.pairs_dropped,
        "deep_work_capped_tables": stats.deep_work_capped,
        "deep_target_capped_tables": stats.deep_target_capped,
        "deep_pairs_dropped": stats.deep_pairs_dropped,
    }


__all__ = [
    "RULESET_VERSION",
    "Params",
    "Route",
    "SearchStats",
    "check_routes",
    "find_paths",
    "hub_factor",
    "route_dirs",
    "route_shape",
    "summary_counts",
]
