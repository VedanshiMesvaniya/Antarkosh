# Antarkosh — Multi-Database Structure (v2)

Target structure for running several databases (SQLite, MySQL, PostgreSQL, SQL Server, Oracle) through the Text-to-SQL pipeline without cross-database confusion or hallucinated tables and columns.

Updated for branch `sql_update` at commit `1640431` ("granular document access control and admin restrictions"). The first version of this doc was written against `033ecfa`. Since then the repo gained SQL Server and Oracle connection support, config isolation (`config/ingestion.json`), per-document access control, and a dedicated `chat_title` provider route.

---

## 1. What changed since v1 and what it means

| Change in your repo | Effect on the structure |
|---|---|
| SQL Server (`pyodbc`) and Oracle (`oracledb`) added to `db_client.py`, `db_settings.py`, `.env.example`, the UI field spec, and `sql_safety.py` | Five engines now exist, so `db_client._execute` is a five-branch `if/elif` chain and `db_settings` has a `_test_*` function per engine. These become one **connector class per engine** |
| `config/db_connection.json` and `config/ingestion.json` (both gitignored, runtime-written) | Same idea, but per database: one `config/connections.json` holding every connection by `db_id`. `ingestion.json` stays global |
| `allowed_users` per document (`ingestion_registry`, `ui.py`, `s12_s13_s14_retrieval`, admin-only `/documents/{id}/access`) | Same pattern is reused for databases: `allowed_users` in each `db.yaml` plus an admin-only `/databases/{db_id}/access` endpoint |
| `chat_title` route in `providers.yaml` | Stays global. No structural change |
| `has_dangerous_qualified_call` in `sql_safety.py` (package-qualified calls such as Oracle `DBMS_*`/`UTL_*`) | Goes to `sql/safety/`. Dangerous-function lists should be per engine, not one shared set |

---

## 2. Why the current layout still breaks with more than one database

| # | Problem | Where |
|---|---------|-------|
| 1 | **One active database, switched by overwriting globals.** `db_settings._apply_runtime` writes `settings.db_*` and then clears every cache because "everything cached from the previous database is now wrong" | `core/db_settings.py`, `core/config.py` |
| 2 | **SQL Server and Oracle are only half wired.** The UI marks them `ready: True`, the connection test passes and `db_client` can run queries, but `sql_dialects.DIALECTS` still has only `sqlite`, `mysql` and `postgresql`; the file's own comment says "SQL Server, if/when needed: add an entry here". `get_dialect_profile("mssql")` raises `ValueError`, so schema sync and SQL generation should fail for both. Not run, based on reading the code | `core/sql_dialects.py`, `pipeline/schema_ingestion.py` |
| 3 | **Three different dialect vocabularies.** Engine keys (`postgresql`, `mssql`), sqlglot names (`postgres`, `tsql`) and ad-hoc tuples such as `("mysql","postgres","tsql","oracle")` in `sql_column_registry.py` and `s12b` | `sql_column_registry.py`, `s12b`, `schema_ingestion.py` |
| 4 | **Schema chunks are not tagged per database.** Everything is stored under `document_id="live_db_schema"` and retrieval filters only on `chunk_type=SQL_SCHEMA` | `schema_ingestion.py`, `s12b._get_schema` |
| 5 | **Caches ignore the database.** `SQLRetriever._result_cache` (key = question text), `_full_schema_cache`, `_column_registry`, `_load_glossary`, `_get_raw_relationships`, `_BEHAVIORAL_ATLAS_CACHE`, `_SOFT_DELETE_TABLES_CACHE` are process-wide | `s12b`, `semantic_cache` |
| 6 | **ERP table names are hardcoded in generic code** (`party`, `sales_order`, `financial_year`, `carton`, soft-delete fallback list, keyword maps) | `s12b` (~L585, ~L898–906, ~L1807–1834) |
| 7 | **Per-database knowledge lives in global or test folders.** Schema JSON is in `evals/Antarkosh/` but production reads it; glossary, relationships and atlas are global in `config/` | `config/`, `evals/` |
| 8 | **Learning files referenced but absent.** `sql_pattern_library.json`, `schema_atlas.json`, `experiments.yaml` do not exist; outputs go to global `data/*.jsonl` | `pattern_learner`, `schema_monitor`, `ab_test_engine` |
| 9 | **Unused modules (no importers found):** `ab_test_engine`, `context_gatekeeper`, `join_graph`, `schema_monitor`. Check for dynamic imports before removing | `core/` |
| 10 | **One giant file.** `s12b_sql_retrieval.py` is ~2,578 lines | `stages/` |
| 11 | **`api/ui.py` keeps growing** (chats, titles, documents, document access, users, DB settings, telemetry in one router) | `api/ui.py` |
| 12 | **Smaller:** `requirements.txt` next to `pyproject.toml` + `uv.lock`; `frontend/` is build output of `Antarkosh_UI/`; `.env.example` sets `DB_ENGINE` twice; root clutter (`pre_implementation_baseline.txt`, `final_run_sql_pipline_eval/`); the ERP database is named `Antarkosh`, same as the product | repo root |

