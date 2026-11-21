"""Release 3: the connected schema contract (``src/sql/schema_contract.py`` and its wiring in retrieval and the prompt).

Nothing here names a real table or question: the schemas are small synthetic ones.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import src.sql.prompt_builder as pb
import src.sql.schema_contract as sc
import src.sql.schema_retrieval as sr
from src.sql import registry
from src.sql.paths import route_index as ri
from src.sql.schema_token_estimator import estimate_schema_tokens
from tests.sql.relationships_fixtures import documents, table

DB = "contract_db"
ROOT = Path(__file__).resolve().parents[2]
RELS = [
    {"from_table": "orders", "from_column": "plant_id", "to_table": "plant", "to_column": "id"},
    {"from_table": "plant", "from_column": "region_id", "to_table": "region", "to_column": "id"},
    {"from_table": "invoice", "from_column": "order_id", "to_table": "orders", "to_column": "id"},
]


def cand(name: str, source: str = "vector_rag") -> dict[str, Any]:
    return {"table_name": name, "ddl": f"TABLE {name} (\n  id int\n)", "source": source}


# ----------------------------------------------------------------------------------------------- the approved graph


def test_trusted_edges_are_lower_case_undirected_and_skip_noise() -> None:
    rels = [
        {"from_table": "Orders", "from_column": "plant_id", "to_table": "PLANT", "to_column": "id"},
        {"from_table": "orders", "from_column": "x", "to_table": "orders", "to_column": "id"},  # a self-link connects nothing
        {"from_table": "orders", "from_column": "created_id", "to_table": "users", "to_column": "id"},  # audit link: would connect everything
        {"from_table": "orders", "from_column": "buyer_id", "to_table": "users", "to_column": "id"},  # a real link to users stays
        {"from_table": "", "from_column": "a", "to_table": "b", "to_column": "c"},
        "not a dict",  # type: ignore[list-item]
    ]
    edges = sc.trusted_edges(rels, chains=[("A", "B", "C"), ("solo",), ()])
    assert edges == {frozenset({"orders", "plant"}), frozenset({"orders", "users"}), frozenset({"a", "b"}), frozenset({"b", "c"})}
    assert sc.trusted_edges(None) == set()


def test_connected_groups_keep_input_order_and_ignore_edges_to_tables_not_listed() -> None:
    edges = sc.trusted_edges(RELS)
    assert sc.connected_groups(["region", "orders", "vendor", "plant"], edges) == [["region", "orders", "plant"], ["vendor"]]
    assert sc.connected_groups(["orders", "region"], edges) == [["orders"], ["region"]]  # plant is not listed: no route through it
    assert sc.connected_groups(["Orders", "orders"], edges) == [["orders"]]


# ----------------------------------------------------------------------------------------------- 1. drop weak, disconnected matches


def test_a_weak_match_that_nothing_approved_connects_is_dropped_and_a_connected_one_stays() -> None:
    plan = sc.plan_contract([cand("orders"), cand("plant"), cand("vendor"), cand("region"), cand("noise")], RELS)
    assert plan is not None
    # vendor is the 3rd vector match (rank 2): not one of the top two, and nothing connects it
    assert dict(plan.dropped) == {"vendor": "no approved route to the main tables", "noise": "no approved route to the main tables"}
    assert plan.keep == ("orders", "plant", "region") and plan.groups == (("orders", "plant", "region"),)


def test_the_questions_top_vector_matches_stay_even_when_nothing_connects_them() -> None:
    plan = sc.plan_contract([cand("orders"), cand("vendor"), cand("noise")], RELS)
    assert plan is not None and plan.keep == ("orders", "vendor") and [t for t, _ in plan.dropped] == ["noise"]
    assert plan.groups == (("orders",), ("vendor",))  # kept, and reported as two groups: the contract will say they are not connected
    assert sc.plan_contract([cand("orders"), cand("vendor"), cand("noise")], RELS, keep_top=1).keep == ("orders",)


def test_tables_that_are_there_on_purpose_are_never_judged_by_connectivity() -> None:
    cands = [cand("orders"), cand("g1", "domain_anchor"), cand("copy_real", "table_authority"), cand("mid", "route_bridge"), cand("w1"), cand("w2"), cand("w3")]
    plan = sc.plan_contract(cands, RELS)
    assert plan is not None
    assert {"g1", "copy_real", "mid", "w1"} <= set(plan.keep)  # w1 is the second vector match: a subject of the question
    assert [t for t, _ in plan.dropped] == ["w2", "w3"]


def test_the_most_relevant_table_is_never_dropped() -> None:
    plan = sc.plan_contract([cand("zzz"), cand("orders", "graph_expansion"), cand("plant", "graph_expansion")], RELS)
    assert plan is not None and plan.keep == ("zzz",)  # nothing connects the first table, yet it stays
    assert [t for t, _ in plan.dropped] == ["orders", "plant"]  # the connected group of weak tables goes together


def test_a_neighbour_of_a_weak_table_goes_with_it_and_a_neighbour_of_a_kept_one_stays() -> None:
    rels = [*RELS, {"from_table": "p2", "from_column": "a", "to_table": "x_nb", "to_column": "b"}]
    cands = [cand("orders"), cand("p1"), cand("p2"), cand("invoice", "graph_expansion"), cand("x_nb", "graph_expansion")]
    plan = sc.plan_contract(cands, rels)
    assert plan is not None
    assert set(plan.keep) == {"orders", "p1", "invoice"}  # p1 is the second vector match; invoice joins orders
    assert dict(plan.dropped).keys() == {"p2", "x_nb"}  # p2 is weak and unconnected; its only neighbour leaves with it


def test_without_approved_joins_nothing_can_be_judged() -> None:
    assert sc.plan_contract([cand("a"), cand("b")], []) is None
    assert sc.plan_contract([cand("a"), cand("b")], None) is None
    assert sc.plan_contract([], RELS) is None
    assert sc.plan_contract([{"table_name": ""}], RELS) is None


def test_routes_count_as_connections_and_the_plan_is_deterministic() -> None:
    other = [{"from_table": "x", "from_column": "y", "to_table": "z", "to_column": "w"}]
    cands = [cand("a"), cand("b"), cand("c"), cand("d"), cand("m", "route_bridge")]
    plan = sc.plan_contract(cands, other, chains=[("a", "m", "d")])
    assert plan is not None and {"a", "m", "d"} <= set(plan.keep)  # connected by a route's steps although no relationship names them
    assert set(plan.groups[0]) == {"a", "m", "d"}
    assert [t for t, _ in plan.dropped] == ["c"]  # b is the second vector match (kept); c is weak and unconnected
    assert sc.plan_contract(cands, other, chains=[("a", "m", "d")]) == plan  # deterministic
    # a route through a table that is NOT shown connects nothing: its middle is missing
    cut = sc.plan_contract([cand("a"), cand("b"), cand("c"), cand("d")], other, chains=[("a", "m", "d")])
    assert cut is not None and set(cut.keep) == {"a", "b"} and [t for t, _ in cut.dropped] == ["c", "d"]


def test_candidate_names_are_matched_in_lower_case_and_repeats_are_ignored() -> None:
    plan = sc.plan_contract([cand("Orders"), cand("ORDERS"), cand("PLANT"), cand("w1"), cand("w2"), cand("w3")], RELS)
    assert plan is not None and plan.keep == ("orders", "plant") and [t for t, _ in plan.dropped] == ["w1", "w2", "w3"]


# ----------------------------------------------------------------------------------------------- 2. say what is not connected


def test_the_note_is_empty_when_everything_shown_is_connected_and_names_the_groups_otherwise() -> None:
    assert sc.unconnected_note(["orders", "plant", "region"], RELS) == ""
    note = sc.unconnected_note(["orders", "plant", "vendor", "region", "stock"], RELS)
    assert note == "No approved route connects these groups: (orders, plant, region) | (vendor) | (stock). Do not join across groups."


def test_a_route_cut_by_the_budget_is_not_a_route() -> None:
    chain = ("hdr", "mid", "kind")
    assert sc.unconnected_note(["hdr", "kind"], [], chains=[chain]) == ""  # no approved graph at all: cannot say
    other = [{"from_table": "q", "from_column": "a", "to_table": "r", "to_column": "b"}]
    assert "(hdr) | (kind)" in sc.unconnected_note(["hdr", "kind"], other, chains=[chain])  # mid was dropped: the route is gone
    assert sc.unconnected_note(["hdr", "mid", "kind"], other, chains=[chain]) == ""  # whole route shown: connected


def test_the_note_is_bounded_and_can_never_be_read_as_a_table_by_the_schema_parsers() -> None:
    names = [f"t{i}" for i in range(40)]
    note = sc.unconnected_note(names, [{"from_table": "t0", "from_column": "a", "to_table": "t1", "to_column": "b"}])
    assert note.count("|") == sc.MAX_NOTE_GROUPS - 1 and "more groups" in note and len(note) < 400
    from src.sql.safety.sql_safety import _normalize_schema_context

    assert pb._prompt_table_names(note) == set() and sr.extract_schema_table_names(note) == set() and _normalize_schema_context(note) == {}
    big = [f"a{i}" for i in range(20)]
    rels = [{"from_table": "a0", "from_column": "x", "to_table": f"a{i}", "to_column": "y"} for i in range(1, 20)]
    assert "+14 more" in sc.unconnected_note([*big, "other"], rels)
    assert sc.unconnected_note(None, RELS) == ""  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------------------- 3. unique keys


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    sc.clear_contract_cache()
    ri.clear_route_index_cache()
    yield tmp_path
    sc.clear_contract_cache()
    ri.clear_route_index_cache()


def _catalog(root: Path, *tables: dict[str, Any]) -> None:
    schema_doc, inventory, _ = documents(*tables)
    folder = root / DB / "schema"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(json.dumps(schema_doc), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")


def test_single_column_unique_keys_are_marked_and_nothing_else_changes(dbs: Path) -> None:
    INT, TXT = "INTEGER", "VARCHAR(30)"
    _catalog(
        dbs,
        table("po", {"id": INT, "po_no": TXT, "a": INT, "b": INT, "note": TXT}, pk=("id",), unique=(("po_no",), ("a", "b"))),
        table("plant", {"id": INT, "code": TXT}, pk=("id",), unique=(("id",),)),  # a primary key that is also unique stays PK only
        table("plain", {"id": INT, "x": TXT}, pk=("id",)),
    )
    assert sc.unique_columns(DB) == {"po": frozenset({"po_no"})}  # composite keys and primary keys are not marked
    lines = ["po: id(int, PK), po_no(varchar), a(int), b(int), note(varchar)", "plain: id(int, PK), x(varchar)", "Join Hints:\n- po.a -> plain.id"]
    out = sc.mark_unique_keys(lines, DB)
    assert out[0] == "po: id(int, PK), po_no(varchar, UQ), a(int), b(int), note(varchar)"
    assert out[1:] == lines[1:]
    assert sc.mark_unique_keys(out, DB) == out  # idempotent
    assert sc.mark_unique_keys(["po: po_no(varchar, FK->x.y)"], DB) == ["po: po_no(varchar, FK->x.y, UQ)"]
    raw = "TABLE po (\n  po_no varchar\n)"
    assert sc.mark_unique_keys([raw], DB) == [raw]  # a table the compactor could not parse is left exactly as it was


def test_unique_keys_follow_the_catalog_and_a_missing_catalog_changes_nothing(dbs: Path) -> None:
    lines = ["po: id(int, PK), po_no(varchar)"]
    assert sc.mark_unique_keys(lines, DB) == lines and sc.unique_columns(DB) == {}
    _catalog(dbs, table("po", {"id": "INTEGER", "po_no": "VARCHAR(9)"}, pk=("id",), unique=(("po_no",),)))
    assert sc.mark_unique_keys(lines, DB) == ["po: id(int, PK), po_no(varchar, UQ)"]
    (dbs / DB / "schema" / "schema.json").write_text("{broken", encoding="utf-8")
    assert sc.mark_unique_keys(lines, DB) == lines  # rebuilt because the file changed; a broken file means no marks, no error
    assert sc.unique_columns("../escape") == {}


def test_unique_marks_never_break_the_validators_that_read_compact_lines() -> None:
    from src.sql.safety.sql_safety import _normalize_schema_context

    line = "po: id(int, PK), po_no(varchar, UQ), buyer_id(int, FK->buyer.id, UQ)"
    assert _normalize_schema_context(line) == {"po": {"id", "po_no", "buyer_id"}}


# ----------------------------------------------------------------------------------------------- when it runs


def _flags(monkeypatch: pytest.MonkeyPatch, **on: bool) -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: on.get(f, False))


@pytest.mark.parametrize(
    ("flags", "mode"),
    [
        ({}, ""),
        ({"auto_schema_contract_apply": True}, ""),  # without the Phase 12 flags there is nothing to apply
        ({"auto_prompt_shadow": True}, "shadow"),
        ({"auto_prompt_inject": True}, "shadow"),  # route bridges alone never switch the contract on
        ({"auto_prompt_shadow": True, "auto_schema_contract_apply": True}, "shadow"),  # no bridges applied: routes could be cut, so no pruning
        ({"auto_prompt_inject": True, "auto_schema_contract_apply": True}, "apply"),
    ],
)
def test_mode_follows_the_flags(monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool], mode: str) -> None:
    _flags(monkeypatch, **flags)
    assert sc.contract_mode(DB) == mode


def test_the_contract_needs_approved_joins_and_the_pinned_database_is_never_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, auto_prompt_inject=True, auto_schema_contract_apply=True)
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    assert sc.contract_mode(DB) == "apply" and sc.contract_active(DB) is False  # no approved joins: the old wording stays
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: RELS)
    assert sc.contract_active(DB) is True
    from src.utils import feature_flags as ff

    monkeypatch.undo()  # the real flag reader: erp_main is pinned off whatever the file says
    assert ff.is_db_feature_enabled("auto_schema_contract_apply", "erp_main") is False and sc.contract_mode("erp_main") == ""


def test_a_flag_reader_that_fails_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", boom)
    assert sc.contract_mode(DB) == "" and sc.contract_active(DB) is False


# ----------------------------------------------------------------------------------------------- retrieval wiring


def _ddl(name: str, cols: int = 3) -> str:
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


CHAIN = ["hdr", "b1", "b2", "kind"]
NOISE = ["n1", "n2", "n3", "n4"]


def _world(monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool], *, extra_rels: list[dict[str, Any]] | None = None) -> tuple[dict[str, str], str, list[dict]]:
    tables = {t: _ddl(t) for t in [*CHAIN, *NOISE, "d1", "d2"]}
    raw = [{"from_table": a, "from_column": f"{b}_id", "to_table": b, "to_column": f"{b}_id"} for a, b in itertools.pairwise(CHAIN)]
    raw += [{"from_table": "hdr", "from_column": "d1_id", "to_table": "d1", "to_column": "d1_id"}, *(extra_rels or [])]
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: raw)
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")
    monkeypatch.setattr(sr, "route_anchor_tables", lambda q, db_id=DB: set())
    monkeypatch.setattr(sr, "is_feature_enabled", lambda f: True)
    _flags(monkeypatch, **flags)
    return tables, "\n\n".join(tables.values()), raw


def _write_routes(root: Path) -> None:
    joins = [{"from": f"{a}.{b}_id", "to": f"{b}.{b}_id", "confidence": 0.9, "evidence": ["declared"], "direction": "many_to_one"} for a, b in itertools.pairwise(CHAIN)]
    pair = {"a": "hdr", "b": "kind", "hops": 3, "score": 0.8, "tier": "near", "routes": [{"score": 0.8, "shape": "chain", "tables": CHAIN, "joins": joins}]}
    folder = root / DB / "schema"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "join_paths.json").write_text(json.dumps({"generated_by": "auto_join_paths", "pairs": [pair]}), encoding="utf-8")


def _retrieve(tables: dict[str, str], full: str, hits: list[str]) -> str:
    return asyncio.run(sr.retrieve_schema_from_qdrant("q", full, _store(tables, hits), _Emb(), db_id=DB))


def _present(out: str) -> set[str]:
    return set(re.findall(r"(?im)^(?:table\s+)?(\w+)\s*(?:\(|:)", out))


APPLY = {"auto_prompt_inject": True, "auto_schema_contract_apply": True}


def test_a_route_is_completed_and_weak_disconnected_matches_are_not_shown(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    hits = ["hdr", "kind", "n1", "n2", "n3"]
    tables, full, _ = _world(monkeypatch, {"auto_prompt_inject": True})
    before = _retrieve(tables, full, hits)
    assert set(CHAIN) <= _present(before) and {"n1", "n2", "n3"} <= _present(before)  # the failure this release closes: noise sits beside the route

    tables, full, _ = _world(monkeypatch, APPLY)
    after = _retrieve(tables, full, hits)
    assert set(CHAIN) <= _present(after)  # multi-hop: both ends and the bridge tables between them
    assert not ({"n1", "n2", "n3"} & _present(after))  # the weak matches nothing approved connects are gone
    assert "No approved route" not in after  # everything shown is one connected group
    assert "- b1.b2_id -> b2.b2_id" in after  # the route's join conditions are still shown


def test_a_second_subject_nothing_connects_stays_but_the_model_is_told_it_is_unconnected(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    tables, full, _ = _world(monkeypatch, APPLY)
    out = _retrieve(tables, full, ["hdr", "n1", "n2"])  # n1 is the second vector match: a subject of the question, kept
    assert {"hdr", "n1"} <= _present(out) and "n2" not in _present(out)
    assert re.search(r"No approved route connects these groups: \(.*hdr.*\) \| \(n1\)\. Do not join across groups\.", out)
    assert pb._prompt_table_names(out) >= {"hdr", "n1"}  # and the note itself added no phantom table


def test_unique_keys_are_marked_in_the_shown_tables(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    _catalog(dbs, table("hdr", {"hdr_id": "INTEGER", "hdr_c0": "VARCHAR(9)", "deleted_at": "DATETIME"}, pk=("hdr_id",), unique=(("hdr_c0",),)))
    tables, full, _ = _world(monkeypatch, APPLY)
    assert "hdr_c0(varchar, UQ)" in _retrieve(tables, full, ["hdr", "kind"])
    tables, full, _ = _world(monkeypatch, {"auto_prompt_inject": True})
    assert "UQ" not in _retrieve(tables, full, ["hdr", "kind"])


def test_shadow_mode_changes_nothing_and_only_logs_what_would_be_dropped(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    hits = ["hdr", "kind", "n1", "n2", "n3"]
    tables, full, _ = _world(monkeypatch, {"auto_prompt_inject": True})
    base = _retrieve(tables, full, hits)
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(sr, "log_telemetry", lambda **kw: seen.append((kw.get("stage", ""), kw.get("extra") or {})))
    assert _retrieve(tables, full, hits) == base  # byte for byte: the contract flag is off
    shadow = [e for stage, e in seen if stage == "schema_contract_shadow"]
    assert shadow and {t for t, _ in shadow[0]["dropped"]} == {"n1", "n2", "n3"}
    assert not [1 for stage, _ in seen if stage == "schema_contract_applied"]


def test_with_every_flag_off_or_no_approved_joins_nothing_changes(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    hits = ["hdr", "kind", "n1", "n2", "n3"]
    tables, full, _ = _world(monkeypatch, {})
    off = _retrieve(tables, full, hits)
    tables, full, _ = _world(monkeypatch, {"auto_schema_contract_apply": True})  # without the Phase 12 flags: nothing runs
    assert _retrieve(tables, full, hits) == off
    tables, full, _ = _world(monkeypatch, APPLY)
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    on_without_graph = _retrieve(tables, full, hits)
    assert {"n1", "n2", "n3"} <= _present(on_without_graph)  # nothing can be judged without approved joins: nothing is dropped


def test_without_compaction_the_contract_does_not_run(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    hits = ["hdr", "kind", "n1", "n2", "n3"]
    tables, full, _ = _world(monkeypatch, APPLY)
    monkeypatch.setattr(sr, "is_feature_enabled", lambda f: f != "schema_compaction_enabled")
    out = _retrieve(tables, full, hits)
    assert {"n1", "n2", "n3"} <= _present(out)  # the note and the marks ride in the compact text: no compaction, no contract


def test_a_planner_crash_never_breaks_retrieval(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    hits = ["hdr", "kind", "n1", "n2", "n3"]
    tables, full, _ = _world(monkeypatch, {"auto_prompt_inject": True})
    base = _retrieve(tables, full, hits)
    tables, full, _ = _world(monkeypatch, APPLY)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sc, "plan_contract", boom)
    assert _retrieve(tables, full, hits) == base  # the contract is an extra: retrieval is exactly what it was


def test_the_note_and_marks_failing_never_break_the_prompt(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_routes(dbs)
    tables, full, _ = _world(monkeypatch, APPLY)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sc, "unconnected_note", boom)
    out = _retrieve(tables, full, ["hdr", "kind"])
    assert set(CHAIN) <= _present(out) and "No approved route" not in out


def test_contract_telemetry_stages_are_registered() -> None:
    from src.utils.telemetry import ALLOWED_STAGES

    assert {"schema_contract_shadow", "schema_contract_applied"} <= ALLOWED_STAGES


# ----------------------------------------------------------------------------------------------- the prompt

_DIALECT = SimpleNamespace(name="T-SQL", date_functions="")
_COMPACT = "orders: id(int, PK), plant_id(int, FK->plant.id)\n\nplant: id(int, PK), name(varchar)"


@pytest.fixture()
def prompt_env(monkeypatch: pytest.MonkeyPatch):
    pb._RAW_RELATIONSHIPS_CACHE[DB] = RELS
    yield monkeypatch
    pb._RAW_RELATIONSHIPS_CACHE.pop(DB, None)


def _prompt(schema: str, relationships: str = "") -> str:
    return pb.build_sql_prompt("orders per plant", schema, _DIALECT, db_id=DB, relationships=relationships)


def test_the_prompt_states_the_three_rules_only_where_the_contract_applies(prompt_env: pytest.MonkeyPatch) -> None:
    _flags(prompt_env)
    assert "Schema contract:" not in _prompt(_COMPACT)
    _flags(prompt_env, auto_prompt_inject=True)  # Phase 12 apply alone: the contract flag is what is being tested
    off = _prompt(_COMPACT)
    assert "Schema contract:" not in off
    _flags(prompt_env, **APPLY)
    on = _prompt(_COMPACT)
    for line in (
        "Use only the listed tables, columns and approved joins",
        "Do not invent tables, columns or joins.",
        "If no approved route is present, do not guess",
    ):
        assert line in on
    assert on.replace(pb._GROUNDING_RULES, "") == off  # the only difference is the block: nothing else in the prompt moved
    assert "NO_SQL" in on and on.index("Table relationships:") < on.index("Schema contract:")


def test_the_whole_database_relationship_map_is_never_offered_in_place_of_the_listed_joins(prompt_env: pytest.MonkeyPatch) -> None:
    whole = "\n".join(f"- other_{i}: ref_id->other_{i + 1}.id" for i in range(50))
    isolated = "vendor: id(int, PK)\n\nstock: id(int, PK)"
    _flags(prompt_env)
    assert "other_5" in _prompt(isolated, whole)  # today: a small map is offered although it names tables that are not listed
    _flags(prompt_env, **APPLY)
    on = _prompt(isolated, whole)
    assert "other_5" not in on and "Table relationships:" not in on
    assert "No approved joins exist among the listed tables" in on  # and the model is told what that means


def test_the_no_joins_line_needs_several_tables_and_no_join_hints(prompt_env: pytest.MonkeyPatch) -> None:
    _flags(prompt_env, **APPLY)
    assert "No approved joins exist" not in _prompt("vendor: id(int, PK)")  # one table: nothing to join
    hinted = "a: id(int, PK)\n\nb: id(int, PK)\n\nJoin Hints:\n- a.id -> b.id"  # a route's join lines count as approved joins
    assert "No approved joins exist" not in _prompt(hinted)
    assert "No approved joins exist" in _prompt("a: id(int, PK)\n\nb: id(int, PK)")


def test_the_grounding_block_is_bounded() -> None:
    assert estimate_schema_tokens(pb._grounding_block(False, 3)) < 130
    assert pb._grounding_block(True, 3) == pb._GROUNDING_RULES


def test_without_approved_joins_the_prompt_keeps_its_old_wording(prompt_env: pytest.MonkeyPatch) -> None:
    pb._RAW_RELATIONSHIPS_CACHE[DB] = []
    _flags(prompt_env, **APPLY)
    assert "Schema contract:" not in _prompt(_COMPACT)  # the strict wording would starve a database whose joins are not discovered yet


def test_a_contract_state_that_cannot_be_read_leaves_the_prompt_alone(prompt_env: pytest.MonkeyPatch) -> None:
    _flags(prompt_env)
    off = _prompt(_COMPACT)

    def boom(_db: str) -> bool:
        raise RuntimeError("boom")

    prompt_env.setattr(sc, "contract_active", boom)
    assert _prompt(_COMPACT) == off


# ----------------------------------------------------------------------------------------------- bounded for hundreds of tables


def test_the_prompt_stays_bounded_on_a_database_with_hundreds_of_tables(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sql.schema_budget import DEFAULT_SCHEMA_TOKEN_BUDGET

    names = [f"t{i:03d}" for i in range(400)]
    tables = {t: _ddl(t, cols=40) for t in names}  # wide tables: the raw schema is hundreds of thousands of tokens
    full = "\n\n".join(tables.values())
    rels = [{"from_table": a, "from_column": f"{b}_id", "to_table": b, "to_column": f"{b}_id"} for a, b in itertools.pairwise(names[:30])]
    rels += [{"from_table": names[i], "from_column": "x_id", "to_table": names[i + 1], "to_column": "x_id"} for i in range(100, 399)]
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: rels)
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")
    monkeypatch.setattr(sr, "route_anchor_tables", lambda q, db_id=DB: set())
    monkeypatch.setattr(sr, "is_feature_enabled", lambda f: True)
    _flags(monkeypatch, **APPLY)
    hits = [names[5], names[250], names[100], names[7], names[399], names[60], names[300], names[200]]
    out = asyncio.run(sr.retrieve_schema_from_qdrant("q", full, _store(tables, hits), _Emb(), db_id=DB))
    assert estimate_schema_tokens(out) <= DEFAULT_SCHEMA_TOKEN_BUDGET + 400  # the budget, plus the note and join lines
    assert 1 <= len(_present(out)) < 30  # a handful of tables out of four hundred
    pb._RAW_RELATIONSHIPS_CACHE[DB] = rels
    try:
        prompt = pb.build_sql_prompt("q", out, _DIALECT, db_id=DB, relationships="\n".join(f"- {r['from_table']}: x" for r in rels))
    finally:
        pb._RAW_RELATIONSHIPS_CACHE.pop(DB, None)
    section = prompt.split("Table relationships:", 1)[1].split("\n\n", 1)[0] if "Table relationships:" in prompt else ""
    assert len(section) < 3000 and "Schema contract:" in prompt  # only joins among the shown tables, never the 330-edge map


# ----------------------------------------------------------------------------------------------- the flag


def test_flag_is_registered_off_by_default_and_documented() -> None:
    from src.utils.feature_flags import DB_FEATURE_FLAGS, DEFAULT_NEW_DB_FLAGS, OFF_BY_DEFAULT_FLAGS

    assert "auto_schema_contract_apply" in DB_FEATURE_FLAGS and "auto_schema_contract_apply" in OFF_BY_DEFAULT_FLAGS
    assert DEFAULT_NEW_DB_FLAGS["auto_schema_contract_apply"] is False
    cfg = yaml.safe_load((ROOT / "config" / "feature_flags.example.yaml").read_text(encoding="utf-8"))
    assert cfg["db_defaults"]["auto_schema_contract_apply"] is False
    assert cfg["db_features"]["_example_db"]["auto_schema_contract_apply"] is False
