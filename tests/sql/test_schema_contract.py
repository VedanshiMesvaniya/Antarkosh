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
import src.sql.retrieval_terms as rt
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
    rt.clear_retrieval_terms_cache()
    yield tmp_path
    sc.clear_contract_cache()
    ri.clear_route_index_cache()
    rt.clear_retrieval_terms_cache()


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


# ----------------------------------------------------------------------------------------------- 6. evidence, minimal route, country filter
#
# General rules only. Names such as supplier, scrap or Germany are invented examples; nothing here depends on one database.


def test_a_table_the_question_names_is_never_dropped_because_it_sits_in_another_group() -> None:
    cands = [cand("orders"), cand("plant"), cand("w1"), cand("w2"), cand("gen_supplier_master")]
    old = sc.plan_contract(cands, RELS)
    assert old is not None and "gen_supplier_master" in dict(old.dropped)  # the failure: the table the user asked for vanished
    plan = sc.plan_contract(cands, RELS, evidence={"gen_supplier_master": 3.0})
    assert plan is not None
    assert "gen_supplier_master" in plan.keep and not {"gen_supplier_master"} & dict(plan.dropped).keys()
    assert dict(plan.dropped).keys() == {"w1", "w2"}  # weak tables with no evidence and no route still go
    assert sc.plan_contract(cands, RELS, protected={"gen_supplier_master"}) is not None  # a route end is protected the same way


def test_without_evidence_the_plan_is_exactly_what_it_was() -> None:
    cands = [cand("orders"), cand("plant"), cand("vendor"), cand("region"), cand("noise")]
    base = sc.plan_contract(cands, RELS)
    assert sc.plan_contract(cands, RELS, evidence=None, protected=()) == base
    assert sc.plan_contract(cands, RELS, evidence={}) == base  # an empty evidence map changes nothing either


def test_the_first_table_is_never_dropped_even_when_the_evidence_is_elsewhere() -> None:
    cands = [cand("zzz", "graph_expansion"), cand("orders"), cand("plant")]
    plan = sc.plan_contract(cands, RELS, evidence={"orders": 3.0, "plant": 2.0}, keep_top=0)
    assert plan is not None and "zzz" in plan.keep and "orders" in plan.keep


def test_the_main_group_is_the_one_with_the_most_evidence() -> None:
    rels = [*RELS, {"from_table": "x1", "from_column": "a", "to_table": "x2", "to_column": "b"}]
    cands = [cand("x1"), cand("x2"), cand("orders"), cand("plant"), cand("w1")]
    plan = sc.plan_contract(cands, rels, evidence={"orders": 3.0, "plant": 2.0}, keep_top=0)
    assert plan is not None and set(plan.keep) == {"x1", "x2", "orders", "plant"}  # x1 is first, so it stays; w1 has nothing
    assert dict(plan.dropped).keys() == {"w1"}


def _neighbour_world(n: int = 8) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    rels = [
        {"from_table": "scrap", "from_column": "part_id", "to_table": "part", "to_column": "id"},
        {"from_table": "part", "from_column": "commodity_id", "to_table": "commodity", "to_column": "id"},
        *({"from_table": f"n{i}", "from_column": "part_id", "to_table": "part", "to_column": "id"} for i in range(n)),
    ]
    cands = [cand("scrap"), cand("commodity"), cand("part", "graph_expansion"), *(cand(f"n{i}", "graph_expansion") for i in range(n))]
    return cands, rels


def test_the_contract_keeps_a_small_connected_route_not_every_neighbour_of_a_big_graph() -> None:
    cands, rels = _neighbour_world()
    everything = sc.plan_contract(cands, rels)
    assert everything is not None and len(everything.keep) == 11 and not everything.dropped  # old: the whole connected graph
    plan = sc.plan_contract(cands, rels, evidence={"scrap": 2.0, "commodity": 3.0})
    assert plan is not None
    assert {"scrap", "part", "commodity"} <= set(plan.keep)  # both ends and the table between them: the smallest approved route
    assert len(plan.keep) == 3 + sc.MAX_LOOSE_NEIGHBORS  # plus a few neighbours only
    assert all(why == "not on the approved route between the requested tables" for _t, why in plan.dropped)
    assert plan.groups == (tuple(plan.keep),)  # and what is left is one connected group


