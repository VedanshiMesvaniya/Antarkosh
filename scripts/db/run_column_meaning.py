"""Generate column meanings for one database now, with a larger budget than a schema sync allows.

A schema sync does at most a few dozen tables per run (so it never holds up the Sync button); on a large database the first
pass is therefore spread over several syncs. This command does the same work in one go:

    python scripts/db/run_column_meaning.py --db qts_ais                 # up to 1000 tables, 30 minutes
    python scripts/db/run_column_meaning.py --db qts_ais --max-tables 100 --budget-seconds 600

It only needs the files an earlier Sync Schema wrote (schema.json and friends) and a configured language model. Tables whose
evidence has not changed are skipped at no cost, so it is safe to run repeatedly. It writes semantics/column_dictionary.json
and the glossary entries; the next Sync Schema embeds the new meanings. It never touches the database itself.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.sql.sa import meaning_phase
from src.utils.feature_flags import is_db_feature_enabled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="database id")
    parser.add_argument("--max-tables", type=int, default=1000)
    parser.add_argument("--budget-seconds", type=float, default=1800.0)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)

    report = asyncio.run(
        meaning_phase.run_meaning_phase(
            db_id=args.db, max_tables=args.max_tables, budget_seconds=args.budget_seconds, concurrency=args.concurrency,
            inject=is_db_feature_enabled("auto_prompt_inject", args.db),
        )
    )
    report.pop("embed_tables", None)
    for key, value in report.items():
        print(f"{key}: {value}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
