"""Helper for locating repository root dynamically from any script location."""

from __future__ import annotations

import sys
from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path(__file__).resolve().parent.parent


REPO_ROOT = find_repo_root()

# Ensure REPO_ROOT is on sys.path so scripts and evals can import src
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