def test_neighbours_with_evidence_are_not_loose_and_nothing_is_cut_when_the_named_tables_are_not_connected() -> None:
    cands, rels = _neighbour_world()
    plan = sc.plan_contract(cands, rels, evidence={"scrap": 2.0, "commodity": 3.0, "n7": 1.0})
    assert plan is not None and "n7" in plan.keep and "n0" in plan.keep
    apart = [cand("scrap"), cand("lone", "vector_rag"), cand("part", "graph_expansion"), *(cand(f"n{i}", "graph_expansion") for i in range(8))]
    cut = sc.plan_contract(apart, rels, evidence={"scrap": 2.0, "lone": 3.0})
    assert cut is not None and {f"n{i}" for i in range(8)} <= set(cut.keep)  # two named tables nothing joins: no route to shrink to


def test_route_tables_finds_the_shortest_approved_route_and_gives_up_when_there_is_none() -> None:
    edges = sc.trusted_edges(RELS)
    within = {"orders", "plant", "region", "invoice", "island"}
    assert sc.route_tables(["invoice", "region"], edges, within) == {"invoice", "orders", "plant", "region"}
    assert sc.route_tables(["invoice", "island"], edges, within) is None  # nothing approved reaches it
    assert sc.route_tables(["invoice", "region"], edges, {"invoice", "region"}) is None  # the middle is not among the chosen tables
    assert sc.route_tables(["orders"], edges, within) is None and sc.route_tables([], edges, within) is None


SUPPLIER_RELS = [{"from_table": "sup_address", "from_column": "supplier_id", "to_table": "gen_supplier_master", "to_column": "supplier_id"}]
PLACES = {"sup_address": 0, "plant_site": 1}
EXIST = {"gen_supplier_master", "sup_address", "plant_site", "gen_supplier_codes"}


def test_a_named_country_is_routed_from_the_entity_through_approved_joins_only() -> None:
    route = sc.plan_filter_route(["gen_supplier_master"], PLACES, SUPPLIER_RELS, available=EXIST, present={"gen_supplier_master"})
    assert route is not None and route.tables == ("gen_supplier_master", "sup_address") and route.add == ("sup_address",)
    assert sc.plan_filter_route(["gen_supplier_master"], PLACES, SUPPLIER_RELS, available=EXIST, present=EXIST) is not None  # already there: nothing to add
    assert sc.plan_filter_route(["gen_supplier_master"], PLACES, SUPPLIER_RELS, available=EXIST, present=EXIST).add == ()  # type: ignore[union-attr]
    # no approved join, no route: a table that merely has a country column is never joined by guesswork
    assert sc.plan_filter_route(["gen_supplier_codes"], PLACES, SUPPLIER_RELS, available=EXIST) is None
    assert sc.plan_filter_route(["gen_supplier_master"], PLACES, [], available=EXIST) is None
    assert sc.plan_filter_route(["gen_supplier_master"], PLACES, SUPPLIER_RELS, available={"gen_supplier_master"}) is None  # the table is not in the live schema
    # the entity that holds the country itself needs no route; nothing to route gives nothing
    assert sc.plan_filter_route(["sup_address"], PLACES, SUPPLIER_RELS, available=EXIST) is None
    assert sc.plan_filter_route([], PLACES, SUPPLIER_RELS, available=EXIST) is None and sc.plan_filter_route(["x"], {}, SUPPLIER_RELS, available=EXIST) is None


def test_a_table_that_holds_a_country_beats_one_that_only_holds_an_address_and_a_long_route_is_capped() -> None:
    rels = [
        *SUPPLIER_RELS,
        {"from_table": "gen_supplier_master", "from_column": "site_id", "to_table": "plant_site", "to_column": "id"},
        {"from_table": "sup_address", "from_column": "geo_id", "to_table": "geo_country", "to_column": "id"},
    ]
    places = {"geo_country": 0, "plant_site": 1}
    got = sc.plan_filter_route(["gen_supplier_master"], places, rels, available=EXIST | {"geo_country"}, present={"gen_supplier_master"})
    assert got is not None and got.place == "plant_site"  # geo_country is two joins away and no Phase 8 route vouches for it: only the direct join is used

    class _Index:
        def best(self, a: str, b: str) -> ri.IndexedRoute | None:
            if (a, b) == ("gen_supplier_master", "geo_country"):
                return ri.IndexedRoute(("gen_supplier_master", "sup_address", "geo_country"), 0.8, "chain",
                                       (("gen_supplier_master.supplier_id", "sup_address.supplier_id"), ("sup_address.geo_id", "geo_country.id")))
            return None

    got = sc.plan_filter_route(["gen_supplier_master"], places, rels, available=EXIST | {"geo_country"}, present={"gen_supplier_master"}, index=_Index())
    assert got is not None and got.place == "geo_country" and got.add == ("sup_address", "geo_country")  # the country table wins over the nearer address table
    assert got.hint_lines == ("- gen_supplier_master.supplier_id -> sup_address.supplier_id", "- sup_address.geo_id -> geo_country.id")
    capped = sc.plan_filter_route(["gen_supplier_master"], places, rels, available=EXIST | {"geo_country"}, present={"gen_supplier_master"}, index=_Index(), max_add=1)
    assert capped is not None and capped.place == "plant_site"  # a route that would add too many tables is skipped for the direct join
    weak = type("W", (), {"best": lambda self, a, b: ri.IndexedRoute((a, "m", b), 0.2, "chain", ((a, "m"), ("m", b)))})()
    assert sc.plan_filter_route(["gen_supplier_master"], {"geo_country": 0}, rels, available=EXIST | {"geo_country"}, index=weak) is None  # a weak route is no route


