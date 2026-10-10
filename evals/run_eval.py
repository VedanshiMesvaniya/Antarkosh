"""Run the Antarkosh Text-to-SQL evaluation across databases.

Shared evaluation runner for multi-database benchmarks.

Two modes:

  --offline   Validate the question bank against the schema (every referenced
              table must exist) and print a coverage summary. Needs NO database,
              NO API keys — safe to run anywhere, including CI.

  (default)   Live eval: run every question through the real QueryPipeline, then
              grade each answer with an LLM-as-judge against its rubric and the
              expected SQL-vs-document route. Needs the live DB (data/live_data.db
              or MySQL) and provider API keys, exactly like the app.

Examples:
  python evals/run_eval.py --offline
  python evals/run_eval.py --db erp_main --offline
  python evals/run_eval.py --limit 10 --difficulty hard_twisted
  python evals/run_eval.py --out databases/erp_main/evals/results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.sql.knowledge.loaders import get_knowledge_path, DEFAULT_DB_ID


# ---------------------------------------------------------------------------
# Dataset loading + offline validation
# ---------------------------------------------------------------------------

def load_questions(questions_path: Path) -> list[dict]:
    with questions_path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def schema_table_names(schema_path: Path) -> set[str]:
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    return {t["name"] for t in data["tables"]}


def offline_validate(questions: list[dict], schema_path: Path) -> int:
    """Check every referenced table exists; print coverage. Returns exit code."""
    tables = schema_table_names(schema_path)
    problems: list[str] = []
    referenced: set[str] = set()

    for q in questions:
        for t in q.get("tables", []):
            referenced.add(t)
            if t not in tables:
                problems.append(f"  {q['id']}: unknown table {t!r}")

    by_diff = Counter(q["difficulty"] for q in questions)
    by_route = Counter(q["route"] for q in questions)
    by_domain = Counter(q["domain"] for q in questions)

    print(f"Questions            : {len(questions)}")
    print(f"Tables in schema     : {len(tables)}")
    print(f"Tables referenced    : {len(referenced)}  "
          f"({len(referenced)}/{len(tables)} = {len(referenced)/len(tables):.0%} coverage)")
    print(f"By difficulty        : {dict(by_diff)}")
    print(f"By expected route    : {dict(by_route)}")
    print(f"By domain            : {dict(by_domain)}")

    unreferenced = sorted(tables - referenced)
    if unreferenced:
        print(f"\nTables no question touches ({len(unreferenced)}):")
        print("  " + ", ".join(unreferenced))

    if problems:
        print(f"\nFAIL — {len(problems)} question(s) reference a non-existent table:")
        print("\n".join(problems))
        return 1

    print("\nOK — every referenced table exists in the schema.")
    return 0


# ---------------------------------------------------------------------------
# Live eval helpers
# ---------------------------------------------------------------------------

_SQL_LINE = re.compile(r"SQL Query Executed:\s*`(.+?)`", re.DOTALL)


def observed_route(answer: str, model_used: str) -> str:
    """Infer which route the pipeline actually took from its output."""
    used_sql = bool(_SQL_LINE.search(answer)) or model_used == "sql/direct"
    used_doc = "**References**" in answer or "References" in answer
    if used_sql and used_doc:
        return "BOTH"
    if used_sql:
        return "SQL"
    if not answer.strip() or "couldn't find" in answer.lower() or \
       "don't have" in answer.lower() or "no relevant" in answer.lower():
        return "ABSTAIN"
    return "DOC"


def route_matches(expected: str, observed: str) -> bool:
    if expected == observed:
        return True
    if expected == "SQL" and observed == "BOTH":
        return True
    return False


_JUDGE_SYSTEM = """You are an exacting evaluation judge for a enterprise Text-to-SQL system.
You will be given:
- the user's natural-language question
- the known schema trap or "twist" the question was designed to probe
- the grading rubric
- the generated SQL query (if one was run)
- the final answer presented to the user

Score the answer on a strict 0.0 to 1.0 scale:
  1.0 = Correct number/result, handled the twist cleanly, rubric fully satisfied.
  0.5 = Mostly correct, but stumbled on a secondary detail (e.g. wrong sort order,
        forgot a soft-delete filter, but got the core aggregate right).
  0.0 = Factually wrong number, query failed, wrong tables, hallucinated answer,
        or fell into the trap described in the twist.

