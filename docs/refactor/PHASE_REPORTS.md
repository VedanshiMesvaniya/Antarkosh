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

---

## Phase C Report: Knowledge Packs and Fallback Loaders

1. **What changed:**
   - Created directory scaffolds `databases/_template/` and `databases/erp_main/` with `schema/`, `semantics/`, `learned/` (with `.gitkeep`), and `evals/`.
   - Created `databases/README.md`, `databases/_template/db.yaml`, and `databases/erp_main/db.yaml`.
   - Copied 5 core knowledge files into `databases/erp_main/`:
     - `evals/Antarkosh/Antarkosh_schema.json` -> `databases/erp_main/schema/schema.json`
     - `config/sql_relationships.json` -> `databases/erp_main/schema/relationships.json`
     - `config/sql_glossary.json` -> `databases/erp_main/semantics/glossary.json`
     - `config/sql_column_glossary.json` -> `databases/erp_main/semantics/column_glossary.json`
     - `config/behavioral_schema_atlas.json` -> `databases/erp_main/semantics/behavioral_atlas.json`
   - Created `src/sql/knowledge/loaders.py` providing `get_database_knowledge_path(db_id, relative_path)` and `get_knowledge_path(kind, db_id)` with automatic fallback to legacy paths and warning logs.
   - Switched loaders in `src/stages/s12b_sql_retrieval.py` (`_get_raw_relationships`, `_load_glossary`, `_get_raw_column_glossary`, `_get_raw_behavioral_atlas`), `src/pipeline/schema_ingestion.py` (`schema_file`, `rel_path`), and `src/core/sql_drift_validator.py` (`SCHEMA_FILE`; `GLOSSARY_FILE` and `RELATIONSHIPS_FILE` were NOT switched in Phase C, scheduled for C2-2).
   - Updated 6 build and verification scripts (`build_sql_relationships.py`, `build_sql_glossary.py`, `build_behavioral_atlas.py`, `generate_behavioral_atlas.py`, `auto_harvest_metadata.py`, `verify_atlas.py`) to accept `--db <id>` (default `erp_main`) and target `databases/<id>/` while keeping old CLI flags working.
   - Created `tests/test_database_knowledge_parity.py` asserting byte-identity between old and new copies and testing loader fallback.
   - Added `config/connections.example.json` connection template.
   - Updated `.gitignore` (`databases/*/learned/*`, `!databases/*/learned/.gitkeep`, `config/connections.json`, `data/sqlite/`), `.dockerignore`, and `.github/workflows/ci.yml` (added JSON/YAML parse validation for `databases/`).
2. **PATH_CHANGES rows added:**
   - Rows C-1 through C-14.
3. **Verification results (a-g):**
   - a. Byte parity test: all 5 file pairs byte-identical; fallback loader verified.
   - b. Syntax/compileall: passed with 0 errors.
   - c. Ruff lint: passed.
   - d. Import `src.main`: passed.
   - e. Pytest: 546 passed (537 baseline + 9 new parity/fallback tests), 17 skipped, 8 deselected (`live`), 1 xfailed, 0 failed.
   - f. CI scripts: `sql_drift_validator.py` (zero drift) and `evals/Antarkosh/run_eval.py --offline` (163/163 valid) passed; CI yaml/json validator passed on all config and databases files.
   - g. Proof test: temporarily renaming old `config/sql_relationships.json` proved pipeline successfully reads from `databases/erp_main/schema/relationships.json` (294 relationships loaded), then restored. Server `/health` and `/api/overview` returned HTTP 200.
4. **Anything left as a shim:** Fallback logic in `src/sql/knowledge/loaders.py` and old copies in `config/` and `evals/Antarkosh/Antarkosh_schema.json` kept for backward compatibility until Phase I.
5. **FOUND_ISSUES additions:** None.
6. **What the next phase will touch:** Phase D: introduce `src/sql/engine.py` (Engine enum) and `src/sql/context.py` (DatabaseContext), re-key caches by `db_id`, add Qdrant payload `db_id` filter, and add two-database isolation tests.

---