# ---- through retrieval


def _supplier_catalog(root: Path, *, address_has_supplier_id: bool = True) -> list[dict[str, Any]]:
    addr = {"addr_id": "int", "country_name": "varchar"} | ({"supplier_id": "int"} if address_has_supplier_id else {"link_id": "int"})
    _catalog(
        root,
        table("gen_supplier_master", {"supplier_id": "int", "supplier_name": "varchar"}, pk=("supplier_id",)),
        table("gen_supplier_codes", {"code_id": "int", "code_value": "varchar", "plant_id": "int"}, pk=("code_id",)),
        table("plant_master", {"plant_id": "int", "plant_name": "varchar"}, pk=("plant_id",)),
        table("sup_address", addr, pk=("addr_id",)),
        table("sup_link", {"link_id": "int", "supplier_id": "int", "geo_id": "int"}, pk=("link_id",)),
        table("geo_country", {"geo_id": "int", "country_name": "varchar"}, pk=("geo_id",)),
        *(table(f"n{i}", {"nid": "int", "nval": "varchar"}, pk=("nid",)) for i in range(1, 4)),
    )
    return [
        {"from_table": "gen_supplier_codes", "from_column": "plant_id", "to_table": "plant_master", "to_column": "plant_id"},
        {"from_table": "sup_address" if address_has_supplier_id else "sup_link", "from_column": "supplier_id", "to_table": "gen_supplier_master", "to_column": "supplier_id"},
        {"from_table": "sup_link", "from_column": "geo_id", "to_table": "geo_country", "to_column": "geo_id"},
    ]


def _supplier_world(monkeypatch: pytest.MonkeyPatch, raw: list[dict[str, Any]], flags: dict[str, bool]) -> tuple[dict[str, str], str]:
    names = ["gen_supplier_master", "gen_supplier_codes", "plant_master", "sup_address", "sup_link", "geo_country", "n1", "n2", "n3"]
    tables = {t: _ddl(t) for t in names}
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: raw)
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")
    monkeypatch.setattr(sr, "route_anchor_tables", lambda q, db_id=DB: set())
    monkeypatch.setattr(sr, "is_feature_enabled", lambda f: True)
    _flags(monkeypatch, **flags)
    return tables, "\n\n".join(tables.values())


def _ask(query: str, tables: dict[str, str], full: str, hits: list[str]) -> str:
    return asyncio.run(sr.retrieve_schema_from_qdrant(query, full, _store(tables, hits), _Emb(), db_id=DB))


HITS = ["gen_supplier_codes", "plant_master", "n1", "n2", "gen_supplier_master"]
SUPPLIER_APPLY = {**APPLY, "auto_retrieval_apply": True}


