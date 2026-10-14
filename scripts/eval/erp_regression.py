#!/usr/bin/env python3
"""erp_main regression harness (Phase 0 safety net).

Records, for a fixed question set, exactly what the SQL layer does today: every
LLM prompt it sends, the SQL it ends up with, and the result it gets. Later
phases are judged by replaying the recorded LLM answers against the new code.

Why record / replay: LLM output varies run to run (and providers rate-limit), so
"same answer" can never be a byte comparison on live calls. With the LLM answers
frozen, everything AFTER the LLM (validation, soft-delete enforcement, execution,
formatting) and everything BEFORE it (schema retrieval, prompt building) is
deterministic, so any difference is a code change.

Modes (run from the repo root, on a machine that has the erp_main database,
Qdrant schema index and provider keys):

    python scripts/eval/erp_regression.py validate            # offline, no src imports
    python scripts/eval/erp_regression.py record              # real LLM, writes baseline
    python scripts/eval/erp_regression.py replay              # frozen LLM, compares
    python scripts/eval/erp_regression.py replay --strict-rows

Nothing here touches production code paths: the router is wrapped, the date seen
by the prompt builder is frozen, caches are cleared and learned patterns are read
from an empty temp folder. No secrets are read or printed. The baseline stores a
row COUNT and an order-insensitive row HASH, never row data.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = next(
    (p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file()),
    Path(__file__).resolve().parents[2],
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DB_ID = "erp_main"
FROZEN_DATE = date(2026, 1, 15)  # the date the prompt builder sees during record AND replay

REG_DIR = REPO_ROOT / "databases" / DB_ID / "evals" / "regression"
IDS_FILE = REG_DIR / "question_ids.json"
BASELINE_FILE = REG_DIR / "baseline.jsonl"
MANIFEST_FILE = REG_DIR / "manifest.json"
QUESTIONS_FILE = REPO_ROOT / "databases" / DB_ID / "evals" / "questions.jsonl"
REPORT_FILE = REPO_ROOT / "reports" / "erp_regression_last.md"

# Knowledge files that feed the prompt. A change here explains a prompt change.
KNOWLEDGE_FILES = (
    "schema/schema.json",
    "schema/relationships.json",
    "semantics/glossary.json",
    "semantics/column_glossary.json",
    "semantics/behavioral_atlas.json",
    "semantics/routing_hints.json",
)

_DATE_SENSITIVE_SQL = re.compile(
    r"\b(CURDATE|CURRENT_DATE|NOW|CURRENT_TIMESTAMP|SYSDATE|DATE_SUB|DATE_ADD|DATEDIFF|INTERVAL)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Offline helpers (no src imports)
# ---------------------------------------------------------------------------

def load_question_ids(path: Path = IDS_FILE) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["question_ids"])


def load_questions(path: Path = QUESTIONS_FILE) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                q = json.loads(line)
                out[q["id"]] = q
    return out


def validate_ids(ids_path: Path = IDS_FILE, questions_path: Path = QUESTIONS_FILE) -> list[str]:
    """Return a list of problems (empty means OK)."""
    problems: list[str] = []
    ids = load_question_ids(ids_path)
    questions = load_questions(questions_path)
    if len(ids) != len(set(ids)):
        problems.append("duplicate ids in question_ids.json")
    for qid in ids:
        if qid not in questions:
            problems.append(f"{qid}: not in questions.jsonl")
    if not ids:
        problems.append("question_ids.json is empty")
    return problems


def _normalized_bytes(path: Path) -> bytes:
    """File bytes with CRLF folded to LF so Windows/Linux checkouts hash the same."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(_normalized_bytes(path)).hexdigest()


def hash_rows(rows: list[dict[str, Any]]) -> str:
    """Order-insensitive hash: unordered SQL results must not look like regressions.

    ORDER BY regressions are still caught because the SQL text is compared too.
    """
    canon = sorted(json.dumps(r, sort_keys=True, default=str, ensure_ascii=False) for r in rows)
    return hashlib.sha256("\n".join(canon).encode("utf-8")).hexdigest()


def is_date_sensitive(sql: str | None) -> bool:
    return bool(sql and _DATE_SENSITIVE_SQL.search(sql))


