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

---

## Step D1 Report: Loader Hardening and Database ID Validation
- **Step ID**: D1
- **Commits**: e59165a
- **Files Changed**: `src/sql/knowledge/loaders.py`, `tests/test_database_knowledge_parity.py`, `docs/refactor/FOUND_ISSUES.md`.
- **What Changed**: Hardened knowledge loaders with `validate_db_id` (regex `^[a-z][a-z0-9_]{1,39}$`), path traversal prevention (rejecting `..` and absolute paths), and `KnowledgeFileNotFound` exception. Restricted legacy knowledge file fallback exclusively to `DEFAULT_DB_ID` (`erp_main`); missing files for other databases raise `KnowledgeFileNotFound`. Updated tests in `test_database_knowledge_parity.py` and resolved findings #9 and #10 in `FOUND_ISSUES.md`.
- **Checks a-g**: a-g all passed; pytest: 552 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +4 passed vs 548 last recorded count due to new hardening/traversal tests); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I).
- **Issues Found**: Resolved findings #9 and #10 in `FOUND_ISSUES.md`.
---

## Step D2 Report: Engine Enum Mapping
- **Step ID**: D2
- **Commits**: 5aa0a06
- **Files Changed**: `src/sql/engine.py`, `tests/test_sql_engine.py`, `src/core/sql_column_registry.py`, `src/stages/s12b_sql_retrieval.py`, `src/pipeline/schema_ingestion.py`.
- **What Changed**: Created `Engine(str, Enum)` with `sqlite`, `mysql`, `postgresql`, `mssql`, `oracle` having `.sqlglot` ("sqlite", "mysql", "postgres", "tsql", "oracle") and `.key` properties. Added `Engine.from_value()` accepting aliases (`"postgres"`, `"tsql"`), case/whitespace insensitively, and rejecting unrecognized dialects (including `mariadb`, which is not registered in `src/`). Replaced ad-hoc dialect strings/tuples with `Engine` comparisons in `ColumnRegistry`, `format_schema_rows`, `fetch_sqlite_foreign_keys`, `_split_schema_by_table`, and `sync_live_schema`. Left `src/core/sql_dialects.py` untouched for Phase E. Added comprehensive unit tests in `tests/test_sql_engine.py`.
- **Checks a-g**: a-g all passed; pytest: 559 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +7 passed vs 552 last recorded count due to new engine tests); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I).
- **Issues Found**: None.
- **Next Step ID**: D3

---

## Step D3 Report: DatabaseContext and Context Cache
- **Step ID**: D3
- **Commits**: a877abd
- **Files Changed**: `src/sql/context.py`, `tests/test_sql_context.py`.
- **What Changed**: Created frozen dataclass `DatabaseContext` (holding `db_id`, `engine`, `display_name`, `description`, paths for the 5 knowledge files, `soft_delete_column`, `routing_hints_path`, and properties `.behavioral_atlas_path` and `.paths`). Implemented `get_context(db_id=DEFAULT_DB_ID)` cached per `db_id` in `_CONTEXT_CACHE`, parsing configuration from `databases/<db_id>/db.yaml` via `validate_db_id` and knowledge loaders. Exported `DEFAULT_DB_ID` and added `clear_context_cache(db_id=None)`. Kept pipeline untouched per instructions. Added comprehensive tests in `tests/test_sql_context.py` verifying `erp_main` loading, unknown/invalid database error handling, context isolation, cache clearing, and immutability.
- **Checks a-g**: a-g all passed; pytest: 564 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +5 passed vs 559 last recorded count due to new context tests); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I).
- **Issues Found**: None.
- **Next Step ID**: D4

---

## Step D4 Report: Knowledge Caches Keyed by db_id
- **Step ID**: D4
- **Commits**: 5954519
- **Files Changed**: `src/stages/s12b_sql_retrieval.py`, `tests/test_sql_knowledge_caches.py`.
- **What Changed**: Keyed all module-level knowledge caches in `s12b_sql_retrieval.py` (`_get_raw_relationships`, `_load_relationships`, `_load_glossary`, `_get_raw_column_glossary`, `_get_raw_behavioral_atlas`, and `_get_tables_with_soft_delete`) by `db_id` defaulting to `DEFAULT_DB_ID` ("erp_main"). Implemented normalizer wrappers ensuring `f()` and `f("erp_main")` share cache entries, preserved `.cache_clear()` on public function names, and retained exact `erp_main` fallback behaviors. Added unit tests in `tests/test_sql_knowledge_caches.py` asserting exact golden baseline metric parity for `erp_main`, zero-sharing isolation between distinct database folders, and `cache_clear()` functionality.
- **Checks a-g**: a-g all passed; pytest: 567 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +3 passed vs 564 last recorded count due to new knowledge cache isolation tests); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I).
- **Issues Found**: None.
- **Next Step ID**: D5

---

