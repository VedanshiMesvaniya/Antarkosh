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
| C2-1-1 | docs/refactor/REFERENCE_INVENTORY.md | docs/refactor/REFERENCE_INVENTORY.md | doc | converted to clean UTF-8 text | a-g pass | pending |
| C2-1-2 | (new) | databases/erp_main/semantics/routing_hints.json | data | empty JSON object {} | a-g pass | pending |
| C2-1-3 | (new) | databases/_template/{schema,semantics}/* | data | 6 skeleton json files next to .gitkeep | a-g pass | pending |
| C2-1-4 | docs/refactor/FOUND_ISSUES.md | docs/refactor/FOUND_ISSUES.md | doc | path/dialect clarifications + freeze entry | a-g pass | pending |
| C2-1-5 | docs/refactor/PHASE_REPORTS.md | docs/refactor/PHASE_REPORTS.md | doc | corrected Phase C drift validator report | a-g pass | pending |
