"""Phase 12 follow-ups: wider table retrieval, late schema expansion, join context for repair, prompt hygiene, repair cap.

Everything that changes behaviour is tied to the Phase 12 prompt flags (``auto_prompt_inject`` = apply, ``auto_prompt_shadow`` =
log only) and is off for ``erp_main``. Each test with the flags off asserts the old behaviour exactly. Schemas are synthetic.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.sql.prompt_builder as pb
import src.sql.schema_retrieval as sr
from src.core.provider_client import ProviderRouter
from src.sql import generation, late_schema, pipeline, registry
from src.sql.paths import route_index as ri
from src.sql.repair import delta_repair
from src.sql.repair.join_context import HEADER, build_repair_join_context
from src.sql.repair.sql_repair import MAX_DELTA_REPAIR_ATTEMPTS
from src.utils import telemetry
from src.utils.query_budget import QueryBudgetController, get_or_create_budget_controller

DB = "p12_db"


@pytest.fixture()
def flags(monkeypatch: pytest.MonkeyPatch):
    """Set the per-database Phase 12 flags: ``flags(inject=True, shadow=False)``."""
    state: dict[str, bool] = {}
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: state.get(f, False) and db != "erp_main")

    def set_flags(inject: bool = False, shadow: bool = False) -> None:
        state["auto_prompt_inject"], state["auto_prompt_shadow"] = inject, shadow

    set_flags()
    return set_flags


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    ri.clear_route_index_cache()
    yield tmp_path
    ri.clear_route_index_cache()


def _ddl(name: str, cols: list[str]) -> str:
    return f"TABLE {name} (\n" + ",\n".join(f"  {c} int" for c in cols) + "\n)"


# ===================================================================== retrieval width


def test_vector_top_k_is_four_unless_the_flags_are_in_apply_mode() -> None:
    assert sr._vector_top_k("") == 4 and sr._vector_top_k("shadow") == 4 and sr._vector_top_k("apply") == 8


@pytest.mark.parametrize(("inject", "shadow", "expected"), [(False, False, 4), (False, True, 4), (True, False, 8), (True, True, 8)])
def test_retrieval_asks_the_vector_store_for_the_right_number_of_tables(flags, monkeypatch, inject, shadow, expected) -> None:
    flags(inject=inject, shadow=shadow)
    seen: dict[str, Any] = {}

    class Store:
        async def search_hybrid(self, **kw: Any) -> list[Any]:
            seen.update(kw)
            return []

    class Emb:
        async def embed_query(self, q: str) -> tuple[list[float], dict]:
            return [0.0], {}

    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    asyncio.run(sr.retrieve_schema_from_qdrant("q", _ddl("t", ["id"]), Store(), Emb(), db_id=DB))
    assert seen["top_k"] == expected


# ===================================================================== late schema expansion


class _Retriever:
    def __init__(self, live: str, db_id: str = DB) -> None:
        self.db_id = db_id
        self._live = live
        self.fetches = 0

    async def _fetch_full_schema(self) -> str:
        self.fetches += 1
        return self._live


LIVE = "\n\n".join([_ddl("shown", ["id"]), _ddl("plants", ["plant_id", "name"]), _ddl("teams", ["team_id"]), _ddl("extra_a", ["id"]), _ddl("extra_b", ["id"]), _ddl("extra_c", ["id"])])
PROMPT = _ddl("shown", ["id"])


def test_a_real_table_the_prompt_lacks_is_added_when_the_flags_are_on(flags) -> None:
    flags(inject=True)
    r = _Retriever(LIVE)
    schema, added = asyncio.run(late_schema.expand_schema_with_live_tables(r, PROMPT, ["shown", "Plants"]))
    assert added == ["plants"] and "TABLE plants (" in schema and schema.startswith(PROMPT)


def test_nothing_changes_with_the_flags_off_or_in_shadow_or_for_the_pinned_database(flags) -> None:
    for kwargs in ({}, {"shadow": True}):
        flags(**kwargs)
        r = _Retriever(LIVE)
        assert asyncio.run(late_schema.expand_schema_with_live_tables(r, PROMPT, ["plants"])) == (PROMPT, []) and r.fetches == 0
    flags(inject=True)
    assert asyncio.run(late_schema.expand_schema_with_live_tables(_Retriever(LIVE, db_id="erp_main"), PROMPT, ["plants"])) == (PROMPT, [])


def test_an_invented_table_is_left_alone_so_it_is_still_reported_as_missing(flags) -> None:
    flags(inject=True)
    assert asyncio.run(late_schema.expand_schema_with_live_tables(_Retriever(LIVE), PROMPT, ["ghost_table"])) == (PROMPT, [])


def test_no_live_lookup_when_every_table_is_already_shown_in_either_form(flags) -> None:
    flags(inject=True)
    r = _Retriever(LIVE)
    compact_prompt = PROMPT + "\n\nteams: team_id(int, PK)"
    assert asyncio.run(late_schema.expand_schema_with_live_tables(r, compact_prompt, ["shown", "teams"])) == (compact_prompt, []) and r.fetches == 0


def test_at_most_three_tables_are_added_and_a_failing_lookup_changes_nothing(flags) -> None:
    flags(inject=True)
    _schema, added = asyncio.run(late_schema.expand_schema_with_live_tables(_Retriever(LIVE), PROMPT, ["plants", "teams", "extra_a", "extra_b", "extra_c"]))
    assert len(added) == late_schema.MAX_ADDED

    class Broken(_Retriever):
        async def _fetch_full_schema(self) -> str:
            raise RuntimeError("db down")

    assert asyncio.run(late_schema.expand_schema_with_live_tables(Broken(LIVE), PROMPT, ["plants"])) == (PROMPT, [])


def _fake_retriever(monkeypatch: pytest.MonkeyPatch, live: str) -> Any:
    """A real retriever (as the existing repair-loop tests build one), re-pointed at the synthetic database."""
    r = pipeline.SQLRetriever(router=MagicMock(spec=ProviderRouter), db_id="erp_main")
    r.db_id = DB
    r._router.chat = AsyncMock(return_value="")
    monkeypatch.setattr(pipeline.SQLRetriever, "_column_registry", {})
    monkeypatch.setattr(pipeline.SQLRetriever, "_full_schema_cache", {DB: live})
    return r


@pytest.mark.asyncio
async def test_the_repair_loop_validates_against_a_real_table_the_prompt_lacked_instead_of_repairing(flags, monkeypatch) -> None:
    flags(inject=True)
    r = _fake_retriever(monkeypatch, LIVE)
    executed: list[str] = []

    async def run(sql: str, db_id: str | None = None) -> list[dict]:
        executed.append(sql)
        return [{"name": "p1"}]

    monkeypatch.setattr(generation, "run_readonly_query", run)
    sql = "SELECT p.name FROM shown AS s JOIN plants AS p ON p.plant_id = s.id"
    r._generate_sql = AsyncMock(return_value=sql)
    chunks = await generation.execute_with_delta_repair(r, "which plants", PROMPT)
    assert len(chunks) == 1 and executed == [sql]
    r._router.chat.assert_not_awaited()  # no repair call was needed


@pytest.mark.asyncio
async def test_with_the_flags_off_the_same_query_is_still_rejected_and_repaired_as_before(flags, monkeypatch) -> None:
    flags()
    r = _fake_retriever(monkeypatch, LIVE)
    monkeypatch.setattr(generation, "run_readonly_query", AsyncMock(return_value=[{"name": "p1"}]))
    r._generate_sql = AsyncMock(return_value="SELECT p.name FROM shown AS s JOIN plants AS p ON p.plant_id = s.id")
    r._router.chat = AsyncMock(return_value="```sql\nSELECT s.id FROM shown AS s\n```")
    await generation.execute_with_delta_repair(r, "which plants", PROMPT)
    r._router.chat.assert_awaited()  # the missing table triggered a repair, exactly as before


# ===================================================================== join context for repair


def _write_routes(root: Path, tables: list[str], score: float = 0.8, shape: str = "chain") -> None:
    folder = root / DB / "schema"
    folder.mkdir(parents=True, exist_ok=True)
    joins = [{"from": f"{x}.{y}_id", "to": f"{y}.{y}_id"} for x, y in itertools.pairwise(tables)]
    (folder / "join_paths.json").write_text(json.dumps({"pairs": [{"a": tables[0], "b": tables[-1], "routes": [{"score": score, "shape": shape, "tables": tables, "joins": joins}]}]}), encoding="utf-8")


def _rels(monkeypatch: pytest.MonkeyPatch, edges: list[tuple[str, str, str, str]]) -> None:
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [{"from_table": a, "from_column": b, "to_table": c, "to_column": d} for a, b, c, d in edges])


def test_join_context_is_empty_unless_the_flags_are_in_apply_mode(flags, dbs, monkeypatch) -> None:
    _rels(monkeypatch, [("a", "x", "b", "y")])
    for kwargs in ({}, {"shadow": True}):
        flags(**kwargs)
        assert build_repair_join_context(DB, ["a", "b"]) == ""
    flags(inject=True)
    assert build_repair_join_context("erp_main", ["a", "b"]) == ""


def test_join_context_lists_known_conditions_between_the_tables_of_the_sql_first(flags, dbs, monkeypatch) -> None:
    flags(inject=True)
    _rels(monkeypatch, [("lear", "qn_number", "team", "qn_number"), ("lear", "plant_code", "plant_master", "plant_code"), ("other", "x", "team", "x"), ("lear", "a", "lear", "b")])
    out = build_repair_join_context(DB, ["team", "lear"], ["plant_master"])
    assert out.splitlines()[0] == HEADER
    lines = out.splitlines()[1:]
    assert lines[0] == "- lear.qn_number = team.qn_number"  # both tables used by the SQL: first
    assert "- lear.plant_code = plant_master.plant_code" in lines  # a table the repair is shown
    assert not any("other" in line for line in lines) and not any("lear.a" in line for line in lines)  # unrelated / self edges left out


def test_join_context_adds_a_multi_hop_route_for_tables_with_no_direct_condition(flags, dbs, monkeypatch) -> None:
    flags(inject=True)
    _rels(monkeypatch, [])
    _write_routes(dbs, ["hdr", "mid", "kind"])
    out = build_repair_join_context(DB, ["hdr", "kind"])
    assert out.splitlines()[1:] == ["- hdr.mid_id = mid.mid_id", "- mid.kind_id = kind.kind_id"]
    _write_routes(dbs, ["hdr", "mid", "kind"], shape="siblings")
    ri.clear_route_index_cache()
    assert build_repair_join_context(DB, ["hdr", "kind"]) == ""  # a row-multiplying route is never offered


def test_join_context_is_small_and_never_raises(flags, dbs, monkeypatch) -> None:
    flags(inject=True)
    _rels(monkeypatch, [(f"t{i}", "k", f"t{i + 1}", "k") for i in range(30)])
    out = build_repair_join_context(DB, [f"t{i}" for i in range(12)])
    assert len(out.splitlines()) - 1 <= 8
    assert build_repair_join_context(DB, ["only_one"]) == ""

    def boom(db_id: str = DB) -> list:
        raise RuntimeError("bad file")

    monkeypatch.setattr(pb, "get_raw_relationships", boom)
    assert build_repair_join_context(DB, ["a", "b"]) == ""


def test_the_repair_payload_is_identical_without_join_context_and_keeps_columns_with_it() -> None:
    kw = {"failed_sql": "SELECT 1", "error_message": "boom", "error_type": "column_not_found", "schema_context": {"a": ["c1", "c2"], "b": ["d1"]}, "user_intent": "q"}
    base = delta_repair.build_delta_repair_payload(**kw)
    assert delta_repair.build_delta_repair_payload(**kw, join_context="") == base
    block = "Known joins (use only these ON conditions):\n- a.c1 = b.d1"
    joined = delta_repair.build_delta_repair_payload(**kw, join_context=block)
    assert block in joined[1]["content"] and "c1, c2" in joined[1]["content"]  # the join block did not push the columns out
    big = {f"t{i}": [f"column_number_{j}" for j in range(40)] for i in range(10)}
    kw["schema_context"] = big
    with_block = delta_repair.build_delta_repair_payload(**kw, join_context=block)
    assert block in with_block[1]["content"]  # still there after the schema had to shrink to names only


@pytest.mark.asyncio
async def test_the_repair_call_receives_the_join_context(flags, dbs, monkeypatch) -> None:
    flags(inject=True)
    _rels(monkeypatch, [("shown", "id", "plants", "plant_id")])
    r = _fake_retriever(monkeypatch, LIVE)
    monkeypatch.setattr(generation, "run_readonly_query", AsyncMock(return_value=[{"id": 1}]))
    seen: dict[str, Any] = {}

    async def spy(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(generation, "attempt_delta_repair", spy)
    r._generate_sql = AsyncMock(return_value="SELECT s.nope FROM shown AS s JOIN plants AS p ON p.plant_id = s.id")
    await generation.execute_with_delta_repair(r, "q", PROMPT)
    assert seen["join_context"].startswith(HEADER) and "shown.id = plants.plant_id" in seen["join_context"]


# ===================================================================== prompt hygiene and telemetry


class _D:
    name = "T-SQL"
    date_functions = ""


@pytest.fixture()
def prompt_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pb, "build_behavioral_atlas_for_query", lambda *a, **k: "ATLAS")
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda *a, **k: "GLOSS")
    monkeypatch.setattr(pb, "format_scoped_relationships", lambda *a, **k: "RELS")


ERP_RULES = ("Status Flags: Active='Y'", "Customer vs Supplier: In party table", "Temporal Scope & Financial Year Filtering:\n  * ONLY add")


def test_erp_rules_stay_for_the_erp_database_and_for_schemas_that_have_their_tables(flags, prompt_stubs) -> None:
    flags(inject=True)
    for schema in ("TABLE foo (\n id int\n)", "party: id(int)\nfinancial_year: id(int)"):
        text = pb.build_sql_prompt("q", schema, _D(), db_id="erp_main")
        assert all(rule in text for rule in ERP_RULES)
    text = pb.build_sql_prompt("q", "party: id(int)\nfinancial_year: id(int)", _D(), db_id=DB)
    assert all(rule in text for rule in ERP_RULES)


def test_erp_rules_are_left_out_only_in_apply_mode_for_a_schema_that_has_none_of_their_tables(flags, prompt_stubs) -> None:
    schema = "TABLE sqts_main_lear (\n id int\n)"
    for kwargs in ({}, {"shadow": True}):
        flags(**kwargs)
        assert all(rule in pb.build_sql_prompt("q", schema, _D(), db_id=DB) for rule in ERP_RULES)  # flags off / shadow: unchanged
    flags(inject=True)
    text = pb.build_sql_prompt("q", schema, _D(), db_id=DB)
    assert not any(rule in text for rule in ERP_RULES)
    assert "Casting: Use CAST" in text and "Read-Only" in text  # the general rules are untouched


def test_prompt_section_sizes_are_logged_only_when_the_flags_are_on(flags, prompt_stubs, monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(telemetry, "log_telemetry", lambda **kw: events.append(kw))
    flags()
    pb.build_sql_prompt("q", "TABLE t (\n id int\n)", _D(), db_id=DB)
    assert events == []
    flags(shadow=True)
    text = pb.build_sql_prompt("q", "TABLE t (\n id int\n)", _D(), db_id=DB)
    assert events and events[0]["stage"] == "prompt_sections"
    tokens = events[0]["extra"]["tokens"]
    assert set(tokens) == {"schema", "behavioral_atlas", "column_glossary", "relationships", "readability_rules", "total_prompt"}
    assert tokens["total_prompt"] >= tokens["schema"] > 0 and "ATLAS" in text


def test_the_new_telemetry_stages_are_kept() -> None:
    assert {"schema_late_expansion", "prompt_sections", "route_bridge_applied", "route_bridge_shadow"} <= telemetry.ALLOWED_STAGES


# ===================================================================== repair cap


def test_the_per_query_repair_cap_matches_the_delta_repair_limit() -> None:
    import inspect

    assert QueryBudgetController(query_id="x").max_repairs == MAX_DELTA_REPAIR_ATTEMPTS
    # Read the factory's default from its signature: calling it would bind a budget to the running context and leak into other tests.
    assert inspect.signature(get_or_create_budget_controller).parameters["max_repairs"].default == MAX_DELTA_REPAIR_ATTEMPTS