def build_manifest(question_ids: list[str]) -> dict[str, Any]:
    db_dir = REPO_ROOT / "databases" / DB_ID
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
        ).stdout.strip() or None
    except Exception:  # noqa: BLE001
        sha = None
    features: dict[str, Any] = {}
    flags_file = REPO_ROOT / "config" / "feature_flags.yaml"
    if flags_file.exists():
        try:
            import yaml

            data = yaml.safe_load(flags_file.read_text(encoding="utf-8")) or {}
            features = dict(data.get("features", {})) if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            features = {}
    return {
        "db_id": DB_ID,
        "frozen_date": FROZEN_DATE.isoformat(),
        "git_sha": sha,
        "question_ids": question_ids,
        "knowledge_sha256": {rel: file_sha256(db_dir / rel) for rel in KNOWLEDGE_FILES},
        "pattern_library_sha256": file_sha256(REPO_ROOT / "config" / "sql_pattern_library.json"),
        "global_features": features,
    }


# ---------------------------------------------------------------------------
# Routers: wrap / replace the LLM router, never change production code
# ---------------------------------------------------------------------------

class RecordingRouter:
    """Forwards every call to the real router and stores prompt + response."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    async def chat(self, task: str = "general_qa", messages: list[dict[str, Any]] | None = None, **kwargs: Any) -> str:
        call: dict[str, Any] = {
            "task": task,
            "max_tokens": kwargs.get("max_tokens"),
            "messages": copy.deepcopy(messages or []),
        }
        self.calls.append(call)
        try:
            response = await self._inner.chat(task=task, messages=messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            call["error"] = f"{type(exc).__name__}: {exc}"
            raise
        call["response"] = response
        return response

    def __getattr__(self, name: str) -> Any:  # anything else the pipeline asks the router for
        return getattr(self._inner, name)


class ReplayRouter:
    """Returns the recorded responses in order; logs what the code actually sent."""

    def __init__(self, recorded_calls: list[dict[str, Any]]) -> None:
        self._recorded = recorded_calls
        self.calls: list[dict[str, Any]] = []

    async def chat(self, task: str = "general_qa", messages: list[dict[str, Any]] | None = None, **kwargs: Any) -> str:
        observed: dict[str, Any] = {
            "task": task,
            "max_tokens": kwargs.get("max_tokens"),
            "messages": copy.deepcopy(messages or []),
        }
        self.calls.append(observed)
        index = len(self.calls) - 1
        if index >= len(self._recorded):
            observed["error"] = "replay: more LLM calls than were recorded"
            raise RuntimeError(observed["error"])
        recorded = self._recorded[index]
        if "error" in recorded:
            observed["error"] = recorded["error"]
            raise RuntimeError(recorded["error"])
        observed["response"] = recorded.get("response")
        return observed["response"]

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"ReplayRouter has no attribute {name!r} (only chat() is replayed)")


# ---------------------------------------------------------------------------
# Running one question (shared by record and replay)
# ---------------------------------------------------------------------------

async def _run_one(
    qid: str,
    question: str,
    make_router: Callable[[], Any],
    make_retriever: Callable[[Any], Any],
    clear_cache: Callable[[], None],
) -> dict[str, Any]:
    router = make_router()
    clear_cache()
    record: dict[str, Any] = {"id": qid, "question": question}
    try:
        retriever = make_retriever(router)
        chunks = await retriever.retrieve(question)
        payload = getattr(retriever, "last_sql_payload", None) or {}
        rows = payload.get("rows") or []
        record.update(
            {
                "final_sql": payload.get("query"),
                "columns": list(payload.get("columns") or []),
                "row_count": payload.get("row_count"),
                "row_hash": hash_rows(rows) if rows else None,
                "status": getattr(retriever, "last_query_status", None),
                "infra_error": getattr(retriever, "last_infra_error", None),
                "chunk_count": len(chunks or []),
            }
        )
    except Exception as exc:  # noqa: BLE001
        record["exception"] = f"{type(exc).__name__}: {exc}"
    record["calls"] = router.calls
    return record


async def run_questions(
    items: list[tuple[str, str]],
    make_router: Callable[[], Any],
    make_retriever: Callable[[Any], Any],
    clear_cache: Callable[[], None],
) -> list[dict[str, Any]]:
    """Run each question in its OWN task.

    The query id and the per-query LLM budget live in contextvars; a task gets a
    private copy, so one question can never use up the next question's budget.
    """
    results: list[dict[str, Any]] = []
    for qid, question in items:
        task = asyncio.create_task(_run_one(qid, question, make_router, make_retriever, clear_cache))
        results.append(await task)
    return results


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _prompt_diff(base_msgs: list[dict[str, Any]], new_msgs: list[dict[str, Any]], limit: int = 40) -> str:
    a = "\n".join(f"[{m.get('role')}] {m.get('content')}" for m in base_msgs).splitlines()
    b = "\n".join(f"[{m.get('role')}] {m.get('content')}" for m in new_msgs).splitlines()
    diff = list(difflib.unified_diff(a, b, "baseline", "now", lineterm="", n=1))
    shown = diff[:limit]
    if len(diff) > limit:
        shown.append(f"... ({len(diff) - limit} more diff lines)")
    return "\n".join(shown)


def compare_records(base: dict[str, Any], new: dict[str, Any], strict_rows: bool = False) -> list[tuple[str, str]]:
    """Return [(level, message)], level is FAIL or WARN. Empty list means identical."""
    issues: list[tuple[str, str]] = []

    if "exception" in new and "exception" not in base:
        issues.append(("FAIL", f"now raises: {new['exception']}"))
    if "exception" in base and "exception" not in new:
        issues.append(("FAIL", f"baseline raised ({base['exception']}) but now does not"))

    b_calls, n_calls = base.get("calls", []), new.get("calls", [])
    if len(b_calls) != len(n_calls):
        issues.append(("FAIL", f"LLM call count changed: baseline {len(b_calls)}, now {len(n_calls)}"))
    for i, (bc, nc) in enumerate(zip(b_calls, n_calls)):
        if bc.get("task") != nc.get("task"):
            issues.append(("FAIL", f"call {i}: task {bc.get('task')!r} -> {nc.get('task')!r}"))
        if bc.get("max_tokens") != nc.get("max_tokens"):
            issues.append(("FAIL", f"call {i}: max_tokens {bc.get('max_tokens')} -> {nc.get('max_tokens')}"))
        if bc.get("messages") != nc.get("messages"):
            issues.append(("FAIL", f"call {i}: prompt changed\n{_prompt_diff(bc.get('messages', []), nc.get('messages', []))}"))

    for key in ("final_sql", "columns", "status"):
        if base.get(key) != new.get(key):
            issues.append(("FAIL", f"{key} changed: {base.get(key)!r} -> {new.get(key)!r}"))

    rows_differ = (base.get("row_count"), base.get("row_hash")) != (new.get("row_count"), new.get("row_hash"))
    if rows_differ:
        same_sql = base.get("final_sql") == new.get("final_sql")
        msg = (
            f"result rows changed: count {base.get('row_count')} -> {new.get('row_count')}, "
            f"hash {'same' if base.get('row_hash') == new.get('row_hash') else 'different'}"
        )
        if strict_rows:
            issues.append(("FAIL", msg))
        elif same_sql and is_date_sensitive(new.get("final_sql")):
            issues.append(("WARN", msg + " (SQL is date-relative, same SQL: expected drift)"))
        elif same_sql:
            issues.append(("WARN", msg + " (same SQL, so data changed, not code)"))
        else:
            issues.append(("WARN", msg + " (SQL differs, see above)"))
    return issues


def manifest_drift(baseline_manifest: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Explain WHY prompts may differ even with unchanged code."""
    notes: list[str] = []
    old_k, new_k = baseline_manifest.get("knowledge_sha256", {}), current.get("knowledge_sha256", {})
    for rel in sorted(set(old_k) | set(new_k)):
        if old_k.get(rel) != new_k.get(rel):
            notes.append(f"knowledge file changed since baseline: {rel}")
    if baseline_manifest.get("pattern_library_sha256") != current.get("pattern_library_sha256"):
        notes.append("config/sql_pattern_library.json changed since baseline")
    if baseline_manifest.get("global_features") != current.get("global_features"):
        notes.append("global feature flags (features:) changed since baseline")
    if baseline_manifest.get("frozen_date") != current.get("frozen_date"):
        notes.append("frozen date differs from the baseline's")
    return notes


