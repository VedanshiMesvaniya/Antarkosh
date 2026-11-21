"""The connected schema contract (metadata-driven SQL accuracy, Release 3).

Retrieval picks tables by meaning, so a question can come back with tables that have nothing to do with each other. Shown side by
side, the model joins them anyway and invents the join. This module keeps what the model sees small, connected and trustworthy,
using only what the database already approved: ``relationships.json`` (through ``get_raw_relationships``) and the routes of
``join_paths.json`` that retrieval turned into bridge tables. Nothing here reads data, calls a model or changes validation.

Three small jobs, each a pure function:

* ``plan_contract`` (before the token budget): the tables retrieval chose form groups connected by approved joins. The group that
  holds the most relevant table is the main one. Another group is dropped unless it holds a table that is there on purpose (a
  glossary or domain anchor, a bridge, the real table of a look-alike copy, or one of the question's top vector matches). A weak
  match that nothing approved connects therefore never reaches the prompt.
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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import validate_db_id

logger = logging.getLogger(__name__)

KEEP_TOP_VECTOR = 2  # vector matches ranked this high are the question's subject: kept even when nothing connects them
STRONG_SOURCES = frozenset({"domain_anchor", "table_authority", "route_bridge"})  # there on purpose: never judged by connectivity
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
) -> ContractPlan | None:
    """Which candidate tables to keep. ``candidates`` are in priority order, each with ``table_name`` and ``source``.

    None when the approved graph is empty (nothing can be judged). Otherwise the group holding the first candidate is kept whole,
    and any other group only if one of its tables is there on purpose.
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

    def on_purpose(t: str) -> bool:
        return source[t] in STRONG_SOURCES or (t in vector_rank and vector_rank[t] < keep_top)

    groups = connected_groups(order, edges)
    keep: set[str] = set(groups[0])  # the group that holds the most relevant table
    dropped: list[tuple[str, str]] = []
    for group in groups[1:]:
        if any(on_purpose(t) for t in group):
            keep.update(group)
        else:
            dropped.extend((t, "no approved route to the main tables") for t in group)
    kept = tuple(t for t in order if t in keep)
    return ContractPlan(kept, tuple(dropped), tuple(tuple(g) for g in connected_groups(kept, edges)), len(edges))


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
    "STRONG_SOURCES",
    "ContractPlan",
    "clear_contract_cache",
    "connected_groups",
    "contract_active",
    "contract_mode",
    "mark_unique_keys",
    "plan_contract",
    "trusted_edges",
    "unconnected_note",
    "unique_columns",
]
