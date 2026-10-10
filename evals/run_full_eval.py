#!/usr/bin/env python3
"""Antarkosh Live Evaluation & Validator Hardening Suite Bridge (Shared Runner)."""

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Default to erp_main evaluation suite
BASELINE_V2_SCRIPT = REPO / "databases" / "erp_main" / "evals" / "baseline_v2" / "run_full_eval.py"

if __name__ == "__main__":
    import runpy
    sys.argv[0] = str(BASELINE_V2_SCRIPT)
    runpy.run_path(str(BASELINE_V2_SCRIPT), run_name="__main__")