---

## 3. Design rules

1. **One question → one `db_id` → one `DatabaseContext`.** Two schemas are never merged into one prompt.
2. **Every cache, Qdrant payload and learned file is keyed by `db_id`.** Switching databases no longer needs a global cache wipe.
3. **Generic code contains zero table or column names.** Domain knowledge lives only in the database's own folder.
4. **One engine vocabulary.** A single `Engine` enum (`sqlite`, `mysql`, `postgresql`, `mssql`, `oracle`) with `.sqlglot` as a property. Nothing else spells dialect names.
5. **Each engine is one connector plus one dialect file.** Adding a sixth engine touches nothing else.
6. **Curated vs learned are separate.** Curated files are human-reviewed and committed. Learned files are machine-written and gitignored. Reviewed learned entries are promoted into curated.
7. **Credentials never live in a database's knowledge folder.** They go in `config/connections.json` (gitignored).
8. **Access follows the document pattern.** Per-database `allowed_users`, enforced server-side, edited by admins only.

---

## 4. Target repository structure

```
Antarkosh/
├── databases/                          # one folder per DB (knowledge packs)
│   ├── README.md
│   ├── _template/                      # copy to onboard a new DB
│   ├── erp_main/                       # example id: today's ERP DB
│   │   ├── db.yaml                     # manifest (section 5)
│   │   ├── schema/
│   │   │   ├── schema.json             # <- evals/Antarkosh/Antarkosh_schema.json
│   │   │   └── relationships.json      # <- config/sql_relationships.json
│   │   ├── semantics/                  # CURATED, committed
│   │   │   ├── glossary.json           # <- config/sql_glossary.json
│   │   │   ├── column_glossary.json    # <- config/sql_column_glossary.json
│   │   │   ├── behavioral_atlas.json   # <- config/behavioral_schema_atlas.json
│   │   │   └── routing_hints.json      # NEW: keyword -> tables (replaces s12b hardcoding)
│   │   ├── learned/                    # MACHINE-WRITTEN, gitignored
│   │   │   ├── patterns.jsonl          # pattern_learner
│   │   │   ├── pattern_metrics.json
│   │   │   ├── schema_snapshot.json    # schema_monitor
│   │   │   ├── drift_log.jsonl
│   │   │   ├── enums_harvested.json    # auto_harvest_metadata
│   │   │   ├── failed_queries.jsonl    # failure_capture
│   │   │   └── pipeline_metrics.jsonl
│   │   └── evals/                      # questions.jsonl, golden cases, reports
│   └── <next_db_id>/                   # same layout (any engine)
│
├── config/                             # GLOBAL only
│   ├── providers.yaml                  # incl. chat_title route
│   ├── features.yaml  feature_flags.yaml  validation_thresholds.yaml
│   ├── acronym_expansions.json         # document RAG, not DB-specific
│   ├── alpha_users.py
│   ├── connections.example.json        # committed sample, no secrets
│   ├── connections.json                # gitignored: { db_id: connection fields + secrets }
│   │                                   # replaces config/db_connection.json
│   └── ingestion.json                  # gitignored runtime file (as today)
├── data/
│   ├── sqlite/<db_id>.db               # replaces the single data/live_data.db
│   ├── uploads/  processed/  inbox/  qdrant_local/
│   └── chats.json  messages.json  documents.json  settings.json
│
├── src/
│   ├── core/                           # shared infrastructure only
│   │   ├── config.py                   # DB_* env fields become bootstrap for the first connection
│   │   ├── paths.py  file_lock.py  state.py
│   │   ├── qdrant_service.py  embeddings.py  local_models.py
│   │   ├── provider_client.py  rate_limiter.py  circuit_breaker.py
│   │   └── telemetry.py  trace_context.py  trace_writer.py  feature_flags.py
│   │
│   ├── rag/                            # document pipeline (logic unchanged)
│   │   ├── stages/                     # s01 ... s11
│   │   ├── ingestion.py  folder_ingestion.py
│   │   ├── ingestion_registry.py       # keeps allowed_users
│   │   ├── metadata_store.py  confidence.py
│   │   └── retrieval.py                # <- s12_s13_s14_retrieval.py (allowed-document filter stays)
│   │
│   ├── sql/                            # NEW: everything Text-to-SQL
│   │   ├── engine.py                   # Engine enum: key, sqlglot name, default port, form fields
│   │   ├── registry.py                 # DatabaseRegistry: list/get/save/delete by db_id
│   │   ├── context.py                  # DatabaseContext (cached per db_id)
│   │   ├── router.py                   # NEW: which DB is this question about
│   │   ├── access.py                   # NEW: allowed_users check per db_id
│   │   ├── connectors/
│   │   │   ├── base.py                 # Protocol: test(), run_readonly(), introspect()
│   │   │   ├── sqlite.py  mysql.py  postgresql.py
│   │   │   ├── mssql.py                # pyodbc + odbc_driver (docs/databases/ODBC_DRIVER_SETUP.md)
│   │   │   └── oracle.py               # oracledb, service_name, lower-cased column names
│   │   ├── dialects/                   # <- core/sql_dialects.py split per engine
│   │   │   ├── base.py                 # SQLDialectProfile
│   │   │   ├── sqlite.py  mysql.py  postgresql.py
│   │   │   ├── mssql.py                # TO ADD: schema_query, fk_query, TOP / OFFSET FETCH rules
│   │   │   └── oracle.py               # TO ADD: schema_query, fk_query, FETCH FIRST rules, DUAL
│   │   ├── knowledge/
│   │   │   ├── loaders.py              # read a db folder into DatabaseContext
│   │   │   └── builders/               # relationships, glossary, atlas builders (from scripts/)
│   │   ├── onboarding/                 # test connection -> introspect -> schema.json -> relationships
│   │   │                               #   -> enums -> atlas draft -> human review -> enable
│   │   ├── schema_retrieval.py         # <- pipeline/schema_ingestion.py, Qdrant filter on db_id
│   │   ├── table_router.py             # generic; reads routing_hints.json
│   │   ├── prompt_builder.py  intent.py  soft_delete.py
│   │   ├── generation.py
│   │   ├── repair/                     # sql_repair + delta_repair prompt
│   │   ├── safety/                     # sql_safety (per-engine dangerous lists + qualified calls),
│   │   │                               #   column_registry, join_graph, result_validator,
│   │   │                               #   empty_result_classifier, sql_privacy, drift_validator,
│   │   │                               #   confidence_scorer
│   │   ├── learning/                   # pattern_learner, schema_monitor, failure_capture,
│   │   │                               #   metrics, ab_test_engine  (write to databases/<id>/learned/)
│   │   ├── caches.py                   # result + semantic cache scoped by db_id
│   │   ├── fast_path.py  query_classifier.py
│   │   ├── schema_compactor.py  schema_budget.py  schema_token_estimator.py
│   │   └── pipeline.py                 # thin SQLRetriever orchestrator (target < 400 lines)
│   │
│   ├── pipeline/
│   │   ├── query.py                    # top-level: SQL vs RAG vs both
│   │   └── context_gatekeeper.py       # follow-up vs new topic (not DB-specific)
│   ├── guards/  models/  prompts/
│   ├── api/
│   │   ├── auth.py  query.py  upload.py
│   │   └── ui/                         # split api/ui.py:
│   │       ├── chats.py                #   chats, messages, titles, feedback
│   │       ├── documents.py            #   documents, versions, delete, access, users
│   │       ├── databases.py            #   connections CRUD, test, sync schema, access
│   │       ├── settings.py             #   ingestion settings, overview
│   │       └── telemetry.py
│   ├── cli.py
│   └── main.py
│
├── scripts/
│   ├── db/                             # build_*, auto_harvest_metadata, load_mysql_dump, setup_db, verify_atlas
│   ├── eval/                           # run_batch_eval, test_question, *_smoke_test, run_shadow_audit
│   ├── ops/                            # check_providers, verify_telemetry, migrate_existing_docs...
│   └── analytics/  golden/  rollout/
├── tests/                              # mirror src: rag/ sql/ core/ api/ golden/
├── evals/                              # shared harness only (run_eval.py, run_full_eval.py)
├── ui/                                 # <- Antarkosh_UI (build output gitignored)
├── docs/
│   ├── ARCHITECTURE.md  DEPLOY.md  MULTI_DB_STRUCTURE.md
│   ├── databases/ODBC_DRIVER_SETUP.md  # <- docs/ODBC_DRIVER_SETUP.md
│   └── sql/ text_to_sql_pipeline_architecture.md, sql_pipeline_enhancements.md
├── pyproject.toml  uv.lock             # single dependency source
├── Dockerfile  render.yaml  .github/workflows/ci.yml
└── README.md
```

