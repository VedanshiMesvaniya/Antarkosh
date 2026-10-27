"""Route-aware bridge tables (auto-schema Phase 12, part 1): the route index, the planner and the retrieval wiring.

Nothing here names a real table or question: the schemas are synthetic chains with shared hub tables.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest

import src.sql.prompt_builder as pb
import src.sql.schema_retrieval as sr
from src.sql import registry
from src.sql.paths import route_index as ri
from src.sql.schema_budget import select_schema_within_budget

DB = "route_db"


# ----------------------------------------------------------------------------------------------- file helpers


def _join(x: str, y: str) -> dict[str, Any]:
    return {"from": f"{x}.{y}_id", "to": f"{y}.{y}_id", "confidence": 0.9, "evidence": ["declared"], "direction": "many_to_one"}


def _pair(tables: list[str], score: float = 0.8, shape: str = "chain") -> dict[str, Any]:
    joins = [_join(x, y) for x, y in itertools.pairwise(tables)]
    return {"a": tables[0], "b": tables[-1], "hops": len(tables) - 1, "score": score, "tier": "near",
            "routes": [{"score": score, "shape": shape, "tables": tables, "joins": joins}]}


def _write(root: Path, pairs: list[dict[str, Any]], db: str = DB) -> Path:
    folder = root / db / "schema"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "join_paths.json"
    path.write_text(json.dumps({"generated_by": "auto_join_paths", "pairs": pairs}), encoding="utf-8")
    return path


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    ri.clear_route_index_cache()
    yield tmp_path
    ri.clear_route_index_cache()


# ----------------------------------------------------------------------------------------------- the index


def test_index_finds_a_route_in_either_direction_and_reverses_it_whole(dbs: Path) -> None:
    _write(dbs, [_pair(["hdr", "b1", "b2", "kind"])])
    idx = ri.load_route_index(DB)
    assert idx is not None and len(idx) == 1
    fwd, rev = idx.best("hdr", "kind"), idx.best("KIND", "Hdr")
    assert fwd is not None and rev is not None
    assert rev.tables == tuple(reversed(fwd.tables))
    assert fwd.steps == (("hdr.b1_id", "b1.b1_id"), ("b1.b2_id", "b2.b2_id"), ("b2.kind_id", "kind.kind_id"))
    assert rev.steps == (("kind.kind_id", "b2.kind_id"), ("b2.b2_id", "b1.b2_id"), ("b1.b1_id", "hdr.b1_id"))
    assert idx.best("hdr", "nothing") is None


def test_missing_empty_malformed_and_pinned_all_mean_no_index(dbs: Path) -> None:
    assert ri.load_route_index(DB) is None  # no file
    folder = dbs / DB / "schema"
    folder.mkdir(parents=True)
    (folder / "join_paths.json").write_text("{not json", encoding="utf-8")
    assert ri.load_route_index(DB) is None  # unreadable
    (folder / "join_paths.json").write_text(json.dumps({"pairs": []}), encoding="utf-8")
    assert ri.load_route_index(DB) is None  # empty
    (folder / "join_paths.json").write_text(json.dumps({"pairs": [{"a": "x"}, "junk", {"routes": [{"tables": ["a", "b"]}]}]}), encoding="utf-8")
    assert ri.load_route_index(DB) is None  # nothing usable (a 2-table route is a direct join, not a route)
    _write(dbs, [_pair(["a", "m", "z"])], db="erp_main")
    assert ri.load_route_index("erp_main") is None  # pinned database is never read
    assert ri.load_route_index("Bad-Id") is None  # invalid id never raises


def test_index_reloads_when_the_file_changes(dbs: Path) -> None:
    path = _write(dbs, [_pair(["a", "m", "z"])])
    first = ri.load_route_index(DB)
    assert first is ri.load_route_index(DB)  # cached
    _write(dbs, [_pair(["a", "m", "z"]), _pair(["p", "q", "r", "s"])])
    path.touch()
    second = ri.load_route_index(DB)
    assert second is not first and len(second) == 2


# ----------------------------------------------------------------------------------------------- the planner


def _idx(*pairs: dict[str, Any]) -> ri.RouteIndex:
    return ri.RouteIndex({k: v for p in pairs for k, v in [ri._parse_pair(p)]})  # type: ignore[misc]


def test_planner_adds_every_middle_table_of_a_long_route() -> None:
    idx = _idx(_pair(["hdr", "b1", "b2", "b3", "kind"]))
    plan = ri.plan_route_bridges(["hdr", "kind", "d1"], DB, index=idx, available={"hdr", "b1", "b2", "b3", "kind", "d1"})
    assert plan is not None and plan.bridges == ("b1", "b2", "b3")
    assert plan.routes[0].hint_lines == ("- hdr.b1_id -> b1.b1_id", "- b1.b2_id -> b2.b2_id", "- b2.b3_id -> b3.b3_id", "- b3.kind_id -> kind.kind_id")
    assert plan.pairs_checked == 3 and plan.pairs_routed == 1


def test_planner_is_deterministic_and_direction_free() -> None:
    idx = _idx(_pair(["hdr", "b1", "b2", "kind"]))
    avail = {"hdr", "b1", "b2", "kind"}
    one = ri.plan_route_bridges(["hdr", "kind"], DB, index=idx, available=avail)
    two = ri.plan_route_bridges(["kind", "hdr"], DB, index=idx, available=avail)
    assert one.bridges == ("b1", "b2") and two.bridges == ("b2", "b1")  # the route is read from the first seed
    assert set(one.bridges) == set(two.bridges)


def test_planner_never_adds_a_partial_route_or_more_than_the_cap() -> None:
    idx = _idx(_pair(["a", "b1", "b2", "b3", "b4", "z"]))  # needs 4 bridges
    avail = {"a", "b1", "b2", "b3", "b4", "z"}
    assert ri.plan_route_bridges(["a", "z"], DB, index=idx, available=avail, max_add=3).bridges == ()
    assert ri.plan_route_bridges(["a", "z"], DB, index=idx, available=avail, max_add=4).bridges == ("b1", "b2", "b3", "b4")


def test_planner_skips_weak_sibling_missing_and_already_present_routes() -> None:
    avail = {"a", "m", "z", "x", "y"}
    weak = _idx(_pair(["a", "m", "z"], score=0.30))
    assert ri.plan_route_bridges(["a", "z"], DB, index=weak, available=avail).bridges == ()
    sib = _idx(_pair(["a", "m", "z"], shape="siblings"))
    assert ri.plan_route_bridges(["a", "z"], DB, index=sib, available=avail).bridges == ()
    ok = _idx(_pair(["a", "m", "z"]))
    assert ri.plan_route_bridges(["a", "z"], DB, index=ok, available={"a", "z"}).bridges == ()  # bridge not in the live schema
    assert ri.plan_route_bridges(["a", "m", "z"], DB, index=ok, available=avail).bridges == ()  # bridge already a seed
    assert ri.plan_route_bridges(["a"], DB, index=ok, available=avail).bridges == ()  # one seed: no pair


def test_planner_prefers_the_stronger_route_when_the_cap_forces_a_choice() -> None:
    idx = _idx(_pair(["a", "p1", "p2", "z"], score=0.9), _pair(["a", "q1", "q2", "y"], score=0.5))
    avail = {"a", "z", "y", "p1", "p2", "q1", "q2"}
    plan = ri.plan_route_bridges(["a", "z", "y"], DB, index=idx, available=avail, max_add=2)
    assert plan.bridges == ("p1", "p2")


def test_planner_without_a_file_is_none(dbs: Path) -> None:
    assert ri.plan_route_bridges(["a", "b"], DB) is None


def test_hint_lines_show_only_routes_that_fully_survived_the_budget() -> None:
    idx = _idx(_pair(["a", "m", "z"]))
    plan = ri.plan_route_bridges(["a", "z"], DB, index=idx, available={"a", "m", "z"})
    assert plan.hint_lines(["a", "m", "z"]) == ["- a.m_id -> m.m_id", "- m.z_id -> z.z_id"]
    assert plan.hint_lines(["a", "z"]) == []  # the bridge was dropped by the budget: no dangling hint


def test_merge_join_hints_keeps_the_existing_block_and_skips_repeats() -> None:
    assert ri.merge_join_hints("Join Hints:\n- a.x -> b.y", ["- a.x -> b.y", "- b.y -> c.z"]) == "Join Hints:\n- a.x -> b.y\n- b.y -> c.z"
    assert ri.merge_join_hints("", ["- a.x -> b.y"]) == "Join Hints:\n- a.x -> b.y"
    assert ri.merge_join_hints("Join Hints:\n- a.x -> b.y", []) == "Join Hints:\n- a.x -> b.y"


# ----------------------------------------------------------------------------------------------- the budget hook


def test_budget_selector_is_unchanged_without_a_cost_function() -> None:
    cands = [{"table_name": f"t{i}", "ddl": "x" * 400} for i in range(5)]  # ~100 tokens each
    base = select_schema_within_budget(cands, token_budget=250)
    assert [c["table_name"] for c in base[0]] == ["t0", "t1"]
    cheap = select_schema_within_budget(cands, token_budget=250, cost_fn=lambda c: 40)
    assert len(cheap[0]) == 5 and cheap[1] == []


# ----------------------------------------------------------------------------------------------- retrieval wiring


def _ddl(name: str, cols: int) -> str:
    body = [f"  {name}_id int"] + [f"  {name}_c{i} varchar" for i in range(cols)] + ["  deleted_at datetime"]
    return f"TABLE {name} (\n" + ",\n".join(body) + "\n)"


class _Emb:
    async def embed_query(self, q: str) -> tuple[list[float], dict]:
        return [0.0], {}


def _store(tables: dict[str, str], hits: list[str]) -> Any:
    class _Chunk:
        def __init__(self, t: str) -> None:
            self.content, self.document_id, self.db_id, self.metadata = tables[t], f"schema:{DB}", DB, {"db_id": DB}

    class _Hit:
        def __init__(self, t: str) -> None:
            self.chunk = _Chunk(t)

    class _Store:
        async def search_hybrid(self, **_kw: Any) -> list[Any]:
            return [_Hit(t) for t in hits]

    return _Store()


def _world(monkeypatch: pytest.MonkeyPatch, chain_len: int, cols: int, hubs: int, flags: dict[str, bool]) -> tuple[list[str], dict[str, str], str]:
    chain = ["hdr"] + [f"b{i}" for i in range(1, chain_len)] + ["kind"]
    hub_names = [f"hub{i}" for i in range(hubs)]
    others = ["d1", "d2"]
    tables = {t: _ddl(t, cols) for t in chain + hub_names + others}
    edges = list(itertools.pairwise(chain)) + [(t, h) for h in hub_names for t in chain + others]
    raw = [{"from_table": a, "from_column": f"{b}_id", "to_table": b, "to_column": f"{b}_id"} for a, b in edges]
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: raw)
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")
    monkeypatch.setattr(sr, "route_anchor_tables", lambda q, db_id=DB: set())
    monkeypatch.setattr(sr, "is_feature_enabled", lambda f: True)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: flags.get(f, False))
    return chain, tables, "\n\n".join(tables.values())


def _retrieve(tables: dict[str, str], full: str) -> str:
    return asyncio.run(sr.retrieve_schema_from_qdrant("q", full, _store(tables, ["hdr", "kind", "d1", "d2"]), _Emb(), db_id=DB))


def _present(out: str) -> set[str]:
    return set(re.findall(r"(?im)^(?:table\s+)?(\w+)\s*(?:\(|:)", out))


def test_long_route_is_completed_when_the_flag_is_on_and_unchanged_otherwise(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain, tables, full = _world(monkeypatch, chain_len=4, cols=60, hubs=6, flags={})
    _write(dbs, [_pair(chain)])
    off = _retrieve(tables, full)
    assert not set(chain) <= _present(off)  # the failure this phase fixes: bridges are missing from the prompt

    _world(monkeypatch, 4, 60, 6, {"auto_prompt_shadow": True})
    assert _retrieve(tables, full) == off  # shadow mode: logged only, the prompt is byte-for-byte the same

    _world(monkeypatch, 4, 60, 6, {"auto_prompt_inject": True})
    on = _retrieve(tables, full)
    assert set(chain) <= _present(on)  # every table of the route is now shown
    assert "- b1.b2_id -> b2.b2_id" in on and on.count("Join Hints:") == 1  # and so are its join conditions


def test_nothing_changes_without_a_route_file_or_for_the_pinned_database(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tables, full = _world(monkeypatch, 4, 25, 3, {})
    off = _retrieve(tables, full)
    _world(monkeypatch, 4, 25, 3, {"auto_prompt_inject": True, "auto_prompt_shadow": True})
    assert _retrieve(tables, full) == off  # flags on but no join_paths.json: identical to before


def test_a_broken_route_file_never_breaks_retrieval(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tables, full = _world(monkeypatch, 4, 25, 3, {})
    off = _retrieve(tables, full)
    folder = dbs / DB / "schema"
    folder.mkdir(parents=True)
    (folder / "join_paths.json").write_text("{broken", encoding="utf-8")
    _world(monkeypatch, 4, 25, 3, {"auto_prompt_inject": True})
    assert _retrieve(tables, full) == off


def test_a_planner_crash_never_breaks_retrieval(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain, tables, full = _world(monkeypatch, 4, 25, 3, {})
    _write(dbs, [_pair(chain)])
    off = _retrieve(tables, full)
    _world(monkeypatch, 4, 25, 3, {"auto_prompt_inject": True})

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ri, "plan_route_bridges", boom)
    assert _retrieve(tables, full) == off


def test_route_bridge_telemetry_stages_are_kept_and_not_recorded_as_unknown() -> None:
    from src.utils.telemetry import ALLOWED_STAGES

    assert {"route_bridge_shadow", "route_bridge_applied"} <= ALLOWED_STAGES
