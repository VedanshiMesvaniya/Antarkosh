#!/usr/bin/env python3
"""Show which tables a database's soft-delete filtering would cover, and preview it on a SQL statement.

    python scripts/db/show_soft_delete.py --db-id qts_ais
    python scripts/db/show_soft_delete.py --db-id qts_ais --sql "SELECT * FROM orders o LEFT JOIN items i ON i.order_id = o.id"

Review this before turning on ``auto_soft_delete_apply`` for a database: the column per table is worked out from the catalog
(``schema/schema.json``) by name AND type, plus the behavioral atlas, ``db.yaml`` and the optional curated
``semantics/soft_delete.json`` (``{"tables": {"t": {"column": "c", "kind": "char_flag"}}, "ignore": ["t2"]}``). A table that
should not be filtered goes in ``ignore``. Reads only files; never touches the database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.sql import registry
from src.sql.soft_delete_spec import discover_from_schema, enforce, load_specs, soft_delete_mode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-id", required=True)
    ap.add_argument("--sql", help="preview the filtering on this statement")
    ap.add_argument("--intent", default="ACTIVE_ONLY", choices=["ACTIVE_ONLY", "DELETED_ONLY", "INCLUDE_ARCHIVED"])
    args = ap.parse_args(argv)

    specs = load_specs(args.db_id)
    print(f"mode for {args.db_id}: {soft_delete_mode(args.db_id) or 'off'}   (apply needs the auto_soft_delete_apply flag)")
    if not specs:
        print("No soft-delete columns known. Is schema/schema.json there (auto_catalog)? Is every candidate column listed under 'ignore'?")
    for t in sorted(specs):
        print(f"  {specs[t].describe()}")

    try:
        doc = json.loads((registry.DATABASES_DIR / args.db_id / "schema" / "schema.json").read_text(encoding="utf-8"))
        for note in discover_from_schema(doc)[1]:
            print(f"  note: {note}")
    except (OSError, ValueError):
        pass

    if args.sql:
        dialect = "mysql"
        try:
            from src.sql.dialects import get_dialect_profile

            dialect = get_dialect_profile(registry.get_database(args.db_id).get("engine", "mysql")).sqlglot_dialect
        except Exception as exc:  # noqa: BLE001 - fall back to MySQL syntax
            print(f"(could not read this database's engine, previewing in MySQL syntax: {exc})")
        out, added = enforce(args.sql, args.intent, dialect, specs)
        print(f"\n{dialect} / {args.intent}:\n{out}\n")
        for a in added:
            print(f"  added: {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