---

## 5. `db.yaml` and `connections.json`

`db.yaml` (committed, no secrets):

```yaml
id: erp_main
display_name: ERP (Main)
description: Manufacturing ERP - parties, sales and purchase orders, stock, production, leads
engine: mysql                 # sqlite | mysql | postgresql | mssql | oracle (single source of truth)
connection: erp_main          # key in config/connections.json; sqlite uses data/sqlite/<id>.db
enabled: true
soft_delete:
  column: deleted_at          # tables with this column are auto-detected from schema.json
default_filters: []
allowed_users: [admin]        # same semantics as documents; admins always allowed
schema_sync:
  last_synced_at: null
  schema_hash: null           # used by schema_monitor to detect drift
```

`config/connections.json` (gitignored). Fields follow the engine's form spec in `db_settings.ENGINES`:

```json
{
  "erp_main":   {"host": "", "port": 3306, "database": "", "username": "", "password": ""},
  "sales_pg":   {"host": "", "port": 5432, "database": "", "username": "", "password": ""},
  "legacy_sql": {"host": "", "port": 1433, "database": "", "odbc_driver": "ODBC Driver 18 for SQL Server",
                 "username": "", "password": ""},
  "hr_oracle":  {"host": "", "port": 1521, "service_name": "", "username": "", "password": ""}
}
```

