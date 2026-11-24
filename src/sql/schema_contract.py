"""The connected schema contract (metadata-driven SQL accuracy, Release 3).

Retrieval picks tables by meaning, so a question can come back with tables that have nothing to do with each other. Shown side by
side, the model joins them anyway and invents the join. This module keeps what the model sees small, connected and trustworthy,
using only what the database already approved: ``relationships.json`` (through ``get_raw_relationships``) and the routes of
``join_paths.json`` that retrieval turned into bridge tables. Nothing here reads data, calls a model or changes validation.

Four small jobs, each a pure function:

* ``plan_contract`` (before the token budget): the tables retrieval chose form groups connected by approved joins. The group that
  holds the most relevant table is the main one. Another group is dropped unless it holds a table that is there on purpose (a
  glossary or domain anchor, a bridge, the real table of a look-alike copy, or one of the question's top vector matches). A weak
  match that nothing approved connects therefore never reaches the prompt. When the caller also passes ``evidence`` (how directly
  the question's own words point at each table, see ``retrieval_terms.question_evidence``): the main group is the one with the
  most evidence, not the one whose vector chunk came first; a table with direct evidence (a word of its name, a column, a meaning)
  is never dropped, so an entity the question names is never lost because it sits in a separate graph component; and when two or
  more such tables are joined by approved relationships, only a few loosely related neighbours are kept beside the route between
  them, not every neighbour of a large connected graph.
* ``plan_filter_route``: when the question names a country, the shortest approved route from the entity table to a table that
  carries a country, region, location or address column. Approved relationships and ``join_paths.json`` routes only; no join is
  ever invented.
* ``unconnected_note`` (after the budget, on what will really be shown): when the shown tables still fall into groups with no
  approved route between them, one line says so, so the model is told not to join across them instead of left to guess.
* ``mark_unique_keys``: single-column unique keys (not primary) are marked ``UQ`` in the compact table lines, so a business key
  such as an order number is visible next to ``PK`` and ``FK->``.

``contract_mode`` / ``contract_active`` say when this runs. The prompt's grounding block (see ``prompt_builder``) uses the same
switch, so the model is only told "approved joins only" when the schema it sees was built that way.

Safety rules, each tested:

* Off unless the database runs the Phase 12 flags in apply mode AND ``auto_schema_contract_apply`` is on; shadow mode only logs
  what would change. ``erp_main`` is never affected (its flags are pinned off).
* A database with no approved relationship at all cannot be judged: nothing is dropped and the prompt keeps its old wording.
* The first (most relevant) table is never dropped. A missing, unreadable or empty input gives "no change".
* The note never contains the word "table" followed by a name and never starts with ``word:``: both shapes are read as tables by
  the validators that parse the schema text.

Origin: new (no previous file)
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import validate_db_id

logger = logging.getLogger(__name__)

KEEP_TOP_VECTOR = 2  # vector matches ranked this high are the question's subject: kept even when nothing connects them
STRONG_SOURCES = frozenset({"domain_anchor", "table_authority", "route_bridge"})  # there on purpose: never judged by connectivity
MAX_LOOSE_NEIGHBORS = 4  # neighbours without evidence kept beside a route between tables the question names (the old 1-hop intent)
MAX_FILTER_ADD = 3  # tables one filter route may add
MAX_NOTE_GROUPS = 6
MAX_NOTE_TABLES = 6  # per group in the note; the rest are counted

_AUDIT_COLUMNS = ("created_id", "updated_id", "deleted_id")  # links to users that would connect everything: same rule as 1-hop expansion
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------------------- when it runs


def contract_mode(db_id: str) -> str:
    """``"apply"``, ``"shadow"`` or ``""`` (off), the way ``hints_mode`` works.

    Needs the Phase 12 flags (bridge tables are what keep a route in one piece). Even with ``auto_prompt_inject`` on, the contract
    only applies where ``auto_schema_contract_apply`` is also on for the database (it starts off); every other flagged database
    runs it in shadow mode, so turning on route bridges never switches it on by itself.
    """
    try:
        from src.sql.schema_retrieval import _route_bridge_mode
        from src.utils.feature_flags import is_db_feature_enabled

        base = _route_bridge_mode(db_id)
        if not base:
            return ""
        if base == "apply" and is_db_feature_enabled("auto_schema_contract_apply", db_id):
            return "apply"
        return "shadow"
    except Exception as exc:  # noqa: BLE001 - on any doubt the contract stays off
        logger.debug("Schema contract mode unreadable for %s: %s", db_id, exc)
        return ""


def contract_active(db_id: str) -> bool:
    """True when the contract applies AND the database has approved joins to build it from (else it cannot be judged)."""
    try:
        if contract_mode(db_id) != "apply":
            return False
        from src.sql.prompt_builder import get_raw_relationships

        return bool(trusted_edges(get_raw_relationships(db_id)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Schema contract state unreadable for %s: %s", db_id, exc)
        return False


# ---------------------------------------------------------------------------------------------------- the approved graph


def trusted_edges(rels: Iterable[dict[str, Any]] | None, chains: Iterable[Sequence[str]] = ()) -> set[frozenset[str]]:
    """Undirected table pairs joined by an approved relationship, plus the steps of the approved routes in ``chains``."""
    edges: set[frozenset[str]] = set()
    for r in rels or ():
        if not isinstance(r, dict):
            continue
        frm, to = str(r.get("from_table") or "").lower(), str(r.get("to_table") or "").lower()
        if not frm or not to or frm == to:
            continue
        if to == "users" and str(r.get("from_column") or "").lower() in _AUDIT_COLUMNS:
            continue
        edges.add(frozenset((frm, to)))
    for chain in chains:
        names = [str(t).lower() for t in chain]
        edges.update(frozenset(pair) for pair in itertools.pairwise(names) if pair[0] != pair[1])
    return edges


def connected_groups(names: Sequence[str], edges: set[frozenset[str]]) -> list[list[str]]:
    """The names split into groups joined by ``edges`` (only edges between two of the names count). Input order is kept."""
    ordered: list[str] = []
    for n in names:
        low = str(n).lower()
        if low not in ordered:
            ordered.append(low)
    parent = {n: n for n in ordered}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for edge in edges:
        pair = [t for t in edge if t in parent]
        if len(pair) == 2:
            parent[find(pair[0])] = find(pair[1])
    groups: dict[str, list[str]] = {}
    for n in ordered:
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


# ---------------------------------------------------------------------------------------------------- 1. drop weak, disconnected matches


@dataclass(frozen=True)
class ContractPlan:
    keep: tuple[str, ...]  # the tables to keep, in the order they were offered
    dropped: tuple[tuple[str, str], ...]  # (table, why)
    groups: tuple[tuple[str, ...], ...]  # the groups among the kept tables
    edges_known: int


def plan_contract(
    candidates: Sequence[dict[str, Any]],
    rels: Iterable[dict[str, Any]] | None,
    chains: Iterable[Sequence[str]] = (),
    *,
    keep_top: int = KEEP_TOP_VECTOR,
    evidence: Mapping[str, float] | None = None,
    protected: Collection[str] = (),
    max_loose: int = MAX_LOOSE_NEIGHBORS,
) -> ContractPlan | None:
    """Which candidate tables to keep. ``candidates`` are in priority order, each with ``table_name`` and ``source``.

    None when the approved graph is empty (nothing can be judged). Otherwise the group holding the first candidate is kept whole,
    and any other group only if one of its tables is there on purpose.

    With ``evidence`` (table -> how directly the question's words point at it) the main group is the one with the most evidence
    (the first one on a tie or when there is none), and ``protected`` tables (direct evidence, ends of an approved route) count as
    there on purpose. The first candidate is always protected. Without ``evidence`` nothing changes.
    """
    source: dict[str, str] = {}
    vector_rank: dict[str, int] = {}
    order: list[str] = []
    for c in candidates:
        name = str(c.get("table_name") or "").lower()
        if not name or name in source:
            continue
        order.append(name)
        source[name] = str(c.get("source") or "")
        if source[name] == "vector_rag":
            vector_rank[name] = len(vector_rank)
    if not order:
        return None
    edges = trusted_edges(rels, chains)
    if not edges:
        return None
    scored = {str(t).lower(): float(v) for t, v in (evidence or {}).items() if v and v > 0}
    safe = {str(t).lower() for t in protected} | set(scored)
    if evidence is not None:
        safe.add(order[0])  # the most relevant table is never dropped

    def on_purpose(t: str) -> bool:
        return source[t] in STRONG_SOURCES or (t in vector_rank and vector_rank[t] < keep_top) or t in safe

    groups = connected_groups(order, edges)
    main = 0
    if scored:
        totals = [sum(scored.get(t, 0.0) for t in g) for g in groups]
        main = max(range(len(groups)), key=lambda i: (totals[i], -i))  # most evidence; the earliest group on a tie
    keep: set[str] = set(groups[main])
    dropped: list[tuple[str, str]] = []
    for i, group in enumerate(groups):
        if i == main:
            continue
        if any(on_purpose(t) for t in group):
            keep.update(group)
        else:
            dropped.extend((t, "no approved route to the main tables") for t in group)
    if evidence is not None:
        # The smallest connected subgraph: with two or more tables the question names, joined by approved relationships, the
        # neighbours that were only added because they touch something retrieved are limited to a few.
        targets = [t for t in order if t in keep and t in safe]
        needed = route_tables(targets, edges, keep) if len(targets) >= 2 else None
        if needed is not None:
            loose = [t for t in order if t in keep and source[t] == "graph_expansion" and t not in needed and t not in safe]
            for t in loose[max(0, max_loose):]:
                keep.discard(t)
                dropped.append((t, "not on the approved route between the requested tables"))
    kept = tuple(t for t in order if t in keep)
    return ContractPlan(kept, tuple(dropped), tuple(tuple(g) for g in connected_groups(kept, edges)), len(edges))


def route_tables(targets: Sequence[str], edges: set[frozenset[str]], within: Collection[str]) -> set[str] | None:
    """The tables of the smallest approved route that connects every one of ``targets``, walking only through ``within``.

    Greedy: start from the first target and add the shortest path to each next one (ties broken by name, so it is deterministic).
    None when some target cannot be reached through approved edges: the caller then does nothing extra. Pure.
    """
    allowed = {str(t).lower() for t in within}
    goals = [t for t in dict.fromkeys(str(t).lower() for t in targets) if t in allowed]
    if len(goals) < 2:
        return None
    neighbours: dict[str, set[str]] = {}
    for edge in edges:
        pair = [t for t in edge if t in allowed]
        if len(pair) == 2:
            neighbours.setdefault(pair[0], set()).add(pair[1])
            neighbours.setdefault(pair[1], set()).add(pair[0])
    joined = {goals[0]}
    for goal in goals[1:]:
        if goal in joined:
            continue
        previous: dict[str, str | None] = {t: None for t in joined}
        frontier = sorted(joined)
        reached = False
        while frontier and not reached:
            nxt: list[str] = []
            for node in frontier:
                for n in sorted(neighbours.get(node, ())):
                    if n in previous:
                        continue
                    previous[n] = node
                    if n == goal:
                        reached = True
                        break
                    nxt.append(n)
                if reached:
                    break
            frontier = nxt
        if not reached:
            return None
        node: str | None = goal
        while node is not None and node not in joined:
            joined.add(node)
            node = previous[node]
    return joined


@dataclass(frozen=True)
class FilterRoute:
    entity: str
    place: str  # the table that carries the country / region / location / address column
    tables: tuple[str, ...]  # the whole route, entity first
    add: tuple[str, ...]  # tables of the route retrieval did not already have
    hint_lines: tuple[str, ...] = ()  # ``- a.col -> b.col`` for a route read from join_paths.json (a direct join is shown by the prompt already)


def plan_filter_route(
    entities: Sequence[str],
    places: Mapping[str, int],
    rels: Iterable[dict[str, Any]] | None,
    *,
    available: Iterable[str],
    present: Iterable[str] = (),
    index: Any | None = None,
    db_id: str | None = None,
    max_add: int = MAX_FILTER_ADD,
) -> FilterRoute | None:
    """The shortest approved route from an entity table to a table that holds a country/region/location/address column.

    ``entities`` are the tables the question names; ``places`` maps tables to 0 (holds a country) or 1 (region, location, address).
    A route is a direct approved relationship, or a ``join_paths.json`` route that is convincing and not a sibling route. Every table
    of it must exist in ``available``. A table that holds a country always beats one that only holds an address; then fewer joins,
    then the better route score, then names. None when the entity already holds the country itself, when there is no approved
    route, or when the route would add more than ``max_add`` tables. Nothing is guessed, and no data is read. Pure.
    """
    try:
        ents = [e for e in dict.fromkeys(str(e).lower() for e in entities) if e]
        flt = {str(t).lower(): int(v) for t, v in places.items()}
        if not ents or not flt or any(e in flt for e in ents):
            return None  # nothing to route, or the entity holds the country itself
        exist = {str(t).lower() for t in available}
        have = {str(t).lower() for t in present}
        edges = trusted_edges(rels)
        if index is None and db_id:
            from src.sql.paths.route_index import load_route_index

            index = load_route_index(db_id)
        from src.sql.paths.route_index import EXCLUDED_SHAPES, MIN_ROUTE_SCORE

        options: list[tuple[tuple[Any, ...], FilterRoute]] = []
        for e in ents:
            for f in sorted(flt):
                if f == e or f not in exist:
                    continue
                if frozenset((e, f)) in edges:
                    tables, hints, score = (e, f), (), 1.0
                else:
                    route = index.best(e, f) if index is not None else None
                    if route is None or route.score < MIN_ROUTE_SCORE or route.shape in EXCLUDED_SHAPES:
                        continue
                    if any(t not in exist for t in route.tables):
                        continue
                    tables, hints, score = route.tables, tuple(f"- {a} -> {b}" for a, b in route.steps), route.score
                add = tuple(t for t in tables if t not in have)
                if len(add) > max_add:
                    continue
                options.append(((flt[f], len(tables), -score, e, f), FilterRoute(e, f, tuple(tables), add, hints)))
        if not options:
            return None
        return min(options, key=lambda o: o[0])[1]
    except Exception as exc:  # noqa: BLE001 - an extra; the caller carries on without it
        logger.debug("Filter route planning skipped: %s", exc)
        return None


# ---------------------------------------------------------------------------------------------------- 2. say what is not connected


def unconnected_note(
    names: Sequence[str],
    rels: Iterable[dict[str, Any]] | None,
    chains: Iterable[Sequence[str]] = (),
) -> str:
    """One line naming the groups of shown tables that no approved route connects; "" when they form one group (or on any doubt).

    ``chains`` are routes whose tables are ALL among ``names`` (a route cut by the token budget is not a route).
    """
    try:
        shown = {str(n).lower() for n in names}
        whole = [c for c in chains if c and all(str(t).lower() in shown for t in c)]
        edges = trusted_edges(rels, whole)
        if not edges:
            return ""
        groups = connected_groups(names, edges)
        if len(groups) < 2:
            return ""

        def show(group: list[str]) -> str:
            head = ", ".join(group[:MAX_NOTE_TABLES])
            return f"({head}{f', +{len(group) - MAX_NOTE_TABLES} more' if len(group) > MAX_NOTE_TABLES else ''})"

        parts = [show(g) for g in groups[:MAX_NOTE_GROUPS]]
        more = f" and {len(groups) - MAX_NOTE_GROUPS} more groups" if len(groups) > MAX_NOTE_GROUPS else ""
        return f"No approved route connects these groups: {' | '.join(parts)}{more}. Do not join across groups."
    except Exception as exc:  # noqa: BLE001 - an extra line only
        logger.debug("Unconnected note skipped: %s", exc)
        return ""


# ---------------------------------------------------------------------------------------------------- 3. unique keys in the table lines

_UQ_CACHE: dict[str, tuple[tuple[int, int] | None, dict[str, frozenset[str]]]] = {}
_LINE = re.compile(r"^(\w+): (.*)$")
_COLUMN = re.compile(r"(\w+)\(([^()]*)\)")


def clear_contract_cache(db_id: str | None = None) -> None:
    with _LOCK:
        if db_id is None:
            _UQ_CACHE.clear()
        else:
            _UQ_CACHE.pop(db_id, None)


def unique_columns(db_id: str) -> dict[str, frozenset[str]]:
    """``{table: {column}}``: single-column unique keys that are not the primary key, from ``schema/schema.json`` (cached until it
    changes). Tables whose bare name two schemas share are left out. Empty on any problem."""
    try:
        validate_db_id(db_id)
        path = registry.DATABASES_DIR / db_id / "schema" / "schema.json"
        try:
            st = path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with _LOCK:
            cached = _UQ_CACHE.get(db_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        found: dict[str, frozenset[str]] = {}
        if stamp is not None:
            from src.sql.relationships.model import load_graph

            graph = load_graph(db_id)
            if graph is not None:
                collisions = graph.name_collisions()
                for table in graph.tables.values():
                    bare = table.name.lower()
                    if bare in collisions or table.is_view:
                        continue
                    cols = frozenset(c.name.lower() for c in table.columns if table.key_strength(c.name) in ("unique", "unique_index"))
                    if cols:
                        found[bare] = cols
        with _LOCK:
            _UQ_CACHE[db_id] = (stamp, found)
        return found
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unique columns unavailable for %s: %s", db_id, exc)
        return {}


def mark_unique_keys(compact_lines: Sequence[str], db_id: str) -> list[str]:
    """Add ``UQ`` to the unique-key columns of compact table lines (``table: col(type, PK), col(type)``). Other lines, and columns
    already marked ``PK``, stay exactly as they were."""
    lines = list(compact_lines)
    unique = unique_columns(db_id)
    if not unique:
        return lines
    out: list[str] = []
    for line in lines:
        out.append("\n".join(_mark_line(part, unique) for part in line.split("\n")))
    return out


def _mark_line(line: str, unique: dict[str, frozenset[str]]) -> str:
    m = _LINE.match(line)
    cols = unique.get(m.group(1).lower()) if m else None
    if not m or not cols:
        return line

    def add(col: re.Match[str]) -> str:
        name, body = col.group(1), col.group(2)
        markers = [p.strip() for p in body.split(",")]
        if name.lower() not in cols or "PK" in markers or "UQ" in markers:
            return col.group(0)
        return f"{name}({body}, UQ)"

    return f"{m.group(1)}: {_COLUMN.sub(add, m.group(2))}"


__all__ = [
    "KEEP_TOP_VECTOR",
    "MAX_FILTER_ADD",
    "MAX_LOOSE_NEIGHBORS",
    "STRONG_SOURCES",
    "ContractPlan",
    "FilterRoute",
    "clear_contract_cache",
    "connected_groups",
    "contract_active",
    "contract_mode",
    "mark_unique_keys",
    "plan_contract",
    "plan_filter_route",
    "route_tables",
    "trusted_edges",
    "unconnected_note",
    "unique_columns",
]
