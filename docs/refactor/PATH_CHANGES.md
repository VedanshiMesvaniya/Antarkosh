# PATH_CHANGES.md — Restructure Branch

Tracks every file move and reference update made during the multi-database restructure.

Format: | # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |

---

## Phase A — Scaffolding & Tracking Files

| # | old path / module | new path / module | kind | references updated (file:line) | verified | commit |
|---|---|---|---|---|---|---|
| A-1 | (new) | docs/refactor/BASELINE.md | doc | — | verified | 64ff583 |
| A-2 | (new) | docs/refactor/PATH_CHANGES.md | doc | — | verified | 64ff583 |
| A-3 | (new) | docs/refactor/FOUND_ISSUES.md | doc | — | verified | 64ff583 |
| A-4 | (new) | docs/refactor/PHASE_REPORTS.md | doc | — | verified | 64ff583 |
| A-5 | (new) | docs/refactor/REFERENCE_INVENTORY.md | doc | — | verified | 64ff583 |

---

## Phase B — Centralize Path Constants

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| B-1 | (new) | scripts/_paths.py | code | helper module for REPO_ROOT discovery | a-g pass | cf71068 |
| B-2 | src/core/config.py | src/core/config.py | code | added DATABASES_DIR, SQLITE_DIR | a-g pass | cf71068 |
| B-3 | src/main.py | src/main.py | code | replaced __file__ math with PROJECT_ROOT | a-g pass | cf71068 |
| B-4 | src/core/db_config_file.py | src/core/db_config_file.py | code | dynamic root discovery without config loop | a-g pass | cf71068 |
| B-5 | src/core/sql_column_registry.py | src/core/sql_column_registry.py | code | REPO_ROOT -> PROJECT_ROOT, CONFIG_DIR | a-g pass | cf71068 |
| B-6 | src/core/sql_drift_validator.py | src/core/sql_drift_validator.py | code | REPO_ROOT -> PROJECT_ROOT, CONFIG_DIR | a-g pass | cf71068 |
| B-7 | src/pipeline/schema_ingestion.py | src/pipeline/schema_ingestion.py | code | schema_file/rel_path -> PROJECT_ROOT/CONFIG_DIR | a-g pass | cf71068 |
| B-8 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | code | rels/glossary/col_glossary/atlas -> CONFIG_DIR | a-g pass | cf71068 |
| B-9 | src/stages/s12_s13_s14_retrieval.py | src/stages/s12_s13_s14_retrieval.py | code | expansion_path -> CONFIG_DIR | a-g pass | cf71068 |
| B-10 | src/utils/failure_capture.py | src/utils/failure_capture.py | code | DATA_DIR / failed_queries.jsonl | a-g pass | cf71068 |
| B-11 | src/utils/feature_flags.py | src/utils/feature_flags.py | code | CONFIG_DIR / feature_flags.yaml | a-g pass | cf71068 |
| B-12 | src/utils/sql_safety.py | src/utils/sql_safety.py | code | REPO_ROOT/CONFIG_DIR | a-g pass | cf71068 |
| B-13 | src/utils/telemetry.py | src/utils/telemetry.py | code | DATA_DIR / telemetry_events.jsonl | a-g pass | cf71068 |
| B-14 | scripts/* | scripts/* | code | 10 script files wired to scripts._paths | a-g pass | cf71068 |
| B-15 | evals/* | evals/* | code | 2 eval scripts wired to dynamic REPO root | a-g pass | cf71068 |

---

## Phase C — Knowledge Packs and Fallback Loaders

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| C-1 | evals/Antarkosh/Antarkosh_schema.json | databases/erp_main/schema/schema.json | copy | byte-identical copy | a-g pass | 04a64d8 |
| C-2 | config/sql_relationships.json | databases/erp_main/schema/relationships.json | copy | byte-identical copy | a-g pass | 04a64d8 |
| C-3 | config/sql_glossary.json | databases/erp_main/semantics/glossary.json | copy | byte-identical copy | a-g pass | 04a64d8 |
| C-4 | config/sql_column_glossary.json | databases/erp_main/semantics/column_glossary.json | copy | byte-identical copy | a-g pass | 04a64d8 |
| C-5 | config/behavioral_schema_atlas.json | databases/erp_main/semantics/behavioral_atlas.json | copy | byte-identical copy | a-g pass | 04a64d8 |
| C-6 | (new) | databases/_template/ & databases/erp_main/ | scaffold | db.yaml, README.md, .gitkeep | a-g pass | 04a64d8 |
| C-7 | (new) | src/sql/knowledge/loaders.py | code | get_database_knowledge_path, get_knowledge_path | a-g pass | 04a64d8 |
| C-8 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | wire | switched 4 loaders to get_knowledge_path | a-g pass | 04a64d8 |
| C-9 | src/pipeline/schema_ingestion.py | src/pipeline/schema_ingestion.py | wire | switched schema and rels to loaders | a-g pass | 04a64d8 |
| C-10 | src/core/sql_drift_validator.py | src/core/sql_drift_validator.py | wire | switched SCHEMA_FILE and rels to loaders | a-g pass | 04a64d8 |
| C-11 | scripts/build_* | scripts/build_* | wire | 6 scripts updated with --db <id> and databases/<id> paths | a-g pass | 04a64d8 |
| C-12 | (new) | tests/test_database_knowledge_parity.py | test | byte-identity and fallback tests (9 passed) | a-g pass | 04a64d8 |
| C-13 | (new) | config/connections.example.json | config | template connection spec for multi-db | a-g pass | 04a64d8 |
| C-14 | .gitignore, .dockerignore, .github/workflows/ci.yml | .gitignore, .dockerignore, ci.yml | config | ignore rules & CI validation for databases/ | a-g pass | 04a64d8 |

---

## Step C2-1 — Docs, Inventory UTF-8, and Template Skeletons

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| C2-1-1 | docs/refactor/REFERENCE_INVENTORY.md | docs/refactor/REFERENCE_INVENTORY.md | doc | converted to clean UTF-8 text | a-g pass | 51b8037 |
| C2-1-2 | (new) | databases/erp_main/semantics/routing_hints.json | data | empty JSON object {} | a-g pass | 51b8037 |
| C2-1-3 | (new) | databases/_template/{schema,semantics}/* | data | 6 skeleton json files next to .gitkeep | a-g pass | 51b8037 |
| C2-1-4 | docs/refactor/FOUND_ISSUES.md | docs/refactor/FOUND_ISSUES.md | doc | path/dialect clarifications + freeze entry | a-g pass | 51b8037 |
| C2-1-5 | docs/refactor/PHASE_REPORTS.md | docs/refactor/PHASE_REPORTS.md | doc | corrected Phase C drift validator report | a-g pass | 51b8037 |

---

## Step C2-2 — Switch Remaining Readers in src/ to Loaders

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| C2-2-1 | src/core/sql_column_registry.py | src/core/sql_column_registry.py | wire | GLOSSARY_PATH, COLUMN_GLOSSARY_PATH -> get_knowledge_path | a-g pass | 305bad1 |
| C2-2-2 | src/utils/sql_safety.py | src/utils/sql_safety.py | wire | GLOSSARY_PATH, COLUMN_GLOSSARY_PATH -> get_knowledge_path | a-g pass | 305bad1 |
| C2-2-3 | src/core/sql_drift_validator.py | src/core/sql_drift_validator.py | wire | GLOSSARY_FILE, RELATIONSHIPS_FILE -> get_knowledge_path | a-g pass | 305bad1 |
| C2-2-4 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | doc | cleaned legacy filenames from docstrings (L292, L568) | a-g pass | 305bad1 |
| C2-2-5 | src/pipeline/schema_ingestion.py | src/pipeline/schema_ingestion.py | doc | cleaned legacy filenames from docstring & comment (L78, L170) | a-g pass | 305bad1 |
| C2-2-6 | src/core/join_graph.py | src/core/join_graph.py | doc | cleaned legacy filename from comment (L56) | a-g pass | 305bad1 |
| C2-2-7 | src/core/result_validator.py | src/core/result_validator.py | doc | cleaned legacy filename from comment (L86) | a-g pass | 305bad1 |

---

## Step C2-3 — Switch Non-src Readers to Loaders

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| C2-3-1 | scripts/verify_atlas.py | scripts/verify_atlas.py | wire | ATLAS_PATH, SCHEMA_PATH -> get_knowledge_path | a-g pass | 375a3df |
| C2-3-2 | scripts/build_sql_relationships.py | scripts/build_sql_relationships.py | wire | DEFAULT_OUT, docstrings, schema_path, out_path -> get_knowledge_path | a-g pass | 375a3df |
| C2-3-3 | scripts/auto_harvest_metadata.py | scripts/auto_harvest_metadata.py | wire | GLOSSARY_PATH, RELATIONSHIPS_PATH, main -> get_knowledge_path | a-g pass | 375a3df |
| C2-3-4 | scripts/build_behavioral_atlas.py | scripts/build_behavioral_atlas.py | wire | SCHEMA_PATH, GLOSSARY_PATH, RELS_PATH, OUTPUT_PATH, main -> get_knowledge_path | a-g pass | 375a3df |
| C2-3-5 | scripts/build_sql_glossary.py | scripts/build_sql_glossary.py | wire | SCHEMA_FILE, OLD_GLOSSARY_FILE, OUT_FILE, main -> get_knowledge_path | a-g pass | 375a3df |
| C2-3-6 | scripts/generate_behavioral_atlas.py | scripts/generate_behavioral_atlas.py | wire | INPUT_SCHEMA_PATH, INPUT_GLOSSARY_PATH, OUTPUT_ATLAS_PATH, main -> get_knowledge_path | a-g pass | 375a3df |
| C2-3-7 | evals/Antarkosh/run_eval.py | evals/Antarkosh/run_eval.py | wire | SCHEMA -> get_knowledge_path("schema") | a-g pass | 375a3df |
| C2-3-8 | evals/Antarkosh/baseline_v2/run_full_eval.py | evals/Antarkosh/baseline_v2/run_full_eval.py | wire | _load_schema_context -> get_knowledge_path("schema") | a-g pass | 375a3df |
| C2-3-9 | tests/test_sql_retrieval.py | tests/test_sql_retrieval.py | test | added test_relationships_knowledge_file_exists_and_loads | a-g pass | 375a3df |

---

## Step C2-4 — Guard Test Against Legacy Knowledge Paths in src/

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| C2-4-1 | (new) | tests/test_no_legacy_knowledge_paths.py | test | guard scanning all src/ .py files for forbidden legacy names | a-g pass | 3426347 |

---

## Step D1 — Loader Hardening and Database ID Validation

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| D1-1 | src/sql/knowledge/loaders.py | src/sql/knowledge/loaders.py | behavior | added validate_db_id, KnowledgeFileNotFound, traversal guards, erp_main-only fallback | a-g pass | e59165a |
| D1-2 | tests/test_database_knowledge_parity.py | tests/test_database_knowledge_parity.py | test | updated fallback test, added validation/traversal/not-found tests | a-g pass | e59165a |
| D1-3 | docs/refactor/FOUND_ISSUES.md | docs/refactor/FOUND_ISSUES.md | doc | recorded resolution of findings #9 and #10 | a-g pass | e59165a |

---

## Step D2 — Engine Enum Mapping

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| D2-1 | (new) | src/sql/engine.py | code | class Engine(str, Enum) with sqlite, mysql, postgresql, mssql, oracle, .key, .sqlglot, from_value() | a-g pass | 5aa0a06 |
| D2-2 | (new) | tests/test_sql_engine.py | test | unit tests for enum members, properties, aliases, case-insensitivity, and validation | a-g pass | 5aa0a06 |
| D2-3 | src/core/sql_column_registry.py | src/core/sql_column_registry.py | wire | replaced ad-hoc dialect strings in ColumnRegistry with Engine-derived checks | a-g pass | 5aa0a06 |
| D2-4 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | wire | replaced ad-hoc dialect strings in format_schema_rows and foreign key fetch with Engine | a-g pass | 5aa0a06 |
| D2-5 | src/pipeline/schema_ingestion.py | src/pipeline/schema_ingestion.py | wire | replaced ad-hoc dialect checks in _split_schema_by_table and sync_live_schema with Engine | a-g pass | 5aa0a06 |

---

## Step D3 — DatabaseContext and Context Cache

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| D3-1 | (new) | src/sql/context.py | code | DatabaseContext frozen dataclass, get_context, clear_context_cache, DEFAULT_DB_ID | a-g pass | a877abd |
| D3-2 | (new) | tests/test_sql_context.py | test | unit tests for erp_main loading, unknown db_id, context isolation, cache clearing, immutability | a-g pass | a877abd |

---

## Step D4 — Knowledge Caches Keyed by db_id

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| D4-1 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | behavior | keyed _get_raw_relationships, _load_relationships, _load_glossary, _get_raw_column_glossary, _get_raw_behavioral_atlas, _get_tables_with_soft_delete by db_id | a-g pass | 5954519 |
| D4-2 | (new) | tests/test_sql_knowledge_caches.py | test | golden snapshot parity, shared cache for default/erp_main, isolation between db folders, cache_clear | a-g pass | 5954519 |

---

## Step D5 — SQLRetriever and SemanticCache Keyed by db_id

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| D5-1 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | behavior | SQLRetriever._result_cache keyed on (db_id, query), _full_schema_cache and _column_registry keyed by db_id, db_id init arg defaulting to DEFAULT_DB_ID, selective/global cache clear | a-g pass | 69def1f |
| D5-2 | src/utils/semantic_cache.py | src/utils/semantic_cache.py | behavior | SemanticCache lookup & store scoped by db_id via build_scope_key | a-g pass | 69def1f |
| D5-3 | src/pipeline/query.py | src/pipeline/query.py | wire | scope_key prefixed with target_db_id in query pipeline | a-g pass | 69def1f |
| D5-4 | tests/test_sql_retrieval.py | tests/test_sql_retrieval.py | test | updated test_empty_schema_not_cached_permanently to check keyed _full_schema_cache | a-g pass | 69def1f |
| D5-5 | (new) | tests/test_sql_retriever_multi_db_cache.py | test | unit tests for SQLRetriever multi-db result & schema isolation, wipe all vs selective, SemanticCache db_id isolation | a-g pass | 69def1f |

---

## Step D6 — Qdrant Schema Chunks Tagged and Filtered by db_id

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| D6-1 | src/models/schemas.py | src/models/schemas.py | model | added optional db_id field to Chunk schema | a-g pass | 5b58d8a |
| D6-2 | src/pipeline/schema_ingestion.py | src/pipeline/schema_ingestion.py | behavior | sync_live_schema accepts db_id (default erp_main), tags chunks with document_id "schema:<db_id>" and payload db_id, clears schema cache per db_id | a-g pass | 5b58d8a |
| D6-3 | src/stages/s11_vector_store.py | src/stages/s11_vector_store.py | behavior | upsert persists db_id payload; _point_to_retrieved_chunk maps db_id; _build_filter supports db_id with erp_main legacy fallback | a-g pass | 5b58d8a |
| D6-4 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | behavior | _get_schema filters hybrid search on db_id with transition rule; allows candidate_list vector chunks when full_ddls empty | a-g pass | 5b58d8a |
| D6-5 | src/api/ui.py | src/api/ui.py | wire | /settings/sync-schema accepts optional validated db_id param | a-g pass | 5b58d8a |
| D6-6 | (new) | tests/test_schema_retrieval_multi_db.py | test | integration tests for two dbs same table name isolation, legacy transition rule, and sync_live_schema db_id tagging | a-g pass | 5b58d8a |

---

## Step D7 — db_id Through API and Pipeline, Metrics Field

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| D7-1 | src/core/pipeline_metrics.py | src/core/pipeline_metrics.py | model/log | added db_id to PipelineEvent, CURRENT_DB_ID ContextVar, log_event records db_id | a-g pass | d4046a4 |
| D7-2 | src/api/query.py | src/api/query.py | api | added optional db_id to QueryRequest, validate_db_id validation (HTTP 400), passed to QueryPipeline | a-g pass | d4046a4 |
| D7-3 | src/api/ui.py | src/api/ui.py | api | added optional db_id to SendMessage, validated in send_message and send_message_stream, passed to QueryPipeline | a-g pass | d4046a4 |
| D7-4 | src/pipeline/query.py | src/pipeline/query.py | pipeline | QueryPipeline accepts db_id, routes queries with db_id, _get_sql_retriever returns scoped retriever, logs routing with db_id | a-g pass | d4046a4 |
| D7-5 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | behavior | SQLRetriever.retrieve sets CURRENT_DB_ID context var | a-g pass | d4046a4 |
| D7-6 | (new) | tests/test_api_query_db_id.py | test | tests verifying optional db_id defaults to erp_main, rejects invalid db_id with 400, UI routes validate db_id, and metrics records contain db_id | a-g pass | d4046a4 |

---

## Step E1 — Database Connectors (Protocol & per-engine extraction)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| E1-1 | (new) | src/sql/connectors/base.py | code | Connector Protocol with test(), run_readonly() | a-g pass | 00e709f |
| E1-2 | src/core/db_client.py (sqlite) | src/sql/connectors/sqlite.py | code | SQLiteConnector with URI ro mode, test(), run_readonly() | a-g pass | 00e709f |
| E1-3 | src/core/db_client.py (mysql) | src/sql/connectors/mysql.py | code | MySQLConnector with dedicated readonly connection, test(), run_readonly() | a-g pass | d4f3b35 |
| E1-4 | src/core/db_client.py (postgresql) | src/sql/connectors/postgresql.py | code | PostgreSQLConnector with readonly transaction, timeout, test(), run_readonly() | a-g pass | 5aee3dc |
| E1-5 | src/core/db_client.py (mssql) | src/sql/connectors/mssql.py | code | MSSQLConnector with odbc_driver handling, test(), run_readonly() | a-g pass | 23f6af7 |
| E1-6 | src/core/db_client.py (oracle) | src/sql/connectors/oracle.py | code | OracleConnector with lower-cased columns, test(), run_readonly() | a-g pass | 481aac9 |
| E1-7 | src/core/db_client.py | src/core/db_client.py | wire | run_readonly_query preserves signature and limit logic; _execute delegates to get_connector | a-g pass | 481aac9 |
| E1-8 | (new) | src/sql/connectors/__init__.py | wire | package re-exports Connector, all 5 engine connectors, and get_connector dispatcher | a-g pass | 481aac9 |
| E1-9 | (new) | tests/test_sql_connectors.py | test | protocol conformance, SQLite file execution, get_connector factory for all 5 engines | a-g pass | 481aac9 |

---

## Step E2 — SQL Dialects (Base profile, per-engine modules, stubs, and shim)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| E2-1 | src/core/sql_dialects.py (SQLDialectProfile) | src/sql/dialects/base.py | code | SQLDialectProfile dataclass | a-g pass | 5b9ba01 |
| E2-2 | src/core/sql_dialects.py (sqlite) | src/sql/dialects/sqlite.py | code | SQLITE_PROFILE and get_profile() | a-g pass | 5b9ba01 |
| E2-3 | src/core/sql_dialects.py (mysql) | src/sql/dialects/mysql.py | code | MYSQL_PROFILE and get_profile() | a-g pass | 5b9ba01 |
| E2-4 | src/core/sql_dialects.py (postgresql) | src/sql/dialects/postgresql.py | code | POSTGRESQL_PROFILE and get_profile() | a-g pass | 5b9ba01 |
| E2-5 | (new) | src/sql/dialects/mssql.py | stub | stub get_profile() raising NotImplementedError | a-g pass | 5b9ba01 |
| E2-6 | (new) | src/sql/dialects/oracle.py | stub | stub get_profile() raising NotImplementedError | a-g pass | 5b9ba01 |
| E2-7 | src/core/sql_dialects.py (registry) | src/sql/dialects/__init__.py | code | DIALECTS registry (sqlite, mysql, postgresql) & get_dialect_profile() | a-g pass | 5b9ba01 |
| E2-8 | src/core/sql_dialects.py | src/core/sql_dialects.py | shim | re-exports DIALECTS, SQLDialectProfile, get_dialect_profile for 5 importers | a-g pass | 5b9ba01 |
| E2-9 | tests/test_sql_dialects.py | tests/test_sql_dialects.py | test | tests pinning mssql/oracle ValueError on get_dialect_profile and NotImplementedError on stubs | a-g pass | 5b9ba01 |
| E3-1 | (new) | src/sql/registry.py | code | list_databases, get_database, save_connection, get_connection, connections.json atomic write and first-use bootstrap | a-g pass | 25a13bf |
| E3-2 | src/core/db_config_file.py | src/core/db_config_file.py | shim | backward-compatible shim delegating read, write, apply_to to src.sql.registry | a-g pass | 25a13bf |
| E3-3 | src/sql/context.py | src/sql/context.py | wire | get_context delegates db.yaml loading and validation to get_database | a-g pass | 25a13bf |
| E3-4 | (new) | tests/test_sql_registry.py | test | tests for database discovery, engine validation, connections atomic persistence, legacy bootstrap, password safety | a-g pass | 25a13bf |
| E4-1 | src/core/db_settings.py (ENGINES) | src/sql/engine.py | code | ENGINES form spec and REQUIRED_FIELDS moved to engine.py | a-g pass | cba67de |
| E4-2 | src/core/db_settings.py (_test_*) | src/sql/connectors/*.py | code | _test_* functions delegate to connector.test() | a-g pass | cba67de |
| E4-3 | src/core/db_settings.py | src/core/db_settings.py | wire | db_settings delegates ENGINES, connector tests, and mirrors erp_main via registry.save_connection | a-g pass | cba67de |
| E4-4 | tests/test_db_settings.py | tests/test_db_settings.py | test | existing test suite passes unmodified | a-g pass | cba67de |

---

## Step E5 — Dynamic Connection Resolution and Per-Database Cache Invalidation

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| E5-1 | src/core/db_client.py | src/core/db_client.py | behavior | run_readonly_query accepts db_id (default erp_main), resolves connection config via registry, passes to connectors | a-g pass | 622dd28 |
| E5-2 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | behavior | SQLRetriever resolves engine/dialect by db_id, passes db_id to run_readonly_query, per-db knowledge caching & clear | a-g pass | 622dd28 |
| E5-3 | src/core/db_settings.py | src/core/db_settings.py | behavior | _apply_runtime performs per-db_id cache invalidation instead of global wipe; save accepts db_id | a-g pass | 622dd28 |
| E5-4 | src/utils/semantic_cache.py | src/utils/semantic_cache.py | behavior | clear and clear_cache accept db_id to selectively purge scoped semantic caches | a-g pass | 622dd28 |
| E5-5 | src/sql/connectors/ | src/sql/connectors/ | behavior | connectors accept connection config dictionary override in get_connector / run_readonly | a-g pass | 622dd28 |
| E5-6 | tests/test_sql_retriever_multi_db_cache.py | tests/test_sql_retriever_multi_db_cache.py | test | updated test_selective_and_global_cache_clearing for per-db_id invalidation | a-g pass | 622dd28 |
| E5-7 | (new) | tests/test_sql_db_connection_invalidation.py | test | tests verifying connection resolution by db_id, db A caches cleared and db B caches survive on connection change | a-g pass | 622dd28 |

---

## Step E6 — Database Access Control and Admin Endpoints

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| E6-1 | src/sql/registry.py | src/sql/registry.py | behavior | added can_access_database, get_database_access, update_database_access, atomic_write_yaml, and user filtering in list_databases | a-g pass | d9f6606 |
| E6-2 | src/sql/context.py | src/sql/context.py | behavior | added allowed_users and can_access() to DatabaseContext; parsed in get_context | a-g pass | d9f6606 |
| E6-3 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | behavior | SQLRetriever.retrieve and fetch_sqlite_foreign_keys enforce access control and hide unauthorized query results | a-g pass | d9f6606 |
| E6-4 | src/pipeline/query.py | src/pipeline/query.py | wire | QueryPipeline threads user_id from filters to SQLRetriever.retrieve | a-g pass | d9f6606 |
| E6-5 | src/api/query.py | src/api/query.py | api | query_documents enforces database access control when explicit db_id is provided (403 Forbidden) | a-g pass | d9f6606 |
| E6-6 | src/api/ui.py | src/api/ui.py | api | added GET /databases, GET /databases/{id}, admin-only GET/POST access, test and save connection endpoints | a-g pass | d9f6606 |
| E6-7 | (new) | tests/test_sql_database_access_control.py | test | tests mirroring test_alpha_auth_and_isolation.py for database resources, list filtering, query enforcement, admin connection test/save | a-g pass | d9f6606 |

---

## Step F1 — Soft-Delete Detection and Filter Extraction

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| F1-1 | src/stages/s12b_sql_retrieval.py | src/sql/soft_delete.py | code | Extracted detect_soft_delete_intent, _get_tables_with_soft_delete, _clear_soft_delete_tables_cache, enforce_soft_delete_filter, FALLBACK_SOFT_DELETE_TABLES | a-g pass | 816be00 |
| F1-2 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | shim | Re-exports all soft-delete functions and caches preserving exact signatures and attributes | a-g pass | 816be00 |
| F1-3 | (new) | tests/test_sql_soft_delete.py | test | Golden snapshot parity tests across direct and re-exported APIs for 10 representative intent queries and 6 SQL transformations | a-g pass | 816be00 |

---

## Step F2 — Analytical Intent Extraction

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| F2-1 | src/stages/s12b_sql_retrieval.py | src/sql/intent.py | code | Extracted extract_analytical_intent parsing metrics, dimensions, filters, time periods, temporal scope, limits, and soft-delete intent | a-g pass | 4837b9b |
| F2-2 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | shim | Re-exports extract_analytical_intent preserving signature and behavior for internal methods and external scripts | a-g pass | 4837b9b |
| F2-3 | (new) | tests/test_sql_intent.py | test | Tests verifying object identity and golden snapshot parity across 8 representative user queries | a-g pass | 4837b9b |

---

## Step F3 — Table Routing and Keyword Maps

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| F3-1 | src/stages/s12b_sql_retrieval.py (hardcoded lists) | databases/erp_main/semantics/routing_hints.json | data | Moved hardcoded fallback domain rules and anchor seed rules into routing_hints.json | a-g pass | ac1fa08 |
| F3-2 | src/stages/s12b_sql_retrieval.py | src/sql/table_router.py | code | Extracted database-agnostic table routing (load_routing_hints, route_tables_for_query, route_anchor_tables) with zero table names in code | a-g pass | ac1fa08 |
| F3-3 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | shim | Re-exports load_routing_hints, route_tables_for_query, route_anchor_tables, and cache functions; clear_knowledge_caches invalidates routing hints | a-g pass | ac1fa08 |
| F3-4 | (new) | tests/test_sql_table_router.py | test | Tests verifying object identity, zero hardcoded table names in router code, and golden routing snapshot parity across 10 queries | a-g pass | ac1fa08 |

---

## Step F4 — Schema Retrieval from Qdrant

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| F4-1 | src/stages/s12b_sql_retrieval.py | src/sql/schema_retrieval.py | code | Extracted extract_schema_table_names, extract_table_ddl_map, get_1hop_neighbors, format_scoped_relationships, build_scoped_schema_fallback, retrieve_schema_from_qdrant | a-g pass | F4 |
| F4-2 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | shim | Re-exports all schema helpers, fallback generator, and Qdrant retrieval; delegates SQLRetriever._get_schema to retrieve_schema_from_qdrant | a-g pass | F4 |
| F4-3 | tests/test_schema_budget.py, tests/test_schema_compactor.py | tests/test_schema_budget.py, tests/test_schema_compactor.py | wire | Updated patch targets for is_feature_enabled and log_telemetry to src.sql.schema_retrieval where names are looked up | a-g pass | F4 |
| F4-4 | (new) | tests/test_sql_schema_retrieval.py | test | Tests verifying object identity, DDL extraction, 1-hop graph expansion, scoped relationships, Qdrant fallback & db_id isolation, and golden fallback schema parity across 10 queries | a-g pass | F4 |

---

## Step F5 — Prompt Building and Knowledge Cache Loaders

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| F5-1 | src/stages/s12b_sql_retrieval.py | src/sql/prompt_builder.py | code | Extracted knowledge loaders (relationships, glossary, column glossary, behavioral atlas), glossary matching, scoped readability rules, and build_sql_prompt | a-g pass | F5 |
| F5-2 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | shim | Re-exports all prompt builder functions, caches, and readability rules; delegates SQLRetriever._generate_sql to build_sql_prompt; clear_knowledge_caches invalidates prompt caches | a-g pass | F5 |
| F5-3 | src/sql/schema_retrieval.py, src/sql/soft_delete.py | src/sql/schema_retrieval.py, src/sql/soft_delete.py | wire | Switched lazy imports of knowledge loaders and glossary builder to src.sql.prompt_builder | a-g pass | F5 |
| F5-4 | (new) | tests/test_sql_prompt_builder.py | test | Tests verifying object identity for all public & underscore aliases, stem rules, glossary matching, scoped readability, prompt assembly, and cache isolation across databases | a-g pass | F5 |

---

## Step F6 — SQL Generation, Execution Glue, and Repair Calls

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| F6-1 | src/stages/s12b_sql_retrieval.py | src/sql/generation.py | code | Extracted SQL generation (generate_sql), execution glue (execute_with_retry, execute_with_delta_repair), safety validation (is_safe_read_query, UnsafeQueryError, DANGEROUS_FUNCTIONS), formatting & unwrapping helpers (unwrap_sql, extract_cot_and_sql, format_schema_rows, format_fk_rows, format_rows_as_markdown, sanitize_rows, filter_display_rows, is_all_null, is_aggregate_over_zero_rows, extract_table_names), and fetch_sqlite_foreign_keys | a-g pass | F6 |
| F6-2 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | shim | Re-exports all generation functions, execution helpers, safety checks, and formatting utilities; delegates SQLRetriever._generate_sql to generate_sql, _is_safe_read_query to is_safe_read_query, and execution loops to execute_with_retry / execute_with_delta_repair | a-g pass | F6 |
| F6-3 | src/sql/prompt_builder.py | src/sql/prompt_builder.py | wire | Added public cache dict aliases without leading underscore (RAW_RELATIONSHIPS_CACHE etc.) | a-g pass | F6 |
| F6-4 | (new) | tests/test_sql_generation.py | test | Tests verifying object identity for all public & underscore aliases, unwrapping & CoT parsing, schema/FK row formatting, display filtering & sanitation, AST safety checks, 0-row aggregate detection, generate_sql soft-delete enforcement, and abstention handling | a-g pass | F6 |

---

## Step F7 — Thin SQLRetriever Pipeline and Stage 12b Shim

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| F7-1 | src/stages/s12b_sql_retrieval.py | src/sql/pipeline.py | code | Extracted SQLRetriever class, clear_knowledge_caches, and query result cache lifecycle into thin src/sql/pipeline.py (< 300 lines) | a-g pass | F7 |
| F7-2 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | shim | Converted s12b_sql_retrieval.py into a thin re-export compatibility shim (< 250 lines) re-exporting SQLRetriever and all F1–F6 extracted symbols | a-g pass | F7 |
| F7-3 | (new) | tests/test_sql_pipeline.py | test | Tests verifying SQLRetriever object identity, initialization, result & schema cache per-db_id isolation, access control enforcement, and feature flag patching | a-g pass | F7 |

---

## Step G1 — Leaf Module Moves (join_graph, schema_monitor, ab_test_engine, context_gatekeeper, drift_validator, result_validator, confidence_scorer, pattern_learner, sql_privacy)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| G1-1 | src/core/join_graph.py | src/sql/safety/join_graph.py | move | Moved join path validator; internal relationships import re-pointed to src.sql.prompt_builder | a-g pass | G1 |
| G1-2 | src/core/join_graph.py | src/core/join_graph.py | shim | Re-exports ForeignKey, JoinPath, JoinGraphBuilder | a-g pass | G1 |
| G1-3 | src/core/schema_monitor.py | src/sql/learning/schema_monitor.py | move | Moved schema drift detector and auto-healer | a-g pass | G1 |
| G1-4 | src/core/schema_monitor.py | src/core/schema_monitor.py | shim | Re-exports SchemaDrift, SchemaMonitor, path constants | a-g pass | G1 |
| G1-5 | src/core/ab_test_engine.py | src/sql/learning/ab_test_engine.py | move | Moved prompt A/B testing engine and statistical analyzer | a-g pass | G1 |
| G1-6 | src/core/ab_test_engine.py | src/core/ab_test_engine.py | shim | Re-exports ExperimentEvent, ABTestEngine, path constants | a-g pass | G1 |
| G1-7 | src/core/context_gatekeeper.py | src/pipeline/context_gatekeeper.py | move | Moved multi-turn follow-up and reset gatekeeper | a-g pass | G1 |
| G1-8 | src/core/context_gatekeeper.py | src/core/context_gatekeeper.py | shim | Re-exports ContextAction, ConversationState, ContextGatekeeper | a-g pass | G1 |
| G1-9 | src/core/sql_drift_validator.py | src/sql/safety/drift_validator.py | move | Moved schema glossary and relationships drift validator; supports direct CLI invocation | a-g pass | G1 |
| G1-10 | src/core/sql_drift_validator.py | src/core/sql_drift_validator.py | shim | Re-exports all validator functions and path constants; maintains CLI entrypoint | a-g pass | G1 |
| G1-11 | src/core/result_validator.py | src/sql/safety/result_validator.py | move | Moved semantic result correctness validators; internal relationships import re-pointed to src.sql.prompt_builder | a-g pass | G1 |
| G1-12 | src/core/result_validator.py | src/core/result_validator.py | shim | Re-exports all validator classes and enums | a-g pass | G1 |
| G1-13 | src/core/confidence_scorer.py | src/sql/safety/confidence_scorer.py | move | Moved explainable confidence scoring engine | a-g pass | G1 |
| G1-14 | src/core/confidence_scorer.py | src/core/confidence_scorer.py | shim | Re-exports ConfidenceBreakdown, ConfidenceScorer | a-g pass | G1 |
| G1-15 | src/core/pattern_learner.py | src/sql/learning/pattern_learner.py | move | Moved dynamic reflexion pattern learning engine | a-g pass | G1 |
| G1-16 | src/core/pattern_learner.py | src/core/pattern_learner.py | shim | Re-exports LearnedPattern, PatternLearner, path constants | a-g pass | G1 |
| G1-17 | src/utils/sql_privacy.py | src/sql/safety/sql_privacy.py | move | Moved SQL privacy and result row sanitization engine | a-g pass | G1 |
| G1-18 | src/utils/sql_privacy.py | src/utils/sql_privacy.py | shim | Re-exports sanitize_assistant_turn and privacy regexes | a-g pass | G1 |
| G1-19 | (new) | tests/test_g1_modules.py | test | Tests verifying object identity for all 9 shims and functional sanity of moved modules | a-g pass | G1 |

---

## Step G2 — Core, Utils, and Stages Moves (pipeline_metrics, column_registry, fast_path, semantic_cache, empty_result_classifier, sql_repair, delta_repair)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| G2-1 | src/core/pipeline_metrics.py | src/sql/learning/pipeline_metrics.py | move | Moved pipeline event metrics logger and scorer; added _get_metrics_file() monkeypatch resolver | a-g pass | 01a7a7f |
| G2-2 | src/core/pipeline_metrics.py | src/core/pipeline_metrics.py | shim | Re-exports CURRENT_DB_ID, METRICS_FILE, PipelineEvent, log_event, get_score_summary, get_recent_events | a-g pass | 01a7a7f |
| G2-3 | src/core/sql_column_registry.py | src/sql/safety/column_registry.py | move | Moved column schema registry and validation; aliased at src/sql/safety/sql_column_registry.py | a-g pass | 01a7a7f |
| G2-4 | src/core/sql_column_registry.py | src/core/sql_column_registry.py | shim | Re-exports ColumnRegistry, COLUMN_GLOSSARY_PATH, GLOSSARY_PATH | a-g pass | 01a7a7f |
| G2-5 | src/utils/fast_path.py | src/sql/fast_path.py | move | Moved deterministic template fast path and synthesis bypass | a-g pass | 01a7a7f |
| G2-6 | src/utils/fast_path.py | src/utils/fast_path.py | shim | Re-exports DISABLED_TEMPLATES, fast_path_format, format_aggregate_fast_path, format_list_fast_path | a-g pass | 01a7a7f |
| G2-7 | src/utils/semantic_cache.py | src/sql/semantic_cache.py | move | Moved semantic vector cosine cache with TTL and LRU eviction | a-g pass | 01a7a7f |
| G2-8 | src/utils/semantic_cache.py | src/utils/semantic_cache.py | shim | Re-exports SemanticCache, get_semantic_cache, CachedEntry | a-g pass | 01a7a7f |
| G2-9 | src/utils/empty_result_classifier.py | src/sql/safety/empty_result_classifier.py | move | Moved 0-row classification engine (valid_empty vs suspicious_empty) | a-g pass | 01a7a7f |
| G2-10 | src/utils/empty_result_classifier.py | src/utils/empty_result_classifier.py | shim | Re-exports classify_empty_result, VALID_EMPTY, SUSPICIOUS_EMPTY | a-g pass | 01a7a7f |
| G2-11 | src/stages/sql_repair.py | src/sql/repair/sql_repair.py | move | Moved Delta Repair executor and DDL schema context extractor | a-g pass | 01a7a7f |
| G2-12 | src/stages/sql_repair.py | src/stages/sql_repair.py | shim | Re-exports MAX_DELTA_REPAIR_ATTEMPTS, attempt_delta_repair, extract_schema_context_from_ddl, extract_sql_from_response | a-g pass | 01a7a7f |
| G2-13 | src/prompts/delta_repair.py | src/sql/repair/delta_repair.py | move | Moved delta repair prompt builder and token estimator | a-g pass | 01a7a7f |
| G2-14 | src/prompts/delta_repair.py | src/prompts/delta_repair.py | shim | Re-exports build_delta_repair_payload, count_tokens, format_compact_schema, DELTA_REPAIR_SYSTEM_PROMPT | a-g pass | 01a7a7f |
| G2-15 | (new) | tests/test_g2_modules.py | test | Tests verifying object identity for all 7 shims and smoke testing moved modules | a-g pass | 01a7a7f |

---

## Step G3 — Schema Utilities, Failure Capture, and Metadata Store Moves (schema_compactor, schema_budget, schema_token_estimator, failure_capture, metadata_store)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| G3-1 | src/utils/schema_compactor.py | src/sql/schema_compactor.py | move | Moved schema DDL compactor and join hint extractor | a-g pass | 05e1792 |
| G3-2 | src/utils/schema_compactor.py | src/utils/schema_compactor.py | shim | Re-exports compact_ddl, extract_join_hints, AUDIT_COLUMNS | a-g pass | 05e1792 |
| G3-3 | src/utils/schema_budget.py | src/sql/schema_budget.py | move | Moved dynamic schema token budget allocator and table pruner | a-g pass | 05e1792 |
| G3-4 | src/utils/schema_budget.py | src/utils/schema_budget.py | shim | Re-exports DEFAULT_SCHEMA_TOKEN_BUDGET, select_schema_within_budget | a-g pass | 05e1792 |
| G3-5 | src/utils/schema_token_estimator.py | src/sql/schema_token_estimator.py | move | Moved schema token estimator | a-g pass | 05e1792 |
| G3-6 | src/utils/schema_token_estimator.py | src/utils/schema_token_estimator.py | shim | Re-exports estimate_schema_tokens | a-g pass | 05e1792 |
| G3-7 | src/utils/failure_capture.py | src/sql/learning/failure_capture.py | move | Moved SQL generation & execution failure logger; added _get_failure_log_file() resolver | a-g pass | 05e1792 |
| G3-8 | src/utils/failure_capture.py | src/utils/failure_capture.py | shim | Re-exports DEFAULT_FAILURE_LOG_FILE, capture_sql_failure | a-g pass | 05e1792 |
| G3-9 | src/core/metadata_store.py | src/rag/metadata_store.py | move | Moved document metadata backend (JSON and Qdrant backend implementations) | a-g pass | 05e1792 |
| G3-10 | src/core/metadata_store.py | src/core/metadata_store.py | shim | Re-exports MetadataBackend, JsonMetadataBackend, QdrantMetadataBackend, create_metadata_backend, migrate_registry | a-g pass | 05e1792 |
| G3-11 | (new) | tests/test_g3_modules.py | test | Tests verifying object identity for all 5 shims and smoke testing moved modules | a-g pass | 05e1792 |

---

## Step G4 — Core Safety, Query Classifier, and Ingestion Registry Moves (sql_safety, query_classifier, ingestion_registry)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| G4-1 | src/utils/sql_safety.py | src/sql/safety/sql_safety.py | move | Moved SQL safety layer and AST validation engine; organized per-engine dangerous functions | a-g pass | 82cc769 |
| G4-2 | src/utils/sql_safety.py | src/utils/sql_safety.py | shim | Re-exports validate_sql_safety, is_destructive_sql, DANGEROUS_FUNCTIONS, etc. | a-g pass | 82cc769 |
| G4-3 | src/utils/query_classifier.py | src/sql/query_classifier.py | move | Moved deterministic query classifier, QueryType enum, and legacy intent classifier | a-g pass | 82cc769 |
| G4-4 | src/utils/query_classifier.py | src/utils/query_classifier.py | shim | Re-exports QueryType, TTL_BY_QUERY_TYPE, classify_query, classify_query_intent | a-g pass | 82cc769 |
| G4-5 | src/core/ingestion_registry.py | src/rag/ingestion_registry.py | move | Moved document ingestion registry and content-addressed lineage tracker | a-g pass | 82cc769 |
| G4-6 | src/core/ingestion_registry.py | src/core/ingestion_registry.py | shim | Re-exports IngestionRegistry, RegistryStatus, RegistryCheckResult | a-g pass | 82cc769 |
| G4-7 | (new) | tests/test_g4_modules.py | test | Tests verifying object identity for all 3 shims and smoke testing moved modules | a-g pass | 82cc769 |

---

## Step G5 — RAG Stages and Ingestion Moves, Schema Ingestion Merge, and Query Pipeline Removal (s01-s11, s12_s13_s14_retrieval, ingestion, folder_ingestion, schema_ingestion, query_pipeline)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| G5-1 | src/stages/s01_file_detection.py | src/rag/stages/s01_file_detection.py | move | Moved Stage 1 MIME detection and file category validation | a-g pass | 98966bc |
| G5-2 | src/stages/s01_file_detection.py | src/stages/s01_file_detection.py | shim | Re-exports detect_file, is_supported_file, FileCategory | a-g pass | 98966bc |
| G5-3 | src/stages/s02_classification.py | src/rag/stages/s02_classification.py | move | Moved Stage 2 document structure and semantic classification | a-g pass | 98966bc |
| G5-4 | src/stages/s02_classification.py | src/stages/s02_classification.py | shim | Re-exports classify_semantic, classify_structure | a-g pass | 98966bc |
| G5-5 | src/stages/s03_parsing.py | src/rag/stages/s03_parsing.py | move | Moved Stage 3 document parsers (PDF, DOCX, XLSX, TXT, CSV) | a-g pass | 98966bc |
| G5-6 | src/stages/s03_parsing.py | src/stages/s03_parsing.py | shim | Re-exports parse_document, extract_text | a-g pass | 98966bc |
| G5-7 | src/stages/s04_ocr.py | src/rag/stages/s04_ocr.py | move | Moved Stage 4 OCR engine and fallback parsers | a-g pass | 98966bc |
| G5-8 | src/stages/s04_ocr.py | src/stages/s04_ocr.py | shim | Re-exports run_ocr, is_scanned_page | a-g pass | 98966bc |
| G5-9 | src/stages/s05_layout.py | src/rag/stages/s05_layout.py | move | Moved Stage 5 layout analysis and reading order detector | a-g pass | 98966bc |
| G5-10 | src/stages/s05_layout.py | src/stages/s05_layout.py | shim | Re-exports analyze_layout, extract_reading_order | a-g pass | 98966bc |
| G5-11 | src/stages/s06_tables.py | src/rag/stages/s06_tables.py | move | Moved Stage 6 tabular extraction and markdown converter | a-g pass | 98966bc |
| G5-12 | src/stages/s06_tables.py | src/stages/s06_tables.py | shim | Re-exports extract_tables, format_markdown_table | a-g pass | 98966bc |
| G5-13 | src/stages/s07_s08_visuals.py | src/rag/stages/s07_s08_visuals.py | move | Moved Stages 7-8 visual element extractor and diagram parser | a-g pass | 98966bc |
| G5-14 | src/stages/s07_s08_visuals.py | src/stages/s07_s08_visuals.py | shim | Re-exports analyze_visuals, extract_images | a-g pass | 98966bc |
| G5-15 | src/stages/s09_chunking.py | src/rag/stages/s09_chunking.py | move | Moved Stage 9 semantic document chunking engine | a-g pass | 98966bc |
| G5-16 | src/stages/s09_chunking.py | src/stages/s09_chunking.py | shim | Re-exports chunk_document, estimate_chunk_tokens | a-g pass | 98966bc |
| G5-17 | src/stages/s10_embeddings.py | src/rag/stages/s10_embeddings.py | move | Moved Stage 10 BGE-M3 dense/sparse embedding generator | a-g pass | 98966bc |
| G5-18 | src/stages/s10_embeddings.py | src/stages/s10_embeddings.py | shim | Re-exports EmbeddingService, SparseVector | a-g pass | 98966bc |
| G5-19 | src/stages/s11_vector_store.py | src/rag/stages/s11_vector_store.py | move | Moved Stage 11 QdrantStore collection manager and upsert engine | a-g pass | 98966bc |
| G5-20 | src/stages/s11_vector_store.py | src/stages/s11_vector_store.py | shim | Re-exports QdrantStore, CollectionConfig | a-g pass | 98966bc |
| G5-21 | src/stages/s12_s13_s14_retrieval.py | src/rag/retrieval.py | move | Moved Stages 12-14 Retriever, Reranker, Generator pipeline | a-g pass | 98966bc |
| G5-22 | src/stages/s12_s13_s14_retrieval.py | src/stages/s12_s13_s14_retrieval.py | shim | Re-exports Retriever, Reranker, Generator with _ShimModule setattr mirror | a-g pass | 98966bc |
| G5-23 | src/pipeline/ingestion.py | src/rag/ingestion.py | move | Moved multi-stage IngestionPipeline with SHA-256 deduplication and locks | a-g pass | 98966bc |
| G5-24 | src/pipeline/ingestion.py | src/pipeline/ingestion.py | shim | Re-exports IngestionPipeline, IngestionResult, _INGEST_LOCKS with _ShimModule | a-g pass | 98966bc |
| G5-25 | src/pipeline/folder_ingestion.py | src/rag/folder_ingestion.py | move | Moved drop-folder auto-ingestion service and periodic scanner | a-g pass | 98966bc |
| G5-26 | src/pipeline/folder_ingestion.py | src/pipeline/folder_ingestion.py | shim | Re-exports scan_and_ingest, run_periodic_scan with _ShimModule | a-g pass | 98966bc |
| G5-27 | src/pipeline/schema_ingestion.py | src/sql/schema_retrieval.py | merge | Merged live DB schema introspection and Qdrant chunk upsert into schema_retrieval | a-g pass | 98966bc |
| G5-28 | src/pipeline/schema_ingestion.py | src/pipeline/schema_ingestion.py | shim | Re-exports sync_live_schema, SCHEMA_DOCUMENT_ID with _ShimModule | a-g pass | 98966bc |
| G5-29 | src/pipeline/query_pipeline.py | (deleted) | delete | Removed compatibility alias after updating sole importer in run_full_eval.py | a-g pass | 98966bc |
| G5-30 | (new) | tests/test_g5_modules.py | test | Tests verifying object identity for all 14 shims, helpers, and alias removal | a-g pass | 98966bc |

---

## Step G6 — Learned Data Per Database (pattern_learner, schema_monitor, failure_capture)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| G6-1 | src/sql/knowledge/loaders.py | src/sql/knowledge/loaders.py | code | Added get_learned_path and resolve_learned_read_path helpers with folder auto-creation on write | a-g pass | a129765 |
| G6-2 | src/sql/learning/pattern_learner.py | src/sql/learning/pattern_learner.py | code | Routed learned patterns and metrics writes to databases/<db_id>/learned/ with fallback to data/ | a-g pass | a129765 |
| G6-3 | src/sql/learning/schema_monitor.py | src/sql/learning/schema_monitor.py | code | Routed drift log writes to databases/<db_id>/learned/ with fallback to data/ | a-g pass | a129765 |
| G6-4 | src/sql/learning/failure_capture.py | src/sql/learning/failure_capture.py | code | Routed failure logs to databases/<db_id>/learned/ with fallback to data/ and folder auto-creation | a-g pass | a129765 |
| G6-5 | src/sql/pipeline.py | src/sql/pipeline.py | code | Passed self.db_id to PatternLearner in SQLRetriever | a-g pass | a129765 |
| G6-6 | (new) | tests/test_g6_modules.py | test | Tests verifying folder auto-creation on write, per-database routing, and fallback behavior | a-g pass | a129765 |

---

## Step H1 — Scripts Reorganization (scripts/db, scripts/eval, scripts/ops)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| H1-1 | scripts/build_sql_relationships.py | scripts/db/build_sql_relationships.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-2 | scripts/build_sql_glossary.py | scripts/db/build_sql_glossary.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-3 | scripts/build_behavioral_atlas.py | scripts/db/build_behavioral_atlas.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-4 | scripts/generate_behavioral_atlas.py | scripts/db/generate_behavioral_atlas.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-5 | scripts/auto_harvest_metadata.py | scripts/db/auto_harvest_metadata.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-6 | scripts/verify_atlas.py | scripts/db/verify_atlas.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-7 | scripts/load_mysql_dump.py | scripts/db/load_mysql_dump.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-8 | scripts/setup_db.py | scripts/db/setup_db.py | move | Moved to scripts/db/, uses scripts/_paths.py | a-g pass | 59ab567 |
| H1-9 | scripts/run_batch_eval.py | scripts/eval/run_batch_eval.py | move | Moved to scripts/eval/, uses scripts/_paths.py | a-g pass | 0f9f882 |
| H1-10 | scripts/test_question.py | scripts/eval/test_question.py | move | Moved to scripts/eval/, uses scripts/_paths.py | a-g pass | 0f9f882 |
| H1-11 | scripts/sql_smoke_test.py | scripts/eval/sql_smoke_test.py | move | Moved to scripts/eval/, uses scripts/_paths.py | a-g pass | 0f9f882 |
| H1-12 | scripts/adversarial_smoke_test.py | scripts/eval/adversarial_smoke_test.py | move | Moved to scripts/eval/, uses scripts/_paths.py | a-g pass | 0f9f882 |
| H1-13 | scripts/production_smoke_test.py | scripts/eval/production_smoke_test.py | move | Moved to scripts/eval/, uses scripts/_paths.py | a-g pass | 0f9f882 |
| H1-14 | scripts/run_shadow_audit.py | scripts/eval/run_shadow_audit.py | move | Moved to scripts/eval/, uses scripts/_paths.py | a-g pass | 0f9f882 |
| H1-15 | scripts/generate_benchmark_fixtures.py | scripts/eval/generate_benchmark_fixtures.py | move | Moved to scripts/eval/, uses scripts/_paths.py | a-g pass | 0f9f882 |
| H1-16 | scripts/check_providers.py | scripts/ops/check_providers.py | move | Moved to scripts/ops/, uses scripts/_paths.py | a-g pass | 4b7b818 |
| H1-17 | scripts/verify_telemetry.py | scripts/ops/verify_telemetry.py | move | Moved to scripts/ops/, uses scripts/_paths.py | a-g pass | 4b7b818 |
| H1-18 | scripts/migrate_existing_docs_to_system_user.py | scripts/ops/migrate_existing_docs_to_system_user.py | move | Moved to scripts/ops/, uses scripts/_paths.py | a-g pass | 4b7b818 |
| H1-19 | (docs & configs) | README.md, docs, configs | docs | Updated script invocation paths across documentation and configs | a-g pass | 9098c57 |

---

## Step H2 — Tests Mirror Src (tests/sql, tests/rag, tests/core, tests/api)

| # | old path / module | new path / module | kind | references updated (file:line) | verified (checks a-g) | commit |
|---|---|---|---|---|---|---|
| H2-1 | tests/test_sql_*.py (35 files) | tests/sql/ | move | 35 SQL tests moved to tests/sql/; updated relative paths in test_db_config_file, test_delta_repair_*, test_schema_budget, test_schema_compactor, test_sql_safety | a-g pass | feeba3a |
| H2-2 | tests/test_*.py (23 RAG files) | tests/rag/ | move | 23 RAG tests moved to tests/rag/ | a-g pass | 1338266 |
| H2-3 | tests/test_*.py (23 Core files) | tests/core/ | move | 23 Core tests moved to tests/core/ | a-g pass | 6daf4e8 |
| H2-4 | tests/test_*.py (5 API files) | tests/api/ | move | 5 API tests moved to tests/api/ | a-g pass | 00b4ddd |
| H2-5 | pyproject.toml, CI, docs | pyproject.toml, CI, docs | config | Updated testpaths, CI pytest lines (--ignore=tests/golden, --ignore=tests/rag/test_local_qdrant_integration.py), README.md, docs | a-g pass | c42c3e3 |