Reply with STRICT JSON, no prose:
{"score": <float 0..1>, "handled_twist": <true|false>, "reasoning": "<=200 chars"}"""


def _judge_user(q: dict, generated_sql: str, answer: str) -> str:
    return (
        f"QUESTION: {q['question']}\n"
        f"KNOWN TWIST: {q['twist']}\n"
        f"RUBRIC: {q['rubric']}\n"
        f"GENERATED SQL: {generated_sql or '(none — no SQL was run)'}\n"
        f"ANSWER:\n{answer[:2000]}"
    )


async def run_live(questions: list[dict], judge_task: str, out_path: Path | None, db_id: str = DEFAULT_DB_ID) -> int:
    from src.pipeline.query import QueryPipeline

    pipeline = QueryPipeline()
    from src.core.provider_client import ProviderRouter
    judge = ProviderRouter()

    results: list[dict] = []
    for i, q in enumerate(questions, 1):
        rec = {"id": q["id"], "difficulty": q["difficulty"], "domain": q["domain"],
               "expected_route": q["route"], "question": q["question"]}
        try:
            res = await pipeline.query(q["question"], db_id=db_id)
            answer = res.answer or ""
            model_used = res.model_used or ""
            sql_match = _SQL_LINE.search(answer)
            generated_sql = sql_match.group(1).strip() if sql_match else ""
            obs = observed_route(answer, model_used)
            rec["observed_route"] = obs
            rec["route_ok"] = route_matches(q["route"], obs)
            rec["generated_sql"] = generated_sql

            verdict = await _grade(judge, judge_task, q, generated_sql, answer)
            rec.update(verdict)
        except Exception as e:
            rec.update({"observed_route": "ERROR", "route_ok": False,
                        "score": 0.0, "handled_twist": False,
                        "reasoning": f"pipeline error: {e}"})
        results.append(rec)
        _print_row(i, len(questions), rec)

    _summarize(results)
    if out_path:
        out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote detailed results -> {out_path}")
    return 0


async def _grade(judge, judge_task: str, q: dict, generated_sql: str, answer: str) -> dict:
    try:
        raw = await judge.chat(
            task=judge_task,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": _judge_user(q, generated_sql, answer)},
            ],
            max_tokens=200,
            temperature=0.0,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        return {
            "score": float(data.get("score", 0.0)),
            "handled_twist": bool(data.get("handled_twist", False)),
            "reasoning": str(data.get("reasoning", ""))[:300],
        }
    except Exception as e:
        return {"score": 0.0, "handled_twist": False, "reasoning": f"judge error: {e}"}


def _print_row(i: int, n: int, rec: dict) -> None:
    ok = "✓" if rec.get("route_ok") else "✗"
    print(f"[{i:>3}/{n}] {rec['id']}  score={rec.get('score', 0):.2f}  "
          f"route {ok} ({rec['expected_route']}->{rec.get('observed_route')})  "
          f"{rec['question'][:60]}")


def _summarize(results: list[dict]) -> None:
    n = len(results)
    if not n:
        return
    avg = sum(r.get("score", 0) for r in results) / n
    route_ok = sum(1 for r in results if r.get("route_ok")) / n
    twist_ok = sum(1 for r in results if r.get("handled_twist")) / n

    by_diff: dict[str, list[float]] = defaultdict(list)
    for r in results:
        by_diff[r["difficulty"]].append(r.get("score", 0))

    print("\n" + "=" * 60)
    print(f"Overall answer score : {avg:.2f}")
    print(f"Routing accuracy     : {route_ok:.0%}")
    print(f"Twist-handled rate   : {twist_ok:.0%}")
    print("Score by difficulty:")
    for diff, scores in sorted(by_diff.items()):
        print(f"  {diff:<14} {sum(scores)/len(scores):.2f}  (n={len(scores)})")

    worst = sorted(results, key=lambda r: r.get("score", 0))[:5]
    print("\nLowest-scoring questions:")
    for r in worst:
        print(f"  {r['id']} [{r.get('score',0):.2f}] {r['question'][:55]} — {r.get('reasoning','')[:60]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Antarkosh Text-to-SQL eval runner")
    ap.add_argument("--db", default=DEFAULT_DB_ID,
                    help=f"Database ID to evaluate (default: {DEFAULT_DB_ID})")
    ap.add_argument("--questions", type=Path, default=None,
                    help="Path to questions.jsonl file (default: databases/<db>/evals/questions.jsonl)")
    ap.add_argument("--schema", type=Path, default=None,
                    help="Path to schema.json file (default: resolved via get_knowledge_path)")
    ap.add_argument("--offline", action="store_true",
                    help="validate the dataset against the schema; no DB/keys needed")
    ap.add_argument("--limit", type=int, default=None, help="run only the first N questions")
    ap.add_argument("--difficulty", default=None, help="filter by difficulty tier")
    ap.add_argument("--domain", default=None, help="filter by domain")
    ap.add_argument("--judge-task", default="classification",
                    help="ProviderRouter task to use for the judge LLM")
    ap.add_argument("--out", type=Path, default=None, help="write detailed JSON results here")
    args = ap.parse_args()

    questions_path = args.questions or (REPO / "databases" / args.db / "evals" / "questions.jsonl")
    schema_path = args.schema or get_knowledge_path("schema", db_id=args.db)

    if not questions_path.is_file():
        print(f"Error: Questions file not found at {questions_path}", file=sys.stderr)
        return 1
    if not schema_path.is_file():
        print(f"Error: Schema file not found at {schema_path}", file=sys.stderr)
        return 1

    questions = load_questions(questions_path)
    if args.difficulty:
        questions = [q for q in questions if q["difficulty"] == args.difficulty]
    if args.domain:
        questions = [q for q in questions if q["domain"] == args.domain]
    if args.limit:
        questions = questions[: args.limit]

    if not questions:
        print("No questions match the given filters.")
        return 1

    if args.offline:
        return offline_validate(questions, schema_path)

    return asyncio.run(run_live(questions, args.judge_task, args.out, db_id=args.db))


if __name__ == "__main__":
    raise SystemExit(main())
