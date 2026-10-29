"""Shared logic for the question-suite runner and the run comparer (auto-schema Phase 10, part 2).

Pure functions only (no database, no network, no model calls), so every rule here is unit tested. The runner
(``run_question_suite.py``) produces a run file; the comparer (``compare_runs.py``) says what changed between two runs.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OK_STATUSES = ("success",)


# ---------------------------------------------------------------- questions


@dataclass(frozen=True)
class SuiteQuestion:
    id: str
    question: str
    must_use_tables: tuple[str, ...] = ()
    min_rows: int = 0
    expect_empty: bool = False
    tags: tuple[str, ...] = ()


def _question_from(raw: Any, index: int) -> SuiteQuestion | None:
    if isinstance(raw, str):
        raw = {"question": raw}
    if not isinstance(raw, dict) or not str(raw.get("question", "")).strip():
        return None
    return SuiteQuestion(
        id=str(raw.get("id") or f"q{index:03d}"),
        question=str(raw["question"]).strip(),
        must_use_tables=tuple(str(t).lower() for t in raw.get("must_use_tables", ()) or ()),
        min_rows=int(raw.get("min_rows", 0) or 0),
        expect_empty=bool(raw.get("expect_empty", False)),
        tags=tuple(str(t) for t in raw.get("tags", ()) or ()),
    )


def load_questions(path: Path) -> list[SuiteQuestion]:
    """``.json`` (a list), ``.jsonl`` (one object per line) or plain text (one question per line, ``#`` comments).

    Objects may carry ``id``, ``must_use_tables``, ``min_rows``, ``expect_empty`` and ``tags``; all are optional.
    """
    text = path.read_text(encoding="utf-8")
    items: list[Any]
    if path.suffix.lower() == ".json":
        loaded = json.loads(text)
        items = loaded if isinstance(loaded, list) else loaded.get("questions", [])
    elif path.suffix.lower() == ".jsonl":
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        items = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    questions = [q for i, raw in enumerate(items, 1) if (q := _question_from(raw, i)) is not None]
    ids = [q.id for q in questions]
    if len(ids) != len(set(ids)):
        dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
        raise ValueError(f"duplicate question ids: {dupes}")
    return questions


# ---------------------------------------------------------------- judging one answer


def tables_in_sql(sql: str | None, dialect: str | None = None) -> list[str]:
    """Real tables a statement reads, lower case, sorted; CTE names are not tables. Empty on anything unparseable."""
    if not sql:
        return []
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read=dialect)
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        return sorted({t.name.lower() for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes})
    except Exception:  # noqa: BLE001 - a table list is a convenience, never a failure
        return []


def judge(q: SuiteQuestion, status: str | None, row_count: int | None, tables: Iterable[str]) -> tuple[bool, str]:
    """Did the pipeline answer this question acceptably? ``(ok, reason)``; the reason is empty when ok."""
    if status == "empty_result" and q.expect_empty:
        return True, ""
    if status not in OK_STATUSES:
        return False, f"status={status or 'none'}"
    if q.min_rows and (row_count or 0) < q.min_rows:
        return False, f"rows {row_count or 0} < {q.min_rows}"
    used = {t.lower() for t in tables}
    missing = [t for t in q.must_use_tables if t not in used]
    if missing:
        return False, "missing tables: " + ", ".join(missing)
    return True, ""


# ---------------------------------------------------------------- telemetry of one query


def read_events(path: Path, query_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Telemetry events of the given queries, grouped by query id. A missing or damaged file gives what could be read."""
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                qid = event.get("query_id")
                if qid in query_ids:
                    out.setdefault(qid, []).append(event)
    except OSError:
        pass
    return out


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The few numbers that explain a join question: what the prompt held, what the route planner did, how repair went."""
    summary: dict[str, Any] = {"repair_attempts": 0, "sql_input_tokens": 0}
    for event in events:
        stage, extra = event.get("stage"), event.get("extra") or {}
        if stage == "schema_budget_applied":
            summary["schema"] = {
                "tables": extra.get("budgeted_table_count"),
                "tokens": extra.get("estimated_tokens"),
                "dropped": list(extra.get("dropped_tables") or []),
            }
        elif stage in ("route_bridge_applied", "route_bridge_shadow"):
            summary["route"] = {
                "mode": "applied" if stage.endswith("applied") else "shadow",
                "pairs_routed": extra.get("pairs_routed"),
                "bridges": list(extra.get("bridges") or []),
                "added": list(extra.get("added") or []),
                "skipped": dict(extra.get("skipped") or {}),
            }
        elif stage == "schema_late_expansion":
            summary["late_added"] = list(extra.get("added") or [])
        elif stage == "prompt_sections":
            summary["prompt_tokens"] = dict(extra.get("tokens") or {})
        elif stage == "sql_repair":
            summary["repair_attempts"] = max(summary["repair_attempts"], int(extra.get("repair_attempt") or 0))
        elif stage == "sql_generation" and event.get("input_tokens"):
            summary["sql_input_tokens"] = max(summary["sql_input_tokens"], int(event["input_tokens"]))
    return summary


# ---------------------------------------------------------------- comparing runs


def _norm_sql(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).lower()


@dataclass
class Row:
    id: str
    category: str  # regressed | fixed | still_ok | still_failing | flaky | new | dropped
    baseline: str
    candidate: str
    notes: tuple[str, ...] = ()


def compare_runs(baselines: list[dict[str, Any]], candidate: dict[str, Any]) -> list[Row]:
    """Classify every question. With several baseline runs of the same code, a question must be ok in ALL of them to count as
    working (a question that is ok in some and not in others is ``flaky``: the model, not the change, decides it)."""
    base_by_run = [{r["id"]: r for r in run.get("results", [])} for run in baselines]
    cand = {r["id"]: r for r in candidate.get("results", [])}
    ids = sorted({i for run in base_by_run for i in run} | set(cand))
    rows: list[Row] = []
    for qid in ids:
        base_results = [run[qid] for run in base_by_run if qid in run]
        c = cand.get(qid)
        if not base_results:
            rows.append(Row(qid, "new", "-", "ok" if c and c.get("ok") else "fail"))
            continue
        if c is None:
            rows.append(Row(qid, "dropped", "ok" if all(b.get("ok") for b in base_results) else "fail", "-"))
            continue
        oks = [bool(b.get("ok")) for b in base_results]
        base_label = "ok" if all(oks) else "fail" if not any(oks) else "mixed"
        cand_ok = bool(c.get("ok"))
        notes: list[str] = []
        if all(oks) and cand_ok:
            category = "still_ok"
            b = base_results[0]
            if _norm_sql(b.get("sql")) != _norm_sql(c.get("sql")):
                notes.append("sql changed")
            if b.get("row_count") is not None and c.get("row_count") is not None and b["row_count"] != c["row_count"]:
                notes.append(f"row count {b['row_count']} -> {c['row_count']}")
            if sorted(b.get("tables") or []) != sorted(c.get("tables") or []):
                notes.append("tables changed")
        elif all(oks) and not cand_ok:
            category = "regressed"
            notes.append(c.get("reason") or "failed")
        elif not any(oks) and cand_ok:
            category = "fixed"
        elif not any(oks):
            category = "still_failing"
            if c.get("reason"):
                notes.append(c["reason"])
        else:
            category = "flaky"
        rows.append(Row(qid, category, base_label, "ok" if cand_ok else "fail", tuple(notes)))
    return rows


def format_comparison(rows: list[Row], baselines: list[dict[str, Any]], candidate: dict[str, Any]) -> str:
    counts = Counter(r.category for r in rows)
    lines = [
        f"questions: {len(rows)}   baselines: {len(baselines)} run(s)",
        "  ".join(f"{k}: {counts[k]}" for k in ("regressed", "fixed", "still_ok", "still_failing", "flaky", "new", "dropped") if counts[k] or k in ("regressed", "fixed")),
    ]
    for category, title in (("regressed", "REGRESSED (worked before, fails now)"), ("fixed", "FIXED (failed before, works now)"), ("flaky", "FLAKY (mixed in the baseline runs, not counted)")):
        picked = [r for r in rows if r.category == category]
        if picked:
            lines += ["", title] + [f"  {r.id}  {'; '.join(r.notes)}".rstrip() for r in picked]
    changed = [r for r in rows if r.category == "still_ok" and r.notes]
    if changed:
        lines += ["", "STILL OK, BUT DIFFERENT (read these: same question, different SQL, rows or tables)"] + [f"  {r.id}  {'; '.join(r.notes)}" for r in changed]
    lines += ["", _aggregate(baselines, candidate)]
    return "\n".join(lines)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _aggregate(baselines: list[dict[str, Any]], candidate: dict[str, Any]) -> str:
    def stats(runs: list[dict[str, Any]]) -> dict[str, float | None]:
        results = [r for run in runs for r in run.get("results", [])]
        return {
            "latency_ms": _mean([r["latency_ms"] for r in results if r.get("latency_ms") is not None]),
            "prompt_tokens": _mean([r["telemetry"]["sql_input_tokens"] for r in results if r.get("telemetry", {}).get("sql_input_tokens")]),
            "repairs": _mean([r["telemetry"].get("repair_attempts", 0) for r in results if r.get("telemetry") is not None]),
        }

    b, c = stats(baselines), stats([candidate])
    return "averages per question   " + "   ".join(f"{k}: {b[k]} -> {c[k]}" for k in b)


def regression_exit_code(rows: list[Row], *, strict: bool = False) -> int:
    """1 when something that worked now fails (and, with ``strict``, when an answer that still works changed)."""
    if any(r.category == "regressed" for r in rows):
        return 1
    if strict and any(r.category == "still_ok" and r.notes for r in rows):
        return 1
    return 0
