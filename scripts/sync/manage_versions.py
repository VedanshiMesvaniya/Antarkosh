#!/usr/bin/env python3
"""List, inspect and roll back the kept versions of a database's generated files (auto-schema Phase 13).

    python scripts/sync/manage_versions.py list --db-id qts_ais
    python scripts/sync/manage_versions.py show --db-id qts_ais --version v0003
    python scripts/sync/manage_versions.py rollback --db-id qts_ais --version v0002

A rollback keeps the current files as a new version first (so it can be undone), refuses while a sync is running or if the
version fails its checksums, restores only files this project generated, and leaves the vector store alone: run a sync afterwards.
Stop the application first if a sync could start while you roll back (the lock only protects against syncs of the same database).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.sql.sync.versions import VersionError, list_versions, rollback_to_version, verify_version


def cmd_list(args: argparse.Namespace) -> int:
    versions = list_versions(args.db_id)
    if not versions:
        print(f"No kept versions for {args.db_id}.")
        return 0
    for v in versions:
        print(f"{v['id']}  {v.get('created_at', '?')}  tables={v.get('tables')}  files={len(v['files'])}  schema_hash={v.get('schema_hash')}  {v.get('note', '')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        manifest, problems = verify_version(args.db_id, args.version)
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({**manifest, "problems": problems}, indent=2))
    return 1 if problems else 0


def cmd_rollback(args: argparse.Namespace) -> int:
    try:
        result = rollback_to_version(args.db_id, args.version)
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name, fn, needs_version in (("list", cmd_list, False), ("show", cmd_show, True), ("rollback", cmd_rollback, True)):
        p = sub.add_parser(name)
        p.add_argument("--db-id", required=True)
        if needs_version:
            p.add_argument("--version", required=True, help="for example v0002")
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