Bootstrap: on first start, an existing `config/db_connection.json` (or the `DB_*` env values) is imported once as the first entry, so current deployments keep working.

---

## 6. File-by-file placement

### Core and shared infrastructure

| File | What it does | Goes to |
|------|--------------|---------|
| `core/config.py` | Typed settings; `db_*` fields incl. `db_odbc_driver`; loads `ingestion.json` and `db_connection.json` overrides | stays; `db_*` fields reduce to bootstrap defaults |
| `core/db_config_file.py` | Atomic read/write of `config/db_connection.json` | `sql/registry.py` (reads/writes `connections.json`) |
| `core/db_settings.py` | Engine spec, validate, per-engine `_test_*`, save, hot-apply, cache clear | `sql/engine.py` (spec) + `sql/connectors/*.test()` + `sql/registry.py`; the global cache clear disappears |
| `core/db_client.py` | Read-only runner for 5 engines (timeout, row cap) | `sql/connectors/<engine>.py` |
| `core/sql_dialects.py` | Dialect facts for sqlite / mysql / postgresql | `sql/dialects/` (add `mssql.py`, `oracle.py`) |
| `core/provider_client.py`, `rate_limiter.py`, `circuit_breaker.py` | LLM routing, fallback, limits | stay in `core/` |
| `core/embeddings.py`, `local_models.py`, `qdrant_service.py` | Local embeddings, reranker, local Qdrant | stay in `core/` |
| `core/state.py`, `file_lock.py`, `paths.py` | Chat/document JSON state, locking, path safety | stay in `core/` |
| `core/ingestion_registry.py`, `metadata_store.py`, `confidence.py` | Dedup, lineage, `allowed_users`, OCR/table gate | `rag/` |
| `core/confidence_scorer.py`, `result_validator.py`, `join_graph.py`, `sql_column_registry.py`, `sql_drift_validator.py` | SQL scoring and validation | `sql/safety/` |
| `core/pattern_learner.py`, `schema_monitor.py`, `ab_test_engine.py`, `pipeline_metrics.py` | Learning, drift, experiments, metrics | `sql/learning/` (per-db output) |
| `core/context_gatekeeper.py` | Follow-up vs new-topic detection | `pipeline/` |

