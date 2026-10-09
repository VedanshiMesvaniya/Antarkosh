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