## Step D5 Report: SQLRetriever and SemanticCache Keyed by db_id
- **Step ID**: D5
- **Commits**: 69def1f
- **Files Changed**: `src/stages/s12b_sql_retrieval.py`, `src/utils/semantic_cache.py`, `src/pipeline/query.py`, `tests/test_sql_retrieval.py`, `tests/test_sql_retriever_multi_db_cache.py`.
- **What Changed**: Keyed `SQLRetriever._result_cache` by `(db_id, normalized question)` and `_full_schema_cache` / `_column_registry` by `db_id`. Added `db_id` constructor parameter defaulting to `DEFAULT_DB_ID` ("erp_main"). Added `column_registry` property per retriever and updated `clear_result_cache` / `clear_schema_cache` to support both selective `db_id` and global clearing. Scoped `SemanticCache` entries by `db_id` via `build_scope_key`, and prefixed query pipeline cache scopes with `target_db_id`. Retained global cache wiping in `db_settings._apply_runtime`. Added comprehensive unit tests in `tests/test_sql_retriever_multi_db_cache.py` verifying multi-db result and schema cache isolation, cache clearing, and semantic cache scoping.
- **Checks a-g**: a-g all passed; pytest: 571 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +4 passed vs 567 last recorded count due to new multi-db cache tests); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I).
- **Issues Found**: None.
- **Next Step ID**: D6

---

## Step D6 Report: Qdrant Schema Chunks Tagged and Filtered by db_id
- **Step ID**: D6
- **Commits**: 5b58d8a
- **Files Changed**: `src/models/schemas.py`, `src/pipeline/schema_ingestion.py`, `src/stages/s11_vector_store.py`, `src/stages/s12b_sql_retrieval.py`, `src/api/ui.py`, `tests/test_schema_retrieval_multi_db.py`.
- **What Changed**: Tagged schema chunks in `sync_live_schema` with document_id `"schema:<db_id>"`, `chunk_id=f"{db_id}_schema_{table_name}"`, and payload `db_id`. In `s12b_sql_retrieval.py`, updated hybrid search and post-filtering to always filter on `db_id`, implementing the transition rule where legacy chunks (`document_id="live_db_schema"`, no `db_id`) are accepted ONLY for `db_id == "erp_main"` until re-synced. Extended `Chunk` model and `QdrantStore` payload/filter logic for `db_id`. Updated `/settings/sync-schema` endpoint to accept optional validated `db_id`. Added comprehensive tests in `tests/test_schema_retrieval_multi_db.py` verifying cross-database schema RAG isolation on identical table names, legacy chunk transition acceptance, and `sync_live_schema` tagging.
- **Checks a-g**: a-g all passed; pytest: 574 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +3 passed vs 571 last recorded count due to new multi-db schema retrieval tests); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I). Transition rule in `s12b` and `s11` accepting legacy `live_db_schema` chunks for `erp_main` until re-synced.
- **Issues Found**: None.
- **Next Step ID**: D7

### Deployed Environment Re-Sync Procedure (Admin Schema-Sync)
To re-sync schema chunks in a deployed environment without downtime:
1. Log in with an administrator account to obtain an authenticated session cookie or API token.
2. Trigger the schema sync endpoint for `erp_main` (or trigger via the Web UI Settings panel):
   `curl -X POST "https://<HOST>/api/ui/settings/sync-schema?db_id=erp_main" -H "Cookie: session_id=<ADMIN_SESSION>"`
3. For each additional database configured in the cluster (`<db_id>`):
   `curl -X POST "https://<HOST>/api/ui/settings/sync-schema?db_id=<db_id>" -H "Cookie: session_id=<ADMIN_SESSION>"`
4. Verify HTTP 200 response returning `{"status": "ok", "db_id": "<db_id>", "document_id": "schema:<db_id>", "tables_synced": N, "table_names": [...]}`.
5. In Qdrant, points are atomically upserted with `document_id: "schema:<db_id>"` and payload `db_id: "<db_id>"`.
6. Once re-synced, legacy points with `document_id: "live_db_schema"` can optionally be deleted via Qdrant point selector, though the transition filter automatically prioritizes the new tagged chunks.

---

