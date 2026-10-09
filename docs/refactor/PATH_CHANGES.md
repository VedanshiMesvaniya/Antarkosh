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
| B-1 | (new) | scripts/_paths.py | code | helper module for REPO_ROOT discovery | a-g pass | (pending) |
| B-2 | src/core/config.py | src/core/config.py | code | added DATABASES_DIR, SQLITE_DIR | a-g pass | (pending) |
| B-3 | src/main.py | src/main.py | code | replaced __file__ math with PROJECT_ROOT | a-g pass | (pending) |
| B-4 | src/core/db_config_file.py | src/core/db_config_file.py | code | dynamic root discovery without config loop | a-g pass | (pending) |
| B-5 | src/core/sql_column_registry.py | src/core/sql_column_registry.py | code | REPO_ROOT -> PROJECT_ROOT, CONFIG_DIR | a-g pass | (pending) |
| B-6 | src/core/sql_drift_validator.py | src/core/sql_drift_validator.py | code | REPO_ROOT -> PROJECT_ROOT, CONFIG_DIR | a-g pass | (pending) |
| B-7 | src/pipeline/schema_ingestion.py | src/pipeline/schema_ingestion.py | code | schema_file/rel_path -> PROJECT_ROOT/CONFIG_DIR | a-g pass | (pending) |
| B-8 | src/stages/s12b_sql_retrieval.py | src/stages/s12b_sql_retrieval.py | code | rels/glossary/col_glossary/atlas -> CONFIG_DIR | a-g pass | (pending) |
| B-9 | src/stages/s12_s13_s14_retrieval.py | src/stages/s12_s13_s14_retrieval.py | code | expansion_path -> CONFIG_DIR | a-g pass | (pending) |
| B-10 | src/utils/failure_capture.py | src/utils/failure_capture.py | code | DATA_DIR / failed_queries.jsonl | a-g pass | (pending) |
| B-11 | src/utils/feature_flags.py | src/utils/feature_flags.py | code | CONFIG_DIR / feature_flags.yaml | a-g pass | (pending) |
| B-12 | src/utils/sql_safety.py | src/utils/sql_safety.py | code | REPO_ROOT/CONFIG_DIR | a-g pass | (pending) |
| B-13 | src/utils/telemetry.py | src/utils/telemetry.py | code | DATA_DIR / telemetry_events.jsonl | a-g pass | (pending) |
| B-14 | scripts/* | scripts/* | code | 10 script files wired to scripts._paths | a-g pass | (pending) |
| B-15 | evals/* | evals/* | code | 2 eval scripts wired to dynamic REPO root | a-g pass | (pending) |