# ---------------------------------------------------------------------------
# Production wiring (imports src.* lazily so offline tests stay light)
# ---------------------------------------------------------------------------

@contextmanager
def freeze_prompt_date(frozen: date = FROZEN_DATE) -> Iterator[None]:
    """Make the prompt builder's datetime.date.today() return a fixed date."""
    from unittest import mock

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:  # type: ignore[override]
            return frozen

    shim = types.SimpleNamespace(date=_FrozenDate)
    with mock.patch("src.sql.prompt_builder.datetime", shim):
        yield


def _real_parts() -> tuple[Callable[[], Any], Callable[[Any, Any, Path], Any], Callable[[], None]]:
    """Return (build_real_router, retriever_factory, clear_cache) for erp_main."""
    from src.core.provider_client import ProviderRouter
    from src.rag.stages.s10_embeddings import EmbeddingService
    from src.rag.stages.s11_vector_store import QdrantStore
    from src.sql.learning.pattern_learner import PatternLearner
    from src.sql.pipeline import SQLRetriever

    embeddings = EmbeddingService()
    store = QdrantStore(embedding_service=embeddings)

    def retriever_factory(router: Any, _unused: Any, tmp: Path) -> Any:
        retriever = SQLRetriever(router, store, embeddings, db_id=DB_ID)
        # Learned patterns are local, untracked state: read/write an empty temp copy
        # so they can neither change the prompt nor be polluted by this run.
        retriever._pattern_learner = PatternLearner(
            db_id=DB_ID,
            learned_patterns_path=tmp / "learned_patterns.jsonl",
            metrics_path=tmp / "learned_metrics.json",
        )
        return retriever

    return ProviderRouter, retriever_factory, lambda: SQLRetriever.clear_result_cache(DB_ID)