## Step D7 Report: Thread db_id Through API and Pipeline, Metrics Field
- **Step ID**: D7
- **Commits**: d4046a4
- **Files Changed**: `src/core/pipeline_metrics.py`, `src/api/query.py`, `src/api/ui.py`, `src/pipeline/query.py`, `src/stages/s12b_sql_retrieval.py`, `tests/test_api_query_db_id.py`.
- **What Changed**: Added optional `db_id` parameter to `QueryRequest` (/api/query) and `SendMessage` (/api/chats/{chat_id}/messages and /stream). Validates `db_id` using `validate_db_id` returning HTTP 400 on invalid input, and defaults absent `db_id` to `DEFAULT_DB_ID` ("erp_main"). Threaded `db_id` through `QueryPipeline` and dynamically resolved `SQLRetriever` instances per target database. Propagated `db_id` to `PipelineEvent` and `log_event()` in `pipeline_metrics.py` via `CURRENT_DB_ID` ContextVar and explicit arguments so every record in `pipeline_metrics.jsonl` contains `"db_id"`. Added comprehensive tests in `tests/test_api_query_db_id.py` verifying absent default, valid propagation, 400 rejection, and metric logging.
- **Checks a-g**: a-g all passed; pytest: 586 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +12 passed vs 574 last recorded count); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I). Transition rule in `s12b` and `s11` accepting legacy `live_db_schema` chunks for `erp_main` until re-synced.
- **Issues Found**: None.
- **Next Step ID**: E1

---

## Step E1 Report: Database Connectors (Protocol & per-engine extraction)
- **Step ID**: E1
- **Commits**: 00e709f, d4f3b35, 5aee3dc, 23f6af7, 481aac9
- **Files Changed**: `src/sql/connectors/base.py`, `src/sql/connectors/sqlite.py`, `src/sql/connectors/mysql.py`, `src/sql/connectors/postgresql.py`, `src/sql/connectors/mssql.py`, `src/sql/connectors/oracle.py`, `src/sql/connectors/__init__.py`, `src/core/db_client.py`, `tests/test_sql_connectors.py`.
- **What Changed**: Created `src/sql/connectors/` with `Connector` Protocol (`test()`, `run_readonly()`) in `base.py` and dedicated connectors for SQLite (URI ro mode), MySQL (read-only credentials), PostgreSQL (read-only transactions and statements timeout), MSSQL (pyodbc and odbc_driver configuration), and Oracle (oracledb with lower-cased columns). Preserved exact query timeouts, cartesian explosion clamping, and AST LIMIT enforcement in `src/core/db_client.py:run_readonly_query`. Refactored `db_client._execute` into a thin dispatcher delegating to `get_connector(engine).run_readonly`. One commit per engine was made. Added unit tests in `tests/test_sql_connectors.py` validating protocol compliance and query execution.
- **Checks a-g**: a-g all passed; pytest: 599 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +13 passed vs 586 last recorded count); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I). Transition rule in `s12b` and `s11` accepting legacy `live_db_schema` chunks for `erp_main` until re-synced.
- **Issues Found**: Finding #11: `test_telemetry_latency_under_5ms_guardrail_4` jitter under heavy test suite load (logged in FOUND_ISSUES.md).
- **Next Step ID**: E2

---

## Step E2 Report: SQL Dialects (Base profile, per-engine modules, stubs, and shim)
- **Step ID**: E2
- **Commits**: 5b9ba01
- **Files Changed**: `src/sql/dialects/base.py`, `src/sql/dialects/sqlite.py`, `src/sql/dialects/mysql.py`, `src/sql/dialects/postgresql.py`, `src/sql/dialects/mssql.py`, `src/sql/dialects/oracle.py`, `src/sql/dialects/__init__.py`, `src/core/sql_dialects.py`, `tests/test_sql_dialects.py`.
- **What Changed**: Extracted `src/sql/dialects/` package containing `SQLDialectProfile` in `base.py`, engine-specific profiles for `sqlite`, `mysql`, and `postgresql`, and `mssql.py` and `oracle.py` stubs whose `get_profile()` raises `NotImplementedError("needs validation against a real instance")`. Stubs are NOT registered in `DIALECTS`, maintaining strict backward compatibility where `get_dialect_profile("mssql")` and `get_dialect_profile("oracle")` raise `ValueError`. Replaced `src/core/sql_dialects.py` with a thin backward-compatible shim re-exporting `DIALECTS`, `SQLDialectProfile`, and `get_dialect_profile` for its 5 existing importers. Added pinned tests in `tests/test_sql_dialects.py`.
- **Checks a-g**: a-g all passed; pytest: 603 passed, 17 skipped, 8 deselected, 1 xfailed (0 failed; +4 passed vs 599 last recorded count); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (to be removed in Phase I); transition rule in `s12b` and `s11` accepting legacy `live_db_schema` chunks for `erp_main` until re-synced; `src/core/sql_dialects.py` shim re-exporting `DIALECTS`, `SQLDialectProfile`, and `get_dialect_profile` (to be removed in Phase I).
- **Issues Found**: Finding #12: `test_shadow_guards_latency_budget` jitter under heavy test suite load (logged in FOUND_ISSUES.md).
- **Next Step ID**: E3

---

