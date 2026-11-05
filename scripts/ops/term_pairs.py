#!/usr/bin/env python3
"""Review and undo what Antarkosh learned from usage (Phase 14: user phrasing -> table/column).

    python scripts/ops/term_pairs.py show    --db-id qts_ais
    python scripts/ops/term_pairs.py revoke  --db-id qts_ais "vendor claim"                 # block the phrase for every target
    python scripts/ops/term_pairs.py revoke  --db-id qts_ais "qn" --target column:dbo.qn.qn_no   # block one pair only
    python scripts/ops/term_pairs.py restore --db-id qts_ais "vendor claim"                 # lift an earlier revoke
    python scripts/ops/term_pairs.py recompute --db-id qts_ais                              # rebuild term_pairs.json from the ledger

Reads and writes only ``databases/<db_id>/learned/`` (the evidence ledger and ``term_pairs.json``); never touches the database,
the generated schema files or the curated glossaries. Promoted pairs reach prompts only while ``auto_learning_apply`` is on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.sql.learning import term_pairs as tp


def _show(db_id: str) -> None:
    events = tp.read_ledger(db_id)
    promoted, pending = tp.evaluate(events)
    print(f"{db_id}: {len(events)} ledger events, {len(promoted)} promoted, {len(pending)} pending")
    print(f"thresholds: {json.dumps(tp.thresholds())}")
    print("\nPROMOTED (used in prompts when auto_learning_apply is on):")
    for s in promoted:
        print(f"  \"{s['phrase']}\" -> {s['target']}   x{s['confirmations']} over {s['days']} day(s), share {s['share']}, downs {s['downs']}")
    if not promoted:
        print("  (none)")
    print("\nPENDING (best candidate per phrase, not yet enough evidence):")
    for s in pending[:20]:
        print(f"  \"{s['phrase']}\" -> {s['target']}   x{s['confirmations']} over {s['days']} day(s), share {s['share']}, downs {s['downs']}")
    if not pending:
        print("  (none)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["show", "revoke", "restore", "recompute"])
    ap.add_argument("phrase", nargs="?")
    ap.add_argument("--db-id", required=True)
    ap.add_argument("--target", help="limit revoke/restore to one target, e.g. table:dbo.qn or column:dbo.qn.qn_no")
    args = ap.parse_args(argv)

    if args.command == "show":
        _show(args.db_id)
        return 0
    if args.command == "recompute":
        print(json.dumps(tp.recompute(args.db_id)))
        return 0
    if not args.phrase:
        ap.error(f"{args.command} needs a phrase")
    print(json.dumps(tp.set_revoked(args.db_id, args.phrase, args.target, revoke=args.command == "revoke")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
