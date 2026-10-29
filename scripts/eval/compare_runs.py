#!/usr/bin/env python3
"""Compare question-suite runs: what got better, what got worse (auto-schema Phase 10, part 2). Read-only.

    python scripts/eval/compare_runs.py --baseline runs/before1.json runs/before2.json --candidate runs/after.json

Exit code 1 when a question that worked in every baseline run fails in the candidate run (use it as a gate), 0 otherwise.
``--strict`` also fails when a question that still works now returns different rows or reads different tables.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from scripts.eval.suite_core import compare_runs, format_comparison, regression_exit_code  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", nargs="+", required=True, help="one or more runs of the code before the change")
    ap.add_argument("--candidate", required=True, help="the run after the change")
    ap.add_argument("--strict", action="store_true", help="also fail on changed rows or tables for answers that still work")
    ap.add_argument("--json", action="store_true", help="print the classification as json")
    args = ap.parse_args(argv)
    try:
        baselines = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.baseline]
        candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read a run file: {exc}", file=sys.stderr)
        return 2
    rows = compare_runs(baselines, candidate)
    if args.json:
        print(json.dumps([vars(r) for r in rows], indent=2))
    else:
        print(format_comparison(rows, baselines, candidate))
    return regression_exit_code(rows, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