## Step E3 Report: Database Registry & Connections Persistence
- **Step ID**: E3
- **Commits**: 25a13bf
- **Files Changed**: `src/sql/registry.py`, `src/core/db_config_file.py`, `src/sql/context.py`, `tests/test_sql_registry.py`.
- **What Changed**: Added `src/sql/registry.py` providing `list_databases()`, `get_database(db_id)`, `save_connection(db_id, fields)`, and `get_connection(db_id)`. Connections are atomically written to `config/connections.json` with `0o600` permissions via `tempfile.mkstemp` and `os.replace` without logging credentials. On first use, missing `connections.json` is bootstrapped by importing existing `config/db_connection.json` (or `DB_*` settings) into `connections.json` as `erp_main` once. `db.yaml` loading validates database engine with `Engine`. Kept `src/core/db_config_file.py` as a backward-compatible shim delegating `read`, `write`, and `apply_to` to `registry` for `erp_main`. Wired `src/sql/context.py` to delegate `db.yaml` loading to `get_database()`. Added unit tests in `tests/test_sql_registry.py`.
- **Checks a-g**: a-g all passed; pytest: 618 passed, 18 skipped, 8 deselected, 1 xfailed (+15 passed vs 603 last recorded count); zero drift; offline eval 163/163 valid; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I).
- **Issues Found**: None new (Findings #11 and #12 timing jitter on full suite run remain documented).
- **Next Step ID**: E4

---

## Step E4 Report: Move Engine Form Spec and Connector Test Delegation
- **Step ID**: E4
- **Commits**: cba67de
- **Files Changed**: `src/sql/engine.py`, `src/core/db_settings.py`, `tests/test_sql_engine.py`, `tests/test_sql_connectors.py`.
- **What Changed**: Moved the engine form spec `ENGINES` and `REQUIRED_FIELDS` into `src/sql/engine.py`. Delegated the per-engine connection test logic in `src/core/db_settings.py` (`_test_mysql`, `_test_postgresql`, `_test_mssql`, `_test_oracle`, and `_test_connection`) to the matching connector `test()` implementations. Preserved all public functions in `src/core/db_settings.py` with identical signatures, behavior, and JSON structure. In `db_settings.save()`, mirrored connections to `config/connections.json` via `registry.save_connection(DEFAULT_DB_ID, updates)`. Verified `tests/test_db_settings.py` passes completely unmodified.
- **Checks a-g**: a-g all passed; pytest: 621 passed, 18 skipped, 8 deselected, 1 xfailed (+3 passed vs 618 last recorded count); zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I).
- **Issues Found**: None new (Findings #11 and #12 timing jitter on full suite run remain documented).
- **Next Step ID**: E5

---

## Step E5 Report: Dynamic Connection Resolution and Per-Database Cache Invalidation
- **Step ID**: E5
- **Commits**: 622dd28
- **Files Changed**: `src/core/db_client.py`, `src/core/db_settings.py`, `src/sql/connectors/__init__.py`, `src/sql/connectors/mssql.py`, `src/sql/connectors/mysql.py`, `src/sql/connectors/oracle.py`, `src/sql/connectors/postgresql.py`, `src/sql/connectors/sqlite.py`, `src/sql/knowledge/loaders.py`, `src/sql/registry.py`, `src/stages/s12b_sql_retrieval.py`, `src/utils/semantic_cache.py`, `tests/test_sql_retriever_multi_db_cache.py`, `tests/test_sql_db_connection_invalidation.py`.
- **What Changed**: Threaded `db_id` through `run_readonly_query()` and `SQLRetriever` to dynamically resolve connection settings from `src.sql.registry` (`get_connection` / `get_database`) with fallback to `DEFAULT_DB_ID` ("erp_main"). Added configuration override support `cfg` across all engine connectors. In `db_settings._apply_runtime()`, replaced the global cache wipe with targeted per-`db_id` invalidation (`clear_schema_cache(target_db_id)`, `clear_result_cache(target_db_id)`, `clear_cache(target_db_id)`, and `clear_knowledge_caches(target_db_id)`). Scoped semantic cache purges by `db_id`. Fixed circular import in `registry.py` and `loaders.py` with `src.core.config`. Added comprehensive tests in `tests/test_sql_db_connection_invalidation.py` asserting connection resolution by `db_id` and verifying changing a connection for database A clears database A's caches while database B's caches survive. Verified `tests/test_db_settings.py` passes completely unmodified.
- **Checks a-g**: a-g all passed; pytest: 625 passed, 18 skipped, 8 deselected, 1 xfailed (+4 passed vs 621 baseline); zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I).
- **Issues Found**: None new (Findings #11 and #12 timing jitter on full suite run remain documented).
- **Next Step ID**: E6

---

## Step E6 Report: Database Access Control and Admin Endpoints
- **Step ID**: E6
- **Commits**: d9f6606
- **Files Changed**: `src/sql/registry.py`, `src/sql/context.py`, `src/stages/s12b_sql_retrieval.py`, `src/pipeline/query.py`, `src/api/query.py`, `src/api/ui.py`, `tests/test_sql_database_access_control.py`.
- **What Changed**: Enforced `allowed_users` access control from `databases/<id>/db.yaml` following the document-access pattern with admin account always allowed. Added `can_access_database`, `get_database_access`, `update_database_access`, and `atomic_write_yaml` in `src/sql/registry.py`. Added `allowed_users` and `can_access()` method to `DatabaseContext` in `src/sql/context.py`. Filtered `list_databases` so non-admin users cannot see databases they lack access to. Added access enforcement in `SQLRetriever.retrieve()` and `fetch_sqlite_foreign_keys()` so unauthorized databases return empty results. Added permission checks to `query_documents` (/api/query), `send_message`, and `send_message_stream` on explicit `db_id` (returning 403 Forbidden on unauthorized access). Added additive admin-only endpoints: `GET /databases` (user-filtered list), `GET /admin/databases` (admin list), `GET /databases/{id}` (single db), `GET /databases/{id}/access`, `POST /databases/{id}/access`, `POST /databases/{id}/test` (and connection/test), `POST /databases/{id}/connection` (and save). Added comprehensive tests in `tests/test_sql_database_access_control.py` mirroring `tests/test_alpha_auth_and_isolation.py`. Verified existing `tests/test_alpha_auth_and_isolation.py` and `tests/test_db_settings.py` pass completely unmodified.
- **Checks a-g**: a-g all passed; pytest: 634 passed, 18 skipped, 8 deselected, 1 xfailed (+9 passed vs 625 baseline); zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I).
- **Issues Found**: None new (Findings #11 and #12 timing jitter on full suite run remain documented).
- **Next Step ID**: F1

---

## Step F1 Report: Soft-Delete Detection and Filter Extraction
- **Step ID**: F1
- **Commits**: 816be00
- **Files Changed**: `src/sql/soft_delete.py`, `src/stages/s12b_sql_retrieval.py`, `tests/test_sql_soft_delete.py`.
- **What Changed**: Extracted soft-delete concern into new module `src/sql/soft_delete.py`, including `FALLBACK_SOFT_DELETE_TABLES` (40 known tables with deleted_at), `detect_soft_delete_intent(query)`, `_get_tables_with_soft_delete(db_id)`, `_clear_soft_delete_tables_cache(db_id)`, and `enforce_soft_delete_filter(sql, intent, dialect, db_id)`. Re-exported all soft-delete functions and cache symbols in `src/stages/s12b_sql_retrieval.py` preserving exact signatures and attributes (`cache_clear`). Captured golden snapshot and created `tests/test_sql_soft_delete.py` validating golden outputs for 10 representative intent queries, fallback table resolution, and 6 SQL AST filter rewrites across both direct and re-exported APIs.
- **Checks a-g**: a-g all passed; pytest: 652 passed, 18 skipped, 8 deselected, 1 xfailed (+18 passed vs 634 baseline); zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I); `src/stages/s12b_sql_retrieval.py` re-exporting soft-delete symbols from `src.sql.soft_delete` (Phase I).
- **Issues Found**: None.
- **Next Step ID**: F2

