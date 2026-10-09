# PHASE_REPORTS.md — Multi-DB Restructure

Chronological reports produced after completing each refactoring phase.

---

## Phase A Report: Safety Net & Reference Inventory

1. **What changed:**
   - Initialized `docs/refactor/BASELINE.md`, `PATH_CHANGES.md`, `FOUND_ISSUES.md`, `PHASE_REPORTS.md`, `REFERENCE_INVENTORY.md`.
   - Added refactor rules section to `CLAUDE.md`.
2. **PATH_CHANGES rows added:**
   - A-1 through A-4 (tracking documents).
3. **Verification results (a-g):**
   - a. Grep: no code moves in Phase A.
   - b. Compileall: passed.
   - c. Ruff: passed.
   - d. Import `src.main`: passed.
   - e. Pytest: 537 passed, 17 skipped, 1 xfailed, 0 failed.
   - f. CI scripts: passed.
   - g. Server health endpoint: verified.
4. **Anything left as a shim:** None (no moves in Phase A).
5. **FOUND_ISSUES additions:** Logged MSSQL/Oracle profile gap, hardcoded table names in `s12b`, path math fragility, and single-active-DB cache clearing.
6. **What the next phase will touch:** Phase B will centralize path constants in `src/core/config.py` and replace `__file__` math across `src/`, adding `scripts/_paths.py`.

---

## Phase B Report: One Source of Truth for Paths

1. **What changed:**
   - Added `DATABASES_DIR` and `SQLITE_DIR` to `src/core/config.py`.
   - Created `scripts/_paths.py` dynamic `REPO_ROOT` locator.
   - Replaced all `__file__`-based path math across `src/` (`src/main.py`, `src/core/db_config_file.py`, `src/core/sql_column_registry.py`, `src/core/sql_drift_validator.py`, `src/pipeline/schema_ingestion.py`, `src/stages/s12b_sql_retrieval.py`, `src/stages/s12_s13_s14_retrieval.py`, `src/utils/failure_capture.py`, `src/utils/feature_flags.py`, `src/utils/sql_safety.py`, `src/utils/telemetry.py`) with centralized constants from `src.core.config`.
   - Replaced path math in 10 `scripts/` and 2 `evals/` scripts with dynamic `REPO_ROOT` lookup.
2. **PATH_CHANGES rows added:**
   - Rows B-1 through B-15.
3. **Verification results (a-g):**
   - a. Grep: `parents[` and `.parent.parent` in `src/` returns 0 hits outside `src/core/config.py:16`.
   - b. Compileall / syntax: all python files compile with 0 errors.
   - c. Ruff: passed.
   - d. Import `src.main`: passed.
   - e. Pytest: 537 passed, 17 skipped, 8 deselected, 1 xfailed, 0 failed (exact match with baseline).
   - f. CI scripts: `sql_drift_validator.py` (zero drift) and `evals/Antarkosh/run_eval.py --offline` (all 163 questions valid) passed.
   - g. Server check: `/docs` and `/api/overview` returned HTTP 200.
4. **Anything left as a shim:** None (no moved modules yet; path constants consolidated in-place).
5. **FOUND_ISSUES additions:** None.
6. **What the next phase will touch:** Phase C: create `databases/_template/` and `databases/erp_main/`, copy schema/glossary/relationships/atlas files, implement `src/sql/knowledge/loaders.py` with fallback to `config/`, update build scripts to accept `--db`, and add identity test.

