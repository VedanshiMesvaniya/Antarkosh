"""Show, and repair, the per-database feature flags in config/feature_flags.yaml.

For every registered database (or just ``--db <id>``) this makes sure the database has an entry with every flag, adding
any flag it lacks as ``true`` (values you set are never changed), then prints what each flag is NOW for that database, as
the application resolves it. Use it when a phase's output file is not appearing after a sync: the table shows which flag is
off, and for which database.

    python scripts/db/sync_feature_flags.py            # every registered database
    python scripts/db/sync_feature_flags.py --db qts_ais

Reads and writes only config/feature_flags.yaml (the local, git-ignored file). Never touches a database.
Note: loading the connection list already completes these entries, so any restart of the application does the same; this
command just does it on demand and shows the result.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.sql import registry
from src.utils import feature_flags as ff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="only this database id")
    args = parser.parse_args(argv)

    ids = [args.db] if args.db else [i for i in registry.list_connection_ids() if i not in ff._PINNED_DB_IDS]
    if not ids:
        print("No registered databases found in config/connections.json.")
        return 0
    for db_id in ids:
        changed = ff.register_db_feature_flags(db_id)
        print(f"\n{db_id}: {'flags added or completed' if changed else 'nothing to change'}")
        for flag in ff.DB_FEATURE_FLAGS:
            on = ff.is_db_feature_enabled(flag, db_id)
            print(f"  {flag:28} {'ON ' if on else 'OFF'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