---

## Step F2 Report: Analytical Intent Extraction
- **Step ID**: F2
- **Commits**: 4837b9b
- **Files Changed**: `src/sql/intent.py`, `src/stages/s12b_sql_retrieval.py`, `tests/test_sql_intent.py`.
- **What Changed**: Extracted analytical intent extraction into new module `src/sql/intent.py`, providing `extract_analytical_intent(query)`. Parsed metrics, dimensions, filters, relative time periods, aggregations, limits/orderings, temporal scope, and soft-delete intent. Re-exported `extract_analytical_intent` in `src/stages/s12b_sql_retrieval.py` preserving exact signature and behavior for internal methods and external scripts (`scripts/test_question.py`). Captured golden snapshot and created `tests/test_sql_intent.py` validating golden outputs across 8 representative queries and asserting exact object identity on re-export.
- **Checks a-g**: a-g all passed; pytest: 662 passed, 18 skipped, 8 deselected, 1 xfailed (+10 passed vs 652 baseline); zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I); `src/stages/s12b_sql_retrieval.py` re-exporting soft-delete and intent symbols (Phase I).
- **Issues Found**: None.
- **Next Step ID**: F3

---

## Step F3 Report: Table Routing and Keyword Maps
- **Step ID**: F3
- **Commits**: ac1fa08
- **Files Changed**: `databases/erp_main/semantics/routing_hints.json`, `src/sql/table_router.py`, `src/stages/s12b_sql_retrieval.py`, `tests/test_sql_table_router.py`.
- **What Changed**: Moved hardcoded ERP table lists out of `src/stages/s12b_sql_retrieval.py` into `databases/erp_main/semantics/routing_hints.json` (`fallback_rules` with 9 domain mappings and `anchor_rules` with 19 keyword mappings). Extracted database-agnostic table routing into `src/sql/table_router.py` providing `load_routing_hints(db_id)`, `route_tables_for_query(query, db_id, section)`, `route_anchor_tables(query, db_id)`, and `_clear_routing_hints_cache(db_id)`. Verified zero domain table names exist in `src/sql/table_router.py`. Non-erp_main databases gracefully handle missing routing hints without crashing. Re-exported all routing functions and cache symbols in `src/stages/s12b_sql_retrieval.py`, and updated `clear_knowledge_caches(db_id)` to invalidate routing hints. Captured golden routing snapshot and created `tests/test_sql_table_router.py` validating golden parity across 10 representative user queries, asserting zero table name literals, testing cache invalidation, and verifying exact object identity on re-export.
- **Checks a-g**: a-g all passed; pytest: 675 passed, 18 skipped, 8 deselected, 1 xfailed (+13 passed vs 662 baseline); zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I); `src/stages/s12b_sql_retrieval.py` re-exporting soft-delete, intent, and table_router symbols (Phase I).
- **Issues Found**: None.
- **Next Step ID**: F4

