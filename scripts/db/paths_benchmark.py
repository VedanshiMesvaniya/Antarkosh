#!/usr/bin/env python3
"""Measure the multi-join route search and the planner's score floor (auto-schema Phase 10, part 1).

Read-only: never writes, never connects to a database, changes no threshold.

Benchmark (default): on catalogs that declare their foreign keys, the routes found from the declared keys are the truth. Every
key is then hidden, the Phase 6 rules infer edges, Phase 8 searches routes over them, and the report says what share of the truth
routes is found and how many survive each score floor. The five catalogs shipped in tests/sql/fixtures/relationships are used.

    python scripts/db/paths_benchmark.py
    python scripts/db/paths_benchmark.py --edge-thresholds 0.45 0.60 0.70 --floors 0.30 0.40 0.45 --details
    python scripts/db/paths_benchmark.py --db-id my_db        # a database that declares its keys (from databases/my_db/schema/*.json)

Live check: for a real database, how many of the routes stored in databases/<db_id>/schema/join_paths.json clear each floor.
This is the number that shows how many routes a floor throws away on your own data.

    python scripts/db/paths_benchmark.py --live-check --db-id my_db

Add ``--json`` for machine-readable output.
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
from src.sql.paths.benchmark import (  # noqa: E402
    DEFAULT_EDGE_THRESHOLDS,
    DEFAULT_FLOORS,
    format_live_table,
    format_route_report,
    live_floor_table,
    run_route_case,
)
from src.sql.paths.writer import PATHS_REL  # noqa: E402
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
            if not schema.get("relationships"):
                print(f"{db_id}: declares no foreign keys, so there is no answer sheet to measure against (use --live-check)", file=sys.stderr)
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
    ap.add_argument("--floors", type=float, nargs="+", default=list(DEFAULT_FLOORS), help="route score floors to compare")
    ap.add_argument("--edge-thresholds", type=float, nargs="+", default=list(DEFAULT_EDGE_THRESHOLDS), help="score an inferred edge needs before routes may use it")
    ap.add_argument("--details", action="store_true", help="per catalog tables and score ranges")
    ap.add_argument("--live-check", action="store_true", help="floors against a real join_paths.json (needs --db-id)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    floors = tuple(args.floors)

    if args.live_check:
        if not args.db_id:
            ap.error("--live-check needs --db-id")
        base = Path(args.databases_dir) if args.databases_dir else registry.DATABASES_DIR
        out = {}
        for db in args.db_id:
            doc = _read(base / db / PATHS_REL)
            out[db] = live_floor_table(doc, floors) if doc is not None else None
        if args.json:
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            for db, table in out.items():
                print(f"{db}: " + (f"\n{format_live_table(table)}" if table is not None else f"no readable {PATHS_REL} (run a sync with auto_join_paths first)"))
        return 0 if all(v is not None for v in out.values()) else 2

    cases = list(_cases(args))
    if not cases:
        print("nothing to measure", file=sys.stderr)
        return 2
    results = [run_route_case(name, schema, inv, exclude=args.exclude, edge_threshold=t) for t in args.edge_thresholds for name, schema, inv in cases]
    if args.json:
        print(json.dumps([
            {"name": r.name, "edge_threshold": r.edge_threshold, "tables": r.tables, "truth_routes": len(r.truth), "found_routes": len(r.found),
             "edges_admitted": r.edges_admitted, "floors": {f"{f:.2f}": [vars(row) for row in r.at(f)] for f in floors}}
            for r in results
        ], indent=2, sort_keys=True))
    else:
        print(format_route_report(results, floors, details=args.details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
