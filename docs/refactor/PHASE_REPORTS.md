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