---

## Step F4 Report: Schema Retrieval from Qdrant
- **Step ID**: F4
- **Commits**: F4
- **Files Changed**: `src/sql/schema_retrieval.py`, `src/stages/s12b_sql_retrieval.py`, `tests/test_schema_budget.py`, `tests/test_schema_compactor.py`, `tests/test_sql_schema_retrieval.py`.

- **What Changed**: Extracted schema retrieval and DDL parsing into new module `src/sql/schema_retrieval.py`, providing `extract_schema_table_names`, `extract_table_ddl_map`, `get_1hop_neighbors`, `format_scoped_relationships`, `build_scoped_schema_fallback`, and `retrieve_schema_from_qdrant`. Maintained Qdrant hybrid search, cross-database chunk isolation defense, domain anchor table injection, 1-hop relationship expansion, dynamic token budget selection with telemetry, and optional schema compaction. Re-exported all functions and underscore aliases in `src/stages/s12b_sql_retrieval.py`, delegating `SQLRetriever._get_schema` directly to `retrieve_schema_from_qdrant`. Updated patch targets in `test_schema_budget.py` and `test_schema_compactor.py` to `src.sql.schema_retrieval`. Added comprehensive tests in `tests/test_sql_schema_retrieval.py` verifying exact re-export object identity, DDL parsing, 1-hop graph expansion, scoped relationships formatting, Qdrant fallback & database isolation, and golden fallback schema parity across 10 representative queries.
- **Checks a-g**: a-g all passed; pytest: 683 passed, 18 skipped, 8 deselected, 1 xfailed (+8 passed vs 675 baseline); ruff clean; compileall clean; import src.main ok; zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I); `src/stages/s12b_sql_retrieval.py` re-exporting soft-delete, intent, table_router, and schema_retrieval symbols (Phase I).
- **Issues Found**: None.
- **Next Step ID**: F5

---

## Step F5 Report: Prompt Building and Knowledge Cache Loaders
- **Step ID**: F5
- **Commits**: F5
- **Files Changed**: `src/sql/prompt_builder.py`, `src/stages/s12b_sql_retrieval.py`, `src/sql/schema_retrieval.py`, `src/sql/soft_delete.py`, `tests/test_sql_prompt_builder.py`.
- **What Changed**: Extracted prompt assembly and knowledge cache loaders from `s12b_sql_retrieval.py` into new module `src/sql/prompt_builder.py`. Moved `get_raw_relationships`, `load_relationships`, `load_glossary`, `get_raw_column_glossary`, `get_raw_behavioral_atlas`, per-db cache invalidation methods, and `clear_prompt_caches`. Moved glossary matching routines (`stem_word`, `matches_glossary_candidate`, `build_column_glossary_for_query`), behavioral atlas builder (`build_behavioral_atlas_for_query`), scoped readability rules (`get_scoped_readability_rules`, `OUTPUT_READABILITY_RULES`), and prompt assembly (`build_sql_prompt`). Re-exported all prompt builder functions, caches (both public and underscore names), and readability rules in `src/stages/s12b_sql_retrieval.py`, delegating `SQLRetriever._generate_sql` to `build_sql_prompt` and `SQLRetriever._get_scoped_readability_rules` to `get_scoped_readability_rules`. Updated `clear_knowledge_caches(db_id)` to call `clear_prompt_caches(db_id)`. Updated lazy imports in `schema_retrieval.py` and `soft_delete.py` to point directly to `src.sql.prompt_builder`. Added unit tests in `tests/test_sql_prompt_builder.py` covering re-export object identity, stemming rules, glossary candidate matching, scoped readability rules, prompt assembly, per-db cache isolation and clearing, and `s12b.clear_knowledge_caches` delegation.
- **Checks a-g**: a-g all passed; pytest: 690 passed, 18 skipped, 8 deselected, 1 xfailed (+7 passed vs 683 baseline); ruff clean; compileall clean; import src.main ok; zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I); `src/stages/s12b_sql_retrieval.py` re-exporting soft-delete, intent, table_router, schema_retrieval, and prompt_builder symbols (Phase I).
- **Issues Found**: None.
- **Next Step ID**: F6