### `src/utils/`

| File | What it does | Goes to |
|------|--------------|---------|
| `sql_safety` | AST safety, dangerous functions (now incl. SQL Server / Oracle names, package-qualified calls) | `sql/safety/`, lists keyed by engine |
| `sql_privacy`, `empty_result_classifier` | Keep rows out of prompts, 0-row triage | `sql/safety/` |
| `schema_compactor`, `schema_budget`, `schema_token_estimator` | Shrink DDL, fit token budget | `sql/` |
| `fast_path`, `query_classifier` | Template answers, COUNT/SUM/LIST detection | `sql/` |
| `semantic_cache` | Vector cache of answers | `sql/caches.py` (scope = `db_id`) |
| `error_classification` | Error taxonomy (now incl. ODBC/Oracle driver errors) | `sql/` |
| `query_budget`, `stream_token_counter`, `retry_diagnostics`, `failure_capture` | Budgets, retries, failure log | `sql/` (failure log per db) |
| `telemetry`, `trace_context`, `trace_writer`, `feature_flags`, `golden_models` | Observability, flags, eval models | `core/` |

### Pipelines, stages and API

| File | What it does | Goes to |
|------|--------------|---------|
| `stages/s01`–`s11` | Detect, classify, parse, OCR, layout, tables, visuals, chunk, embed, store (`s11` now shares one Qdrant client) | `rag/stages/` |
| `stages/s12_s13_s14_retrieval.py` | Hybrid retrieve, rerank, generate with citations; filters by user's allowed documents | `rag/retrieval.py` |
| `stages/s12b_sql_retrieval.py` | Entire SQL path (~2.6k lines) | split across `sql/` as in the tree |
| `stages/sql_repair.py`, `prompts/delta_repair.py` | Compact SQL repair prompts | `sql/repair/` |
| `pipeline/ingestion.py`, `folder_ingestion.py` | Ingest orchestration, drop-folder scan | `rag/` |
| `pipeline/schema_ingestion.py` | Embed schema chunks into Qdrant (branches on dialect key incl. mssql/oracle) | `sql/schema_retrieval.py` (adds `db_id`, `document_id=schema:<db_id>`) |
| `pipeline/query.py`, `query_pipeline.py` | SQL/RAG/hybrid orchestration; alias | keep `pipeline/query.py`; delete the alias |
| `api/auth.py`, `query.py`, `upload.py` | Auth (`require_admin`), query endpoint, upload | stay; `query.py` accepts optional `db_id` |
| `api/ui.py` | All UI endpoints | split into `api/ui/` modules as in the tree |
| `guards/*`, `models/*`, `cli.py`, `main.py` | Guards, models, CLI, app | stay |

### Scripts