## Step C2-1 Report: Docs, Inventory UTF-8, and Template Skeletons
- **Step ID**: C2-1
- **Commits**: 51b8037
- **Files Changed**: `docs/refactor/REFERENCE_INVENTORY.md`, `docs/refactor/FOUND_ISSUES.md`, `docs/refactor/PHASE_REPORTS.md`, `docs/refactor/PATH_CHANGES.md`, `docs/refactor/PROGRESS.md`, `.gitattributes`, `databases/erp_main/semantics/routing_hints.json`, and 6 files in `databases/_template/{schema,semantics}/`.
- **What Changed**: Converted `REFERENCE_INVENTORY.md` to clean UTF-8 text (null bytes removed; diff treats it as text); clarified paths in `config.py` and dialect stubs planned for Phase E in `FOUND_ISSUES.md`; corrected drift validator scope in Phase C report; added `routing_hints.json` to `erp_main` and skeleton json files to `_template/`; added knowledge freeze decision to `FOUND_ISSUES.md`.
- **Checks a-g**: a-g all passed; pytest: 546 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; exactly matches 546 passed from Phase C); zero drift; offline eval 163/163 passed; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to `config/` and `evals/Antarkosh/` (to be removed in Phase I).
- **Issues Found**: Added freeze rule to `FOUND_ISSUES.md` forbidding regeneration of knowledge files until Phase I parity test.
- **Next Step ID**: C2-2

---

## Step C2-2 Report: Switch Remaining Readers in src/ to Loaders
- **Step ID**: C2-2
- **Commits**: 305bad1
- **Files Changed**: `src/core/sql_column_registry.py`, `src/utils/sql_safety.py`, `src/core/sql_drift_validator.py`, `src/stages/s12b_sql_retrieval.py`, `src/pipeline/schema_ingestion.py`, `src/core/join_graph.py`, `src/core/result_validator.py`.
- **What Changed**: Switched all remaining legacy file readers in `src/` to `get_knowledge_path`; confirmed `join_graph.py` and `result_validator.py` access relationships via `s12b._get_raw_relationships` (not reading files directly); cleaned all stale docstrings/comments naming legacy files. Verified that `git grep` for legacy knowledge files across `src/` now matches only `src/sql/knowledge/loaders.py`.
- **Checks a-g**: a-g all passed; pytest: 546 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; matches last recorded count of 546 passed); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations (to be removed in Phase I).
- **Issues Found**: None.
- **Next Step ID**: C2-3

---

## Step C2-3 Report: Switch Non-src Readers to Loaders
- **Step ID**: C2-3
- **Commits**: 375a3df
- **Files Changed**: `scripts/verify_atlas.py`, `scripts/build_sql_relationships.py`, `scripts/auto_harvest_metadata.py`, `scripts/build_behavioral_atlas.py`, `scripts/build_sql_glossary.py`, `scripts/generate_behavioral_atlas.py`, `evals/Antarkosh/run_eval.py`, `evals/Antarkosh/baseline_v2/run_full_eval.py`, `tests/test_sql_retrieval.py`.
- **What Changed**: Switched all scripts and evals readers to `get_knowledge_path` (supporting optional `--db`), removed stale legacy references from script docstrings/defaults, verified `evals/Antarkosh/run_eval.py --offline` passes (163/163 valid), and added a non-skipping relationship knowledge file assertion in `tests/test_sql_retrieval.py`. Legacy names in `scripts/` and `evals/` now appear only in documentation/report text.
- **Checks a-g**: a-g all passed; pytest: 547 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +1 passed vs 546 last recorded count due to new non-skip test); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations (to be removed in Phase I).
- **Issues Found**: None.
- **Next Step ID**: C2-4

---

## Step C2-4 Report: Guard Test Against Legacy Knowledge Paths in src/
- **Step ID**: C2-4
- **Commits**: 3426347
- **Files Changed**: `tests/test_no_legacy_knowledge_paths.py`.
- **What Changed**: Added guard test `test_no_legacy_knowledge_paths_in_src` scanning every `.py` file under `src/` for forbidden legacy knowledge filenames (`sql_glossary.json`, `sql_column_glossary.json`, `sql_relationships.json`, `behavioral_schema_atlas.json`, `Antarkosh_schema.json`), excluding `src/sql/knowledge/loaders.py`. Confirmed it passes and fails if a forbidden string is injected into `src/`.
- **Checks a-g**: a-g all passed; pytest: 548 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +1 passed vs 547 last recorded count due to new guard test); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations (to be removed in Phase I).
- **Issues Found**: None.
- **Next Step ID**: D1