---

## Step F6 Report: SQL Generation, Execution Glue, and Repair Calls
- **Step ID**: F6
- **Commits**: F6
- **Files Changed**: `src/sql/generation.py`, `src/stages/s12b_sql_retrieval.py`, `src/sql/prompt_builder.py`, `tests/test_sql_generation.py`.
- **What Changed**: Extracted SQL generation, execution retry loops, and repair calls from `s12b_sql_retrieval.py` into new module `src/sql/generation.py`. Moved `generate_sql` (LLM prompt invocation, CoT separation, AST syntax validation, join complexity check, soft-delete enforcement, compressed prompt retry on budget limits), `execute_with_retry` (full retry loop with column/alias/semantic validation, execution, empty result retry, confidence scoring, pattern learning, markdown formatting), `execute_with_delta_repair` (targeted Delta Repair loop for AST safety, column, alias, semantic, empty result, and execution errors), AST safety verification (`is_safe_read_query`, `UnsafeQueryError`, `DANGEROUS_FUNCTIONS`), parsing/formatting helpers (`unwrap_sql`, `extract_cot_and_sql`, `format_schema_rows`, `format_fk_rows`, `format_rows_as_markdown`, `filter_display_rows`, `sanitize_rows`, `is_all_null`, `is_aggregate_over_zero_rows`, `extract_table_names`), and `fetch_sqlite_foreign_keys`. Re-exported all generation symbols, helpers, and caches in `s12b_sql_retrieval.py`, delegating `SQLRetriever._generate_sql` to `generate_sql`, `_is_safe_read_query` to `is_safe_read_query`, and execution handling to `execute_with_retry` and `execute_with_delta_repair`. Added 8 comprehensive unit tests in `tests/test_sql_generation.py`.
- **Checks a-g**: a-g all passed; pytest: 698 passed, 18 skipped, 8 deselected, 1 xfailed (+8 passed vs 690 baseline); ruff clean; compileall clean; import src.main ok; zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I); `src/stages/s12b_sql_retrieval.py` re-exporting soft-delete, intent, table_router, schema_retrieval, prompt_builder, and generation symbols (Phase I).
- **Issues Found**: None.
- **Next Step ID**: F7

---

