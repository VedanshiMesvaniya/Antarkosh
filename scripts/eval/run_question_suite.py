#!/usr/bin/env python3
"""Run a set of questions through the live pipeline and record what happened (auto-schema Phase 10, part 2).

It changes no setting and no code. Run it before and after a change, then compare the two files with ``compare_runs.py``
to see which questions got better, which got worse, and which are unchanged.

    python scripts/eval/run_question_suite.py --db-id qts_ais --questions evals/qts_ais_questions.json --out runs/before.json
    (apply the change, restart if code changed)
    python scripts/eval/run_question_suite.py --db-id qts_ais --questions evals/qts_ais_questions.json --out runs/after.json
    python scripts/eval/compare_runs.py --baseline runs/before.json --candidate runs/after.json

Question file: ``.json`` (a list), ``.jsonl`` or plain text with one question per line. An entry may be a string or an object
``{"id": "q1", "question": "...", "must_use_tables": ["a", "b"], "min_rows": 1, "expect_empty": false, "tags": ["multi-join"]}``.
Only ``question`` is required. A question counts as working when the pipeline answers with rows (``status=success``), and, if given,
returns at least ``min_rows`` rows and its SQL reads every table in ``must_use_tables``.

The model is not deterministic: for a fair comparison run the baseline twice (``before1.json``, ``before2.json``) and pass both
to ``--baseline``. Learned patterns (the pipeline records successful repairs and reuses them) are saved before the run and put
back afterwards, so one run cannot teach the next; pass ``--keep-learning`` to leave them as they end up.

Needs the same services as the application (database, Qdrant, model keys). Each question runs in its own asyncio task so the
per-question budget and query id never leak into the next one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from scripts.eval.suite_core import (  # noqa: E402
    SuiteQuestion,
    judge,
    load_questions,
    read_events,
    summarize_events,
    tables_in_sql,
)

Ask = Callable[[SuiteQuestion], Awaitable[dict[str, Any]]]


@contextmanager
def frozen_learning(db_id: str, enabled: bool = True) -> Iterator[None]:
    """Save the learned-pattern files before the run and put them back after it."""
    if not enabled:
        yield
        return
    from src.sql.learning.pattern_learner import PatternLearner

    learner = PatternLearner(db_id=db_id)
    paths = [learner.learned_patterns_write_path, learner.metrics_write_path]
    snapshot = {p: (p.read_bytes() if p.exists() else None) for p in paths}
    try:
        yield
    finally:
        for path, data in snapshot.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            except OSError as exc:
                print(f"warning: could not restore {path}: {exc}", file=sys.stderr)


def make_ask(retriever: Any) -> Ask:
    """The real question -> pipeline call. Runs inside its own task, so the query id and budget belong to this question only."""
    from src.utils.telemetry import get_or_create_query_id

    dialect = getattr(getattr(retriever, "_dialect", None), "sqlglot_dialect", None)

    async def ask(q: SuiteQuestion) -> dict[str, Any]:
        qid = get_or_create_query_id()
        started = time.perf_counter()
        error: str | None = None
        try:
            await retriever.retrieve(q.question)
        except Exception as exc:  # noqa: BLE001 - one failing question must not stop the run
            error = f"{type(exc).__name__}: {exc}"[:300]
        latency = round((time.perf_counter() - started) * 1000, 1)
        payload = getattr(retriever, "last_sql_payload", None) or {}
        status = "exception" if error else getattr(retriever, "last_query_status", None)
        if error is None and getattr(retriever, "last_infra_error", None):
            error = str(retriever.last_infra_error)[:300]
        sql = payload.get("query")
        row_count = payload.get("row_count")
        tables = tables_in_sql(sql, dialect)
        ok, reason = judge(q, status, row_count, tables)
        return {"query_id": qid, "status": status, "sql": sql, "row_count": row_count, "error": error, "tables": tables, "latency_ms": latency, "ok": ok, "reason": reason}

    return ask


async def run_suite(questions: list[SuiteQuestion], ask: Ask, *, progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for i, q in enumerate(questions, 1):
        record = await asyncio.create_task(ask(q))  # own task = own context
        record.update({"id": q.id, "question": q.question, "tags": list(q.tags)})
        results.append(record)
        if progress:
            progress(f"[{i}/{len(questions)}] {q.id}: {'ok' if record['ok'] else 'FAIL ' + record['reason']}")
    return results


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=_repo_root, timeout=10, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build_run(db_id: str, label: str, results: list[dict[str, Any]], telemetry_file: Path | None) -> dict[str, Any]:
    from src.utils.feature_flags import is_db_feature_enabled

    events = read_events(telemetry_file, {r["query_id"] for r in results}) if telemetry_file else {}
    for record in results:
        record["telemetry"] = summarize_events(events.get(record["query_id"], []))
    ok = sum(1 for r in results if r["ok"])
    return {
        "meta": {
            "db_id": db_id,
            "label": label,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git_commit": _git_commit(),
            "flags": {f: is_db_feature_enabled(f, db_id) for f in ("auto_prompt_inject", "auto_prompt_shadow", "auto_join_paths")},
            "questions": len(results),
            "ok": ok,
        },
        "results": results,
    }


async def _main_async(args: argparse.Namespace) -> int:
    from src.core.provider_client import ProviderRouter
    from src.rag.stages.s10_embeddings import EmbeddingService
    from src.rag.stages.s11_vector_store import QdrantStore
    from src.sql.pipeline import SQLRetriever
    from src.utils.telemetry import TELEMETRY_FILE

    questions = load_questions(Path(args.questions))
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print("no questions to run", file=sys.stderr)
        return 2
    retriever = SQLRetriever(ProviderRouter(), vector_store=QdrantStore(), embedding_service=EmbeddingService(), db_id=args.db_id)
    with frozen_learning(args.db_id, enabled=not args.keep_learning):
        results = await run_suite(questions, make_ask(retriever), progress=print)
    run = build_run(args.db_id, args.label or Path(args.out).stem, results, TELEMETRY_FILE)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{run['meta']['ok']}/{run['meta']['questions']} questions answered; run written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-id", required=True)
    ap.add_argument("--questions", required=True, help=".json / .jsonl / .txt question file")
    ap.add_argument("--out", required=True, help="where to write the run (json)")
    ap.add_argument("--label", help="name stored in the run (default: the file name)")
    ap.add_argument("--limit", type=int, help="run only the first N questions")
    ap.add_argument("--keep-learning", action="store_true", help="do not restore learned patterns afterwards")
    return asyncio.run(_main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
