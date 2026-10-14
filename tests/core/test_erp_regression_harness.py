"""Tests for the Phase 0 erp_main regression harness (no DB, no LLM, no Qdrant)."""

from __future__ import annotations

import asyncio
import contextvars
import json
from datetime import date

import pytest

from scripts.eval import erp_regression as h


def test_committed_question_ids_are_valid():
    assert h.validate_ids() == []
    assert len(h.load_question_ids()) >= 20


def test_validate_ids_catches_unknown_and_duplicates(tmp_path):
    ids = tmp_path / "ids.json"
    ids.write_text(json.dumps({"question_ids": ["gm-001", "gm-001", "nope-1"]}), encoding="utf-8")
    problems = h.validate_ids(ids)
    assert any("duplicate" in p for p in problems)
    assert any("nope-1" in p for p in problems)


def test_hash_rows_ignores_order_but_not_content():
    a = [{"x": 1}, {"x": 2}]
    assert h.hash_rows(a) == h.hash_rows(list(reversed(a)))
    assert h.hash_rows(a) != h.hash_rows([{"x": 1}, {"x": 3}])


def test_date_sensitive_detection():
    assert h.is_date_sensitive("SELECT * FROM t WHERE d > CURDATE() - INTERVAL 6 MONTH")
    assert not h.is_date_sensitive("SELECT COUNT(*) FROM party")


def test_file_hash_ignores_crlf(tmp_path):
    lf, crlf = tmp_path / "a.json", tmp_path / "b.json"
    lf.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    crlf.write_bytes(b'{"a": 1}\r\n{"b": 2}\r\n')
    assert h.file_sha256(lf) == h.file_sha256(crlf)
    assert h.file_sha256(tmp_path / "missing") is None


class _FakeInner:
    def __init__(self, replies):
        self.replies = list(replies)

    async def chat(self, task="general_qa", messages=None, **kw):
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_recording_router_captures_prompt_response_and_errors():
    async def go():
        rr = h.RecordingRouter(_FakeInner(["SELECT 1", RuntimeError("429")]))
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
        assert await rr.chat(task="reasoning", messages=msgs, max_tokens=768) == "SELECT 1"
        msgs[0]["content"] = "mutated after the call"  # must not alter what was recorded
        with pytest.raises(RuntimeError):
            await rr.chat(task="reasoning", messages=msgs, max_tokens=1024)
        return rr.calls

    calls = asyncio.run(go())
    assert calls[0]["messages"][0]["content"] == "S" and calls[0]["response"] == "SELECT 1"
    assert calls[0]["max_tokens"] == 768
    assert "429" in calls[1]["error"]


def test_replay_router_returns_recorded_and_flags_extra_calls():
    async def go():
        rr = h.ReplayRouter([{"task": "reasoning", "max_tokens": 1, "messages": [], "response": "SELECT 1"}])
        assert await rr.chat(task="reasoning", messages=[{"role": "user", "content": "Q"}], max_tokens=1) == "SELECT 1"
        with pytest.raises(RuntimeError):
            await rr.chat(task="reasoning", messages=[], max_tokens=1)
        return rr.calls

    calls = asyncio.run(go())
    assert calls[0]["messages"] == [{"role": "user", "content": "Q"}]
    assert "more LLM calls" in calls[1]["error"]


def _rec(**over):
    base = {
        "id": "gm-001",
        "calls": [{"task": "reasoning", "max_tokens": 768, "messages": [{"role": "system", "content": "A"}], "response": "x"}],
        "final_sql": "SELECT COUNT(*) FROM party",
        "columns": ["c"],
        "row_count": 1,
        "row_hash": "h1",
        "status": "success",
    }
    base.update(over)
    return base


def test_compare_identical_is_clean():
    assert h.compare_records(_rec(), _rec()) == []


def test_compare_prompt_change_is_fail_with_diff():
    new = _rec(calls=[{"task": "reasoning", "max_tokens": 768, "messages": [{"role": "system", "content": "B"}], "response": "x"}])
    issues = h.compare_records(_rec(), new)
    assert issues and issues[0][0] == "FAIL" and "prompt changed" in issues[0][1] and "-[system] A" in issues[0][1]