def _selected_items() -> list[tuple[str, str]]:
    ids = load_question_ids()
    questions = load_questions()
    return [(qid, questions[qid]["question"]) for qid in ids]


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:  # LF always
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def _record(allow_errors: bool) -> int:
    items = _selected_items()
    router_cls, factory, clear_cache = _real_parts()
    real_router = router_cls()
    with tempfile.TemporaryDirectory() as td, freeze_prompt_date():
        tmp = Path(td)
        records = await run_questions(
            items,
            make_router=lambda: RecordingRouter(real_router),
            make_retriever=lambda r: factory(r, None, tmp),
            clear_cache=clear_cache,
        )
    bad = [
        r["id"]
        for r in records
        if "exception" in r
        or r.get("infra_error")
        or any("error" in c for c in r["calls"])
    ]
    if bad and not allow_errors:
        print(f"REFUSING to write baseline: LLM/infra errors during {bad}.")
        print("A baseline recorded while rate-limited is meaningless. Retry later, or pass --allow-errors.")
        return 1
    _write_jsonl(BASELINE_FILE, records)
    MANIFEST_FILE.write_text(
        json.dumps(build_manifest([r["id"] for r in records]), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Recorded {len(records)} questions -> {BASELINE_FILE}")
    for r in records:
        print(f"  {r['id']}: status={r.get('status')} llm_calls={len(r['calls'])} rows={r.get('row_count')}")
    return 0


async def _replay(strict_rows: bool) -> int:
    baseline = {r["id"]: r for r in _read_jsonl(BASELINE_FILE)}
    items = [(qid, q) for qid, q in _selected_items() if qid in baseline]
    _, factory, clear_cache = _real_parts()
    with tempfile.TemporaryDirectory() as td, freeze_prompt_date():
        tmp = Path(td)
        state = {"qid": ""}

        def make_router() -> ReplayRouter:
            # run_questions() creates the router just before running that question
            return ReplayRouter(baseline[state["qid"]]["calls"])

        results: list[dict[str, Any]] = []
        for qid, question in items:
            state["qid"] = qid
            one = await run_questions(
                [(qid, question)], make_router, lambda r: factory(r, None, tmp), clear_cache
            )
            results.append(one[0])

    manifest_old = json.loads(MANIFEST_FILE.read_text(encoding="utf-8")) if MANIFEST_FILE.exists() else {}
    drift = manifest_drift(manifest_old, build_manifest([i for i, _ in items]))
    lines = ["# erp_main regression replay", ""]
    lines += [f"NOTE: {d}" for d in drift] + ([""] if drift else [])
    fails = warns = 0
    for res in results:
        issues = compare_records(baseline[res["id"]], res, strict_rows=strict_rows)
        status = "FAIL" if any(lv == "FAIL" for lv, _ in issues) else ("WARN" if issues else "OK")
        fails += status == "FAIL"
        warns += status == "WARN"
        lines.append(f"{status:4} {res['id']}")
        for level, msg in issues:
            lines.append(f"     {level}: {msg}")
    lines += ["", f"{len(results)} questions: {len(results) - fails - warns} OK, {warns} WARN, {fails} FAIL"]
    text = "\n".join(lines)
    print(text)
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(text + "\n", encoding="utf-8", newline="\n")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("validate", help="offline: check question_ids.json against questions.jsonl")
    rec = sub.add_parser("record", help="run with the real LLM and write the baseline")
    rec.add_argument("--allow-errors", action="store_true", help="write the baseline even if LLM/infra errors occurred")
    rep = sub.add_parser("replay", help="run with recorded LLM answers and compare to the baseline")
    rep.add_argument("--strict-rows", action="store_true", help="treat any result-row change as FAIL")
    args = parser.parse_args(argv)

    if args.mode == "validate":
        problems = validate_ids()
        print("OK" if not problems else "\n".join(problems))
        return 1 if problems else 0

    os.chdir(REPO_ROOT)  # PatternLearner's default static-pattern path is cwd-relative
    if args.mode == "record":
        return asyncio.run(_record(args.allow_errors))
    return asyncio.run(_replay(args.strict_rows))


if __name__ == "__main__":
    sys.exit(main())