def test_the_entity_the_question_names_survives_and_its_country_table_comes_with_it(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _supplier_catalog(dbs)
    tables, full = _supplier_world(monkeypatch, raw, APPLY)
    before = _ask("suppliers from Germany", tables, full, HITS)
    assert "gen_supplier_master" not in _present(before)  # the bug: a vector hit in its own graph component was pruned away

    tables, full = _supplier_world(monkeypatch, raw, SUPPLIER_APPLY)
    after = _ask("suppliers from Germany", tables, full, HITS)
    assert {"gen_supplier_master", "sup_address", "gen_supplier_codes"} <= _present(after)
    assert not {"n1", "n2"} & _present(after)  # weak matches with no evidence and no route are still dropped
    assert "germany" not in after.lower()  # the country name never reaches the prompt


def test_a_missing_country_table_is_added_only_through_an_approved_route_and_its_joins_are_shown(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _supplier_catalog(dbs, address_has_supplier_id=False)
    folder = dbs / DB / "schema"
    chain = ["gen_supplier_master", "sup_link", "geo_country"]
    joins = [
        {"from": "gen_supplier_master.supplier_id", "to": "sup_link.supplier_id", "confidence": 0.9, "evidence": ["declared"], "direction": "many_to_one"},
        {"from": "sup_link.geo_id", "to": "geo_country.geo_id", "confidence": 0.9, "evidence": ["declared"], "direction": "many_to_one"},
    ]
    pair = {"a": chain[0], "b": chain[-1], "hops": 2, "score": 0.8, "tier": "near", "routes": [{"score": 0.8, "shape": "chain", "tables": chain, "joins": joins}]}
    (folder / "join_paths.json").write_text(json.dumps({"generated_by": "auto_join_paths", "pairs": [pair]}), encoding="utf-8")
    tables, full = _supplier_world(monkeypatch, raw, SUPPLIER_APPLY)
    out = _ask("how many suppliers from Germany", tables, full, ["gen_supplier_master", "gen_supplier_codes", "plant_master", "n1"])
    assert {"gen_supplier_master", "sup_link", "geo_country"} <= _present(out)  # country table reached by the approved route
    assert "- sup_link.geo_id -> geo_country.geo_id" in out  # and how to join it
    no_route = _ask("how many suppliers", tables, full, ["gen_supplier_master", "gen_supplier_codes", "plant_master", "n1"])
    assert "- sup_link.geo_id -> geo_country.geo_id" not in no_route  # no country named: no country route, no route joins

    (folder / "join_paths.json").unlink()
    ri.clear_route_index_cache()
    raw_without_link = [r for r in raw if r["from_table"] != "sup_link"]  # no approved join to the country table at all
    tables, full = _supplier_world(monkeypatch, raw_without_link, SUPPLIER_APPLY)
    nothing = _ask("how many suppliers from Germany", tables, full, ["gen_supplier_master", "gen_supplier_codes", "plant_master", "n1"])
    assert "geo_country" not in _present(nothing)  # a country column alone never creates a join


def test_shadow_mode_only_logs_the_country_route_and_the_evidence(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _supplier_catalog(dbs, address_has_supplier_id=False)
    tables, full = _supplier_world(monkeypatch, raw, {"auto_prompt_inject": True, "auto_retrieval_apply": True})  # contract flag off
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(sr, "log_telemetry", lambda **kw: seen.append((kw.get("stage", ""), kw.get("extra") or {})))
    out = _ask("suppliers from Germany", tables, full, HITS)
    assert {"n1", "n2"} <= _present(out)  # nothing is dropped in shadow mode
    shadow = [e for stage, e in seen if stage == "schema_contract_shadow"]
    assert shadow and "evidence" in shadow[0] and "gen_supplier_master" in shadow[0]["evidence"]
    assert not [1 for stage, _ in seen if stage == "schema_contract_applied"]
    assert "germany" not in json.dumps(shadow).lower()  # telemetry carries table names and scores, not the question's values


def test_naming_a_country_never_touches_the_database_or_the_model(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _supplier_catalog(dbs)
    tables, full = _supplier_world(monkeypatch, raw, SUPPLIER_APPLY)

    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("retrieval must never read data")

    monkeypatch.setattr(sr, "run_readonly_query", forbidden)
    out = _ask("suppliers from Germany", tables, full, HITS)
    assert "gen_supplier_master" in _present(out)
    from src.sql import retrieval_terms as terms

    assert terms.detect_countries("suppliers from Germany") == ["Germany"]  # detection reads the question and a static list only


def test_the_new_rules_do_nothing_unless_the_retrieval_flag_is_on(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _supplier_catalog(dbs)
    tables, full = _supplier_world(monkeypatch, raw, APPLY)
    base = _ask("suppliers from Germany", tables, full, HITS)
    tables, full = _supplier_world(monkeypatch, raw, {**APPLY, "auto_retrieval_apply": False})
    assert _ask("suppliers from Germany", tables, full, HITS) == base
    monkeypatch.setattr(rt, "question_evidence", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    tables, full = _supplier_world(monkeypatch, raw, SUPPLIER_APPLY)
    broken = _ask("suppliers from Germany", tables, full, HITS)
    assert {"gen_supplier_codes", "plant_master"} <= _present(broken)  # an evidence failure falls back to the plain contract
