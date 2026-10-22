#!/usr/bin/env python3
"""Measure the relationship rules (auto-schema Phase 6, step 6i). Read-only: never writes, never connects to a database.

Benchmark (default): hide every foreign key a catalog declares, run the rules on what is left, and report how many
of the hidden keys come back, and what else the rules propose.

    python scripts/db/relationships_benchmark.py                       # the five catalogs shipped in tests/sql/fixtures/relationships
    python scripts/db/relationships_benchmark.py --db-id my_db         # your own database, from databases/my_db/schema/*.json
    python scripts/db/relationships_benchmark.py --db-id my_db --details --exclude '_p\\d{4}_\\d{2}$'

Live check: what the phase would do for a database right now, with its declared keys and procedure evidence, as counts.

    python scripts/db/relationships_benchmark.py --live-check --db-id my_db

Both need ``schema.json`` (run a sync with ``auto_catalog`` first). Add ``--json`` for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from scripts._paths import REPO_ROOT  # noqa: E402
from src.sql import registry  # noqa: E402
from src.sql.relationships.benchmark import DEFAULT_THRESHOLDS, format_report, live_check, run_case, total  # noqa: E402
from src.sql.relationships.model import INVENTORY_FILE, SCHEMA_FILE  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "sql" / "fixtures" / "relationships"


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cases(args: argparse.Namespace):
    if args.db_id:
        base = Path(args.databases_dir) if args.databases_dir else registry.DATABASES_DIR
        for db_id in args.db_id:
            schema = _read(base / db_id / SCHEMA_FILE)
            if schema is None:
                print(f"{db_id}: no readable {SCHEMA_FILE} (run a sync with auto_catalog first)", file=sys.stderr)
                continue
            yield db_id, schema, _read(base / db_id / INVENTORY_FILE)
        return
    folder = Path(args.fixtures) if args.fixtures else FIXTURES
    for schema_path in sorted(folder.glob("*.schema.json")):
        name = schema_path.name.removesuffix(".schema.json")
        yield name, json.loads(schema_path.read_text(encoding="utf-8")), _read(folder / f"{name}.inventory.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-id", action="append", help="a database under databases/ (repeatable)")
    ap.add_argument("--databases-dir", help="alternative databases/ folder")
    ap.add_argument("--fixtures", help="folder of <name>.schema.json / <name>.inventory.json pairs")
    ap.add_argument("--exclude", help="regex of table names to leave out (for example monthly partitions)")
    ap.add_argument("--thresholds", type=float, nargs="+", default=list(DEFAULT_THRESHOLDS))
    ap.add_argument("--details", action="store_true", help="list every missed key and every undeclared proposal")
    ap.add_argument("--live-check", action="store_true", help="counts the phase would produce now, keeping declared keys (needs --db-id)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.live_check:
        if not args.db_id:
            ap.error("--live-check needs --db-id")
        base = Path(args.databases_dir) if args.databases_dir else None
        out = {db: live_check(db, base) for db in args.db_id}
        print(json.dumps(out, indent=2, sort_keys=True) if args.json else "\n".join(f"{db}: {json.dumps(v, sort_keys=True)}" for db, v in out.items()))
        return 0 if all(v is not None for v in out.values()) else 2

    results = [run_case(name, schema, inv, exclude=args.exclude) for name, schema, inv in _cases(args)]
    if not results:
        print("nothing to measure", file=sys.stderr)
        return 2
    thresholds = tuple(args.thresholds)
    if args.json:
        print(json.dumps({
            "databases": {r.name: {"tables": r.tables, "declared": len(r.truth), "proposed": len(r.predicted), "at": {str(t): vars(r.at(t)) for t in thresholds}} for r in results},
            "total": {str(t): vars(total(results, t)) for t in thresholds},
        }, indent=2, sort_keys=True))
    else:
        print(format_report(results, thresholds, details=args.details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