def test_compare_sql_and_call_count_changes_fail():
    assert any(lv == "FAIL" for lv, _ in h.compare_records(_rec(), _rec(final_sql="SELECT 2")))
    assert any("call count" in m for _, m in h.compare_records(_rec(), _rec(calls=[])))


def test_compare_rows_warn_by_default_fail_when_strict():
    new = _rec(row_count=2, row_hash="h2")
    default = h.compare_records(_rec(), new)
    assert [lv for lv, _ in default] == ["WARN"] and "data changed, not code" in default[0][1]
    assert [lv for lv, _ in h.compare_records(_rec(), new, strict_rows=True)] == ["FAIL"]


def test_compare_date_relative_sql_rows_drift_is_explained():
    sql = "SELECT COUNT(*) FROM s WHERE d > CURDATE() - INTERVAL 6 MONTH"
    issues = h.compare_records(_rec(final_sql=sql), _rec(final_sql=sql, row_count=9, row_hash="z"))
    assert issues[0][0] == "WARN" and "date-relative" in issues[0][1]


def test_manifest_drift_names_the_changed_input():
    old = {"knowledge_sha256": {"schema/schema.json": "a"}, "global_features": {"x": True}, "frozen_date": "2026-01-15"}
    new = {"knowledge_sha256": {"schema/schema.json": "b"}, "global_features": {"x": False}, "frozen_date": "2026-01-15"}
    notes = h.manifest_drift(old, new)
    assert any("schema/schema.json" in n for n in notes) and any("feature flags" in n for n in notes)
    assert h.manifest_drift(old, old) == []


_budget = contextvars.ContextVar("fake_budget", default=0)


class _FakeRetriever:
    """Mimics the real per-query contextvar budget: increments on every retrieve()."""

    def __init__(self, router):
        self.router = router
        self.last_sql_payload = None
        self.last_query_status = None
        self.last_infra_error = None

    async def retrieve(self, q):
        _budget.set(_budget.get() + 1)
        await self.router.chat(task="reasoning", messages=[{"role": "user", "content": q}], max_tokens=768)
        self.last_sql_payload = {"query": "SELECT 1", "columns": ["a"], "rows": [{"a": 1}], "row_count": 1}
        self.last_query_status = "success"
        self.seen_budget = _budget.get()
        _FakeRetriever.last_seen.append(self.seen_budget)
        return [object()]

    last_seen: list[int] = []


def test_each_question_runs_in_its_own_context_so_budgets_never_carry_over():
    _FakeRetriever.last_seen = []
    cleared = []

    async def go():
        return await h.run_questions(
            [("q1", "a"), ("q2", "b"), ("q3", "c")],
            make_router=lambda: h.RecordingRouter(_FakeInner(["SELECT 1"])),
            make_retriever=_FakeRetriever,
            clear_cache=lambda: cleared.append(1),
        )

    recs = asyncio.run(go())
    assert _FakeRetriever.last_seen == [1, 1, 1]  # a shared context would give [1, 2, 3]
    assert len(cleared) == 3
    assert recs[0]["final_sql"] == "SELECT 1" and recs[0]["row_count"] == 1 and recs[0]["row_hash"]
    assert len(recs[0]["calls"]) == 1


def test_run_questions_records_exception_instead_of_crashing():
    class Boom(_FakeRetriever):
        async def retrieve(self, q):
            raise ValueError("boom")

    recs = asyncio.run(
        h.run_questions([("q1", "a")], lambda: h.RecordingRouter(_FakeInner([])), Boom, lambda: None)
    )
    assert "boom" in recs[0]["exception"]


def test_frozen_prompt_date_applies_to_prompt_builder():
    pb = pytest.importorskip("src.sql.prompt_builder")
    with h.freeze_prompt_date(date(2026, 1, 15)):
        assert pb.datetime.date.today().isoformat() == "2026-01-15"
    assert pb.datetime.date.today() == date.today()  # restored afterwards