| Script | Goes to |
|--------|---------|
| `build_sql_relationships`, `build_sql_glossary`, `build_behavioral_atlas`, `generate_behavioral_atlas` (LLM variant), `auto_harvest_metadata`, `verify_atlas` | `scripts/db/`, each takes `--db <id>` and writes into `databases/<id>/` |
| `load_mysql_dump`, `setup_db` | `scripts/db/`, write `data/sqlite/<db_id>.db` |
| `run_batch_eval`, `test_question`, `sql_smoke_test`, `adversarial_smoke_test`, `production_smoke_test`, `run_shadow_audit`, `generate_benchmark_fixtures` | `scripts/eval/` |
| `check_providers`, `verify_telemetry`, `migrate_existing_docs_to_system_user` | `scripts/ops/` |
| `analytics/`, `golden/`, `rollout/` | keep as sub-folders |

### Tests

`test_db_settings`, `test_db_config_file`, `test_postgres_dialect` → `tests/sql/`. Add `test_mssql_dialect`, `test_oracle_dialect`, and per-database isolation tests. `test_alpha_auth_and_isolation` (now covers document access) → `tests/api/`. Ingestion, stage and integration tests → `tests/rag/`. Provider and telemetry tests → `tests/core/`.

---

## 7. Multi-database behavior

- **Selecting the database.** The UI sends a `db_id` with the query. If none is given, `sql/router.py` picks one by comparing the question against each database's `description` and table summaries, and asks the user when the match is ambiguous. Only databases the user is allowed to see are candidates.
- **Qdrant.** Schema chunks carry `db_id` in the payload, `document_id = schema:<db_id>`, and every schema search filters on `db_id`.
- **Caches.** Result cache key = `(db_id, normalized_question)`. Semantic cache scope = `db_id`. `DatabaseContext` is cached per `db_id`, never globally.
- **Learning.** Everything the pipeline learns about a database lands in `databases/<id>/learned/`. A successful repair appends to `learned/patterns.jsonl`; `schema_monitor` compares live introspection against `learned/schema_snapshot.json`. A review step copies approved entries into `semantics/`.
- **Onboarding a new database** (Settings > Database Connection): save connection under a new `db_id` → test → introspect → write `schema/schema.json` → infer `relationships.json` → harvest enums → draft atlas and glossary → human review → enable.
- **Access.** `allowed_users` in `db.yaml`; admins always allowed; users with no access never see the database in lists, routing or results.

---

## 8. Future-proofing

- New engine: add one connector, one dialect file and one `Engine` entry. Nothing else changes.
- Per-database overrides for thresholds, prompt rules or model routing can be added to `db.yaml` later.
- Evals live next to each database, with a shared runner in `evals/`.
- `_template/` keeps onboarding repeatable.
- Cross-database questions are out of scope for now. If needed later, run one sub-query per `db_id` and merge results outside the SQL prompt, never inside it.

---

## 9. Safe migration order

Nothing that works today may break, so migrate in steps:

0. **Close the SQL Server / Oracle gap first.** Add `mssql` and `oracle` dialect profiles (schema query, FK query, row-limit rules) and validate them against a real instance. Until then, either hide those two engines in the UI or mark them `ready: False` again.
1. Create `databases/erp_main/`, copy the five files in, and add a loader that falls back to the old `config/` paths. No behavior change (the scaffold zip already contains this).
2. Add `Engine`, `DatabaseContext` and `db_id`. Key every cache by it, add `db_id` to Qdrant payloads and filters, and re-sync schema chunks once. Remove the global cache wipe from `db_settings._apply_runtime`.
3. Add `connectors/`, `registry.py` and `connections.json` (import the existing `db_connection.json` on first start). The Settings dropdown selects a `db_id` instead of overwriting one global connection. The query API takes an optional `db_id`. Add `allowed_users` for databases.
4. Split `s12b`, moving the hardcoded table lists into `routing_hints.json`.
5. Split `api/ui.py`, then move scripts, tests and the remaining modules. Consolidate dependencies in `pyproject.toml` + `uv.lock`, after checking what the Dockerfile and CI install from.

`.gitignore` additions: `databases/*/learned/*` (keep `.gitkeep`), `config/connections.json`, `data/sqlite/`. `config/db_connection.json` and `config/ingestion.json` are already ignored.
