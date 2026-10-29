"""Phase 10 (measure only): route benchmark, live floor table, skipped-route telemetry, question-suite runner and comparer.

None of this changes a threshold or the pipeline's answers. The first group proves the planner still chooses exactly the same
bridges while it now also reports why routes were skipped.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

import src.sql.prompt_builder as pb
import src.sql.schema_retrieval as sr
from scripts.db import paths_benchmark as paths_cli
from scripts.eval import compare_runs as compare_cli
from scripts.eval import run_question_suite as runner
from scripts.eval import suite_core as core
from src.sql import registry
from src.sql.paths import benchmark as bench
from src.sql.paths import route_index as ri
from src.utils import telemetry

FIXTURES = Path(__file__).parent / "fixtures" / "relationships"
NAMES = ("employees", "pagila", "northwind", "chinook", "sakila")
DB = "p10_db"


def _catalog(name: str) -> tuple[dict, dict]:
    return json.loads((FIXTURES / f"{name}.schema.json").read_text()), json.loads((FIXTURES / f"{name}.inventory.json").read_text())


def _pair(tables: list[str], score: float = 0.8, shape: str = "chain") -> dict[str, Any]:
    joins = [{"from": f"{a}.{b}_id", "to": f"{b}.{b}_id"} for a, b in itertools.pairwise(tables)]
    return {"a": tables[0], "b": tables[-1], "routes": [{"score": score, "shape": shape, "tables": tables, "joins": joins}]}


# ===================================================================== planner: same bridges, now with reasons


def _idx(*pairs: dict[str, Any]) -> ri.RouteIndex:
    return ri.RouteIndex({k: v for p in pairs for k, v in [ri._parse_pair(p)]})  # type: ignore[misc]


def test_skip_reasons_are_counted_without_changing_which_bridges_are_chosen() -> None:
    idx = _idx(
        _pair(["a", "w", "b"], score=0.30),  # weak
        _pair(["a", "s", "c"], shape="siblings"),  # siblings
        _pair(["a", "gone", "d"]),  # bridge not in the live schema
        _pair(["a", "m1", "m2", "m3", "m4", "e"]),  # needs 4 bridges
    )
    avail = {"a", "b", "c", "d", "e", "w", "s", "m1", "m2", "m3", "m4"}
    seeds = ["a", "b", "c", "d", "e"]
    plan = ri.plan_route_bridges(seeds, DB, index=idx, available=avail, max_add=3)
    assert plan is not None and plan.bridges == ()  # unchanged: nothing qualified
    assert dict(plan.skipped) == {"low_score": 1, "siblings": 1, "missing_table": 1, "over_cap": 1}
    ok = ri.plan_route_bridges(seeds, DB, index=idx, available=avail, max_add=4)
    assert ok.bridges == ("m1", "m2", "m3", "m4") and "over_cap" not in dict(ok.skipped)
    present = ri.plan_route_bridges(["b", "p", "f"], DB, index=_idx(_pair(["b", "p", "f"])), available={"b", "p", "f"})
    assert present.bridges == () and dict(present.skipped) == {"already_present": 1}


def test_the_route_telemetry_event_carries_the_skip_reasons(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    ri.clear_route_index_cache()
    folder = tmp_path / DB / "schema"
    folder.mkdir(parents=True)
    (folder / "join_paths.json").write_text(json.dumps({"pairs": [_pair(["hdr", "b1", "kind"], score=0.30)]}), encoding="utf-8")
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: f == "auto_prompt_inject")
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")
    monkeypatch.setattr(sr, "route_anchor_tables", lambda q, db_id=DB: set())
    events: list[dict] = []
    monkeypatch.setattr(sr, "log_telemetry", lambda **kw: events.append(kw))
    tables = {t: f"TABLE {t} (\n  {t}_id int\n)" for t in ("hdr", "b1", "kind")}

    class Chunk:
        def __init__(self, t: str) -> None:
            self.content, self.document_id, self.db_id, self.metadata = tables[t], f"schema:{DB}", DB, {"db_id": DB}

    class Store:
        async def search_hybrid(self, **_kw: Any) -> list[Any]:
            return [type("H", (), {"chunk": Chunk(t)})() for t in ("hdr", "kind")]

    class Emb:
        async def embed_query(self, q: str) -> tuple[list[float], dict]:
            return [0.0], {}

    asyncio.run(sr.retrieve_schema_from_qdrant("q", "\n\n".join(tables.values()), Store(), Emb(), db_id=DB))
    route_events = [e for e in events if e["stage"] == "route_bridge_applied"]
    assert route_events and route_events[0]["extra"]["skipped"] == {"low_score": 1}
    ri.clear_route_index_cache()


# ===================================================================== route benchmark


@pytest.mark.parametrize("name", NAMES)
def test_benchmark_numbers_are_consistent_and_never_improve_with_a_higher_floor(name: str) -> None:
    schema, inventory = _catalog(name)
    case = bench.run_route_case(name, schema, inventory, edge_threshold=0.45)
    previous: dict[int, int] = {}
    for floor in sorted(bench.DEFAULT_FLOORS):
        for row in case.at(floor):
            assert row.exact <= row.same_pair <= row.truth
            assert row.exact <= previous.get(row.hops, row.exact)  # a stricter floor can only lose routes
            previous[row.hops] = row.exact
    assert all(1 <= len(t) - 1 <= 5 for t, _s in case.truth.values())


def test_inferred_edges_from_names_alone_never_reach_the_apply_threshold_so_no_route_is_found() -> None:
    """The Phase 6 benchmark pins that no inferred edge reaches 0.70 on these catalogs; routes then need other evidence."""
    for name in NAMES:
        schema, inventory = _catalog(name)
        case = bench.run_route_case(name, schema, inventory, edge_threshold=0.70)
        assert case.edges_admitted == 0 and case.found == {}


def test_a_lower_edge_threshold_finds_routes_and_the_report_lists_every_threshold() -> None:
    cases = [bench.run_route_case(n, *_catalog(n), edge_threshold=t) for t in (0.45, 0.70) for n in NAMES]
    assert sum(len(c.found) for c in cases if c.edge_threshold == 0.45) > 0
    report = bench.format_route_report(cases, details=True)
    assert "admitted at score >= 0.45" in report and "admitted at score >= 0.70" in report and "recall" in report


def test_live_floor_table_counts_routes_per_length_and_ignores_malformed_entries() -> None:
    doc = {"pairs": [_pair(["a", "m", "z"], 0.58), _pair(["b", "m", "z"], 0.44), _pair(["c", "m", "n", "z"], 0.41), _pair(["d", "z"], 0.9), {"bad": 1}, "junk", {"routes": [{"tables": ["x", "y", "z"], "score": "nan?"}]}]}
    table = bench.live_floor_table(doc, (0.30, 0.45))
    rows = {r["hops"]: r for r in table["rows"]}
    assert rows[2]["routes"] == 2 and rows[2]["kept"] == {"0.30": 2, "0.45": 1}
    assert rows[3]["routes"] == 1 and rows[3]["kept"] == {"0.30": 1, "0.45": 0}
    assert rows[4]["routes"] == 0 and table["shapes"] == {"chain": 3}
    assert "joins" in bench.format_live_table(table)


def test_paths_cli_live_check_reads_a_real_file_and_reports_a_missing_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "qts" / "schema"
    folder.mkdir(parents=True)
    (folder / "join_paths.json").write_text(json.dumps({"pairs": [_pair(["a", "m", "z"], 0.58)]}), encoding="utf-8")
    assert paths_cli.main(["--live-check", "--db-id", "qts", "--databases-dir", str(tmp_path)]) == 0
    assert ">=0.45" in capsys.readouterr().out
    assert paths_cli.main(["--live-check", "--db-id", "nope", "--databases-dir", str(tmp_path)]) == 2
    assert paths_cli.main(["--edge-thresholds", "0.60", "--floors", "0.30"]) == 0  # the shipped fixtures


# ===================================================================== question suite: pure logic


def test_load_questions_from_text_json_and_jsonl(tmp_path: Path) -> None:
    (tmp_path / "q.txt").write_text("# comment\nfirst?\n\nsecond?\n", encoding="utf-8")
    assert [q.question for q in core.load_questions(tmp_path / "q.txt")] == ["first?", "second?"]
    (tmp_path / "q.json").write_text(json.dumps([{"id": "j1", "question": "x", "must_use_tables": ["A"], "min_rows": 2, "tags": ["multi-join"]}, "plain"]), encoding="utf-8")
    first, second = core.load_questions(tmp_path / "q.json")
    assert first.id == "j1" and first.must_use_tables == ("a",) and first.min_rows == 2 and second.id == "q002"
    (tmp_path / "q.jsonl").write_text('{"question": "l1"}\n{"question": "l2"}\n', encoding="utf-8")
    assert len(core.load_questions(tmp_path / "q.jsonl")) == 2
    (tmp_path / "dup.json").write_text(json.dumps([{"id": "a", "question": "1"}, {"id": "a", "question": "2"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        core.load_questions(tmp_path / "dup.json")


def test_judge_applies_status_rows_and_required_tables() -> None:
    q = core.SuiteQuestion("q", "?", must_use_tables=("lear", "plants"), min_rows=1)
    assert core.judge(q, "success", 3, ["Lear", "plants", "x"]) == (True, "")
    assert core.judge(q, "success", 3, ["lear"])[1] == "missing tables: plants"
    assert core.judge(q, "success", 0, ["lear", "plants"])[1] == "rows 0 < 1"
    assert core.judge(q, "failed", None, [])[1] == "status=failed"
    assert core.judge(q, "empty_result", 0, [])[0] is False
    assert core.judge(core.SuiteQuestion("e", "?", expect_empty=True), "empty_result", 0, [])[0] is True


def test_tables_in_sql_skips_ctes_and_survives_bad_sql() -> None:
    sql = "WITH x AS (SELECT * FROM Orders) SELECT * FROM x JOIN dbo.Customers c ON c.id = x.id"
    assert core.tables_in_sql(sql, "tsql") == ["customers", "orders"]
    assert core.tables_in_sql("not sql at all ((", "tsql") == [] and core.tables_in_sql(None) == []


def test_summarize_events_extracts_what_explains_a_join_question() -> None:
    events = [
        {"stage": "route_bridge_applied", "extra": {"bridges": [], "added": [], "pairs_routed": 0, "skipped": {"low_score": 2}}},
        {"stage": "schema_budget_applied", "extra": {"budgeted_table_count": 9, "estimated_tokens": 988, "dropped_tables": ["t9"]}},
        {"stage": "schema_late_expansion", "extra": {"added": ["plants"]}},
        {"stage": "prompt_sections", "extra": {"tokens": {"schema": 941, "total_prompt": 14000}}},
        {"stage": "sql_generation", "input_tokens": 14293, "extra": {}},
        {"stage": "sql_generation", "input_tokens": 0, "extra": {}},
        {"stage": "sql_repair", "extra": {"repair_attempt": 1}},
        {"stage": "sql_repair", "extra": {"repair_attempt": 2}},
    ]
    s = core.summarize_events(events)
    assert s["route"] == {"mode": "applied", "pairs_routed": 0, "bridges": [], "added": [], "skipped": {"low_score": 2}}
    assert s["schema"] == {"tables": 9, "tokens": 988, "dropped": ["t9"]} and s["late_added"] == ["plants"]
    assert s["prompt_tokens"]["total_prompt"] == 14000 and s["sql_input_tokens"] == 14293 and s["repair_attempts"] == 2
    assert core.summarize_events([]) == {"repair_attempts": 0, "sql_input_tokens": 0}


def test_read_events_groups_by_query_and_tolerates_a_missing_or_damaged_file(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"query_id": "a", "stage": "x"}\nnot json\n\n{"query_id": "b", "stage": "y"}\n{"query_id": "c"}\n', encoding="utf-8")
    got = core.read_events(path, {"a", "b"})
    assert set(got) == {"a", "b"} and got["a"][0]["stage"] == "x"
    assert core.read_events(tmp_path / "missing.jsonl", {"a"}) == {}


def _run(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"results": results}


def _r(qid: str, ok: bool, sql: str = "SELECT 1", rows: int = 1, tables: tuple[str, ...] = ("a",), reason: str = "") -> dict[str, Any]:
    return {"id": qid, "ok": ok, "sql": sql, "row_count": rows, "tables": list(tables), "reason": reason, "latency_ms": 10.0, "telemetry": {"sql_input_tokens": 100, "repair_attempts": 0}}


def test_compare_classifies_regressions_fixes_flaky_and_unchanged() -> None:
    b1 = _run([_r("reg", True), _r("fix", False), _r("same", True), _r("flaky", True), _r("fail", False), _r("gone", True), _r("diff", True)])
    b2 = _run([_r("reg", True), _r("fix", False), _r("same", True), _r("flaky", False), _r("fail", False), _r("gone", True), _r("diff", True)])
    cand = _run([_r("reg", False, reason="status=failed"), _r("fix", True), _r("same", True), _r("flaky", False), _r("fail", False, reason="status=failed"), _r("new", True),
                 _r("diff", True, sql="SELECT 2", rows=5, tables=("a", "b"))])
    rows = {r.id: r for r in core.compare_runs([b1, b2], cand)}
    assert {k: v.category for k, v in rows.items()} == {"reg": "regressed", "fix": "fixed", "same": "still_ok", "flaky": "flaky", "fail": "still_failing", "gone": "dropped", "new": "new", "diff": "still_ok"}
    assert rows["reg"].notes == ("status=failed",) and rows["same"].notes == ()
    assert set(rows["diff"].notes) == {"sql changed", "row count 1 -> 5", "tables changed"}
    text = core.format_comparison(list(rows.values()), [b1, b2], cand)
    assert "REGRESSED" in text and "FIXED" in text and "FLAKY" in text and "STILL OK, BUT DIFFERENT" in text and "averages per question" in text


def test_exit_code_fails_only_on_regressions_unless_strict() -> None:
    clean = core.compare_runs([_run([_r("a", True), _r("b", False)])], _run([_r("a", True), _r("b", True)]))
    assert core.regression_exit_code(clean) == 0
    bad = core.compare_runs([_run([_r("a", True)])], _run([_r("a", False)]))
    assert core.regression_exit_code(bad) == 1
    drifted = core.compare_runs([_run([_r("a", True)])], _run([_r("a", True, rows=9)]))
    assert core.regression_exit_code(drifted) == 0 and core.regression_exit_code(drifted, strict=True) == 1


def test_compare_cli_exit_codes_and_unreadable_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, good, bad = tmp_path / "b.json", tmp_path / "g.json", tmp_path / "x.json"
    base.write_text(json.dumps(_run([_r("a", True)])), encoding="utf-8")
    good.write_text(json.dumps(_run([_r("a", True)])), encoding="utf-8")
    bad.write_text(json.dumps(_run([_r("a", False)])), encoding="utf-8")
    assert compare_cli.main(["--baseline", str(base), "--candidate", str(good)]) == 0
    assert compare_cli.main(["--baseline", str(base), "--candidate", str(bad)]) == 1
    assert compare_cli.main(["--baseline", str(base), "--candidate", str(tmp_path / "none.json")]) == 2
    capsys.readouterr()


# ===================================================================== question suite: runner


def test_each_question_runs_in_its_own_context_so_nothing_leaks_to_the_next() -> None:
    seen: list[str | None] = []

    async def ask(q: core.SuiteQuestion) -> dict[str, Any]:
        seen.append(telemetry.get_current_query_id())  # what this question starts with
        telemetry.set_current_query_id(f"qid-{q.id}")  # what the pipeline does
        return {"ok": True, "reason": "", "query_id": f"qid-{q.id}"}

    results = asyncio.run(runner.run_suite([core.SuiteQuestion("a", "1"), core.SuiteQuestion("b", "2")], ask))
    assert seen == [None, None] and [r["id"] for r in results] == ["a", "b"] and telemetry.get_current_query_id() is None


class _FakeRetriever:
    _dialect = type("D", (), {"sqlglot_dialect": "tsql"})()

    def __init__(self, status: str | None, payload: dict | None, boom: bool = False, infra: str | None = None) -> None:
        self.last_query_status, self.last_sql_payload, self.boom, self.last_infra_error = status, payload, boom, infra

    async def retrieve(self, query: str) -> list:
        if self.boom:
            raise RuntimeError("provider down")
        return []


def test_make_ask_records_status_sql_tables_and_judges_the_answer() -> None:
    q = core.SuiteQuestion("q1", "?", must_use_tables=("lear",), min_rows=1)
    payload = {"query": "SELECT * FROM dbo.lear l JOIN team t ON t.id = l.id", "row_count": 4}
    rec = asyncio.run(runner.make_ask(_FakeRetriever("success", payload))(q))
    assert rec["ok"] and rec["tables"] == ["lear", "team"] and rec["row_count"] == 4 and rec["query_id"].startswith("gm-q-")
    failed = asyncio.run(runner.make_ask(_FakeRetriever("failed", None, infra="db down"))(q))
    assert not failed["ok"] and failed["reason"] == "status=failed" and failed["error"] == "db down"
    crashed = asyncio.run(runner.make_ask(_FakeRetriever(None, None, boom=True))(q))
    assert crashed["status"] == "exception" and "provider down" in crashed["error"] and not crashed["ok"]


def test_build_run_attaches_telemetry_by_query_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: f == "auto_prompt_inject")
    log = tmp_path / "t.jsonl"
    log.write_text(json.dumps({"query_id": "gm-q-1", "stage": "schema_late_expansion", "extra": {"added": ["plants"]}}) + "\n", encoding="utf-8")
    run = runner.build_run(DB, "after", [{"query_id": "gm-q-1", "ok": True, "id": "a"}, {"query_id": "gm-q-2", "ok": False, "id": "b"}], log)
    assert run["results"][0]["telemetry"]["late_added"] == ["plants"] and run["results"][1]["telemetry"]["repair_attempts"] == 0
    assert run["meta"]["ok"] == 1 and run["meta"]["questions"] == 2 and run["meta"]["flags"]["auto_prompt_inject"] is True


def test_frozen_learning_restores_changed_files_and_removes_new_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patterns, metrics = tmp_path / "learned_patterns.jsonl", tmp_path / "learning_metrics.json"
    patterns.write_text("original\n", encoding="utf-8")

    class FakeLearner:
        def __init__(self, db_id: str) -> None:
            self.learned_patterns_write_path, self.metrics_write_path = patterns, metrics

    monkeypatch.setattr("src.sql.learning.pattern_learner.PatternLearner", FakeLearner)
    with runner.frozen_learning(DB):
        patterns.write_text("original\nlearned during the run\n", encoding="utf-8")
        metrics.write_text("{}", encoding="utf-8")
    assert patterns.read_text(encoding="utf-8") == "original\n" and not metrics.exists()
    with runner.frozen_learning(DB, enabled=False):
        patterns.write_text("kept\n", encoding="utf-8")
    assert patterns.read_text(encoding="utf-8") == "kept\n"