## Step F7 Report: Thin SQLRetriever Pipeline and Stage 12b Shim
- **Step ID**: F7
- **Commits**: F7
- **Files Changed**: `src/sql/pipeline.py`, `src/stages/s12b_sql_retrieval.py`, `tests/test_sql_pipeline.py`.
- **What Changed**: Extracted `SQLRetriever`, `clear_knowledge_caches`, and result cache lifecycle into thin module `src/sql/pipeline.py` (270 lines, well below the 400-line limit). Re-exported `SQLRetriever`, `clear_knowledge_caches`, and all F1–F6 extracted symbols in `src/stages/s12b_sql_retrieval.py` which now serves as a clean 230-line backwards-compatibility shim. Added 6 comprehensive unit tests in `tests/test_sql_pipeline.py` covering object identity, retriever initialization, result & schema cache per-db_id isolation, access control, and feature flag mock patching.
- **Checks a-g**: a-g all passed; pytest: 704 passed, 18 skipped, 8 deselected, 1 xfailed (+6 passed vs 698 baseline); ruff clean; compileall clean; import src.main ok; zero drift; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); transition rule in `s12b`/`s11` for legacy schema chunks; `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim delegating to `src.sql.registry` (Phase I); `src/stages/s12b_sql_retrieval.py` re-exporting soft-delete, intent, table_router, schema_retrieval, prompt_builder, generation, and pipeline symbols (Phase I).
- **Issues Found**: None.
- **Next Step ID**: G1

---

## Step G1 Report: Leaf Module Moves to Subpackages
- **Step ID**: G1
- **Commits**: G1
- **Files Changed**: `src/sql/safety/join_graph.py`, `src/sql/learning/schema_monitor.py`, `src/sql/learning/ab_test_engine.py`, `src/pipeline/context_gatekeeper.py`, `src/sql/safety/drift_validator.py`, `src/sql/safety/result_validator.py`, `src/sql/safety/confidence_scorer.py`, `src/sql/learning/pattern_learner.py`, `src/sql/safety/sql_privacy.py`, `src/sql/safety/__init__.py`, `src/sql/learning/__init__.py`, 9 shims under `src/core/` and `src/utils/`, consumers (`src/sql/generation.py`, `src/sql/pipeline.py`, `src/stages/s12b_sql_retrieval.py`, `src/pipeline/query.py`), `tests/test_g1_modules.py`.
- **What Changed**: Moved 9 leaf modules (0-1 importers) to their target subpackages: `join_graph` -> `src/sql/safety/join_graph.py`, `schema_monitor` -> `src/sql/learning/schema_monitor.py`, `ab_test_engine` -> `src/sql/learning/ab_test_engine.py`, `context_gatekeeper` -> `src/pipeline/context_gatekeeper.py`, `sql_drift_validator` -> `src/sql/safety/drift_validator.py`, `result_validator` -> `src/sql/safety/result_validator.py`, `confidence_scorer` -> `src/sql/safety/confidence_scorer.py`, `pattern_learner` -> `src/sql/learning/pattern_learner.py`, `sql_privacy` -> `src/sql/safety/sql_privacy.py`. Created packages `src/sql/safety` and `src/sql/learning`. Left complete backward-compatibility shims at all 9 original locations preserving public and underscore names. Updated consumers to point to new canonical modules. Added 6 unit tests in `tests/test_g1_modules.py` covering object identity across all 9 shims and sanity checks for each module.
- **Checks a-g**: a-g all passed; pytest: 710 passed, 18 skipped, 8 deselected, 1 xfailed (+6 passed vs 704 baseline); ruff clean; compileall clean; import src.main ok; zero drift (both old and new validator paths pass); health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim (Phase I); `src/stages/s12b_sql_retrieval.py` re-export shim (Phase I); 9 G1 shims (`join_graph`, `schema_monitor`, `ab_test_engine`, `context_gatekeeper`, `sql_drift_validator`, `result_validator`, `confidence_scorer`, `pattern_learner`, `sql_privacy`) (Phase I).
- **Issues Found**: None.
- **Next Step ID**: G2

---

## Step G2 Report: Core, Utils, and Stages Moves to Subpackages
- **Step ID**: G2
- **Commits**: 01a7a7f
- **Files Changed**: `src/sql/learning/pipeline_metrics.py`, `src/sql/safety/column_registry.py`, `src/sql/fast_path.py`, `src/sql/semantic_cache.py`, `src/sql/safety/empty_result_classifier.py`, `src/sql/repair/sql_repair.py`, `src/sql/repair/delta_repair.py`, `src/sql/repair/__init__.py`, `src/sql/caches.py`, `src/sql/safety/sql_column_registry.py`, 7 shims under `src/core/`, `src/utils/`, `src/stages/`, and `src/prompts/`, consumers (`src/sql/generation.py`, `src/sql/pipeline.py`, `src/stages/s12_s13_s14_retrieval.py`, `src/stages/s12b_sql_retrieval.py`, `src/pipeline/query.py`, `src/api/ui.py`, `src/core/db_settings.py`, `src/utils/__init__.py`, `src/prompts/__init__.py`), `tests/test_g2_modules.py`.
- **What Changed**: Moved 7 modules (3-4 importers) to their target subpackages: `pipeline_metrics` -> `src/sql/learning/pipeline_metrics.py`, `sql_column_registry` -> `src/sql/safety/column_registry.py`, `fast_path` -> `src/sql/fast_path.py`, `semantic_cache` -> `src/sql/semantic_cache.py`, `empty_result_classifier` -> `src/sql/safety/empty_result_classifier.py`, `sql_repair` -> `src/sql/repair/sql_repair.py`, `delta_repair` -> `src/sql/repair/delta_repair.py`. Created package `src/sql/repair`. Left complete backward-compatibility shims at all 7 original locations preserving public and underscore names. Added monkeypatch compatibility helpers for `METRICS_FILE` and `DISABLED_TEMPLATES`. Updated consumers to point to new canonical modules. Added 4 unit tests in `tests/test_g2_modules.py` covering object identity across all 7 shims and smoke checks for moved modules.
- **Checks a-g**: a-g all passed; pytest: 714 passed, 18 skipped, 8 deselected, 1 xfailed (+4 passed vs 710 baseline); ruff clean; compileall clean; import src.main ok; zero drift (both old and new validator paths pass); offline eval 19/19 (100%) golden cases pass; health/overview 200 OK.
- **Shims Left**: `src/sql/knowledge/loaders.py` fallback to legacy locations for erp_main (Phase I); `src/core/sql_dialects.py` shim (Phase I); `src/core/db_config_file.py` shim (Phase I); `src/stages/s12b_sql_retrieval.py` re-export shim (Phase I); 9 G1 shims (`join_graph`, `schema_monitor`, `ab_test_engine`, `context_gatekeeper`, `sql_drift_validator`, `result_validator`, `confidence_scorer`, `pattern_learner`, `sql_privacy`) (Phase I); 7 G2 shims (`pipeline_metrics`, `sql_column_registry`, `fast_path`, `semantic_cache`, `empty_result_classifier`, `sql_repair`, `delta_repair`) (Phase I).
- **Issues Found**: None.
- **Next Step ID**: G3

