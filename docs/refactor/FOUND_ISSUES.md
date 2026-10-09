# FOUND_ISSUES.md — Multi-DB Restructure

Tracks bugs, architectural gaps, and differences between design docs and live repository state found during the multi-database restructure. These are documented here rather than fixed in-place, adhering to the "move and wire, do not improve" policy.

---

## 1. Pre-existing Gaps & Oddities

### Dialect Gaps
- **MSSQL and Oracle Dialects**: `core/db_client.py` and `core/db_settings.py` support MSSQL (`pyodbc`) and Oracle (`oracledb`), with UI marking them `ready: True`. However, `core/sql_dialects.py` only defines profiles for `sqlite`, `mysql`, and `postgresql`. Calling `get_dialect_profile("mssql")` or `get_dialect_profile("oracle")` raises `ValueError`.
  - *Refactor Treatment*: Dialect stubs are PLANNED for Phase E (not created yet): stubs will be created in `src/sql/dialects/mssql.py` and `oracle.py` raising `NotImplementedError("needs validation against a real instance")`. Lookup falls back safely without breaking existing behavior.

### Hardcoded Domain Knowledge
- **ERP Tables Hardcoded in Generic Retrieval**: `src/stages/s12b_sql_retrieval.py` hardcodes table names such as `party`, `sales_order`, `financial_year`, `carton`, as well as soft-delete columns and domain keyword maps.
  - *Refactor Treatment*: Moving domain table lists and keyword maps into `databases/erp_main/semantics/routing_hints.json` in Phase F.

### Path Fragility (`__file__` math)
- Over 25 files compute paths relative to `__file__` with deep parent traversing (e.g. `Path(__file__).parents[2]` or `.parent.parent.parent`). Moving files changes their depth and breaks these paths unless replaced with centralized constants.
  - *Refactor Treatment*: Paths are centralized in `src/core/config.py` (not `src/core/paths.py`, which is path-safety) and `scripts/_paths.py` during Phase B before code moves.

### Single Active Database & Global Cache Clearing
- Today, `db_settings._apply_runtime` clears all retriever and semantic caches globally on every database connection change.
  - *Refactor Treatment*: Key all caches and contexts by `db_id` (Phase D).

### Resolved Issues (Step D1)
- **Finding #9 (Cross-database contamination in loader)**: Previously, `get_database_knowledge_path` fell back to legacy ERP files for any `db_id` with missing files. Resolved in Step D1: fallback is restricted strictly to `DEFAULT_DB_ID` (`erp_main`); all other databases raise `KnowledgeFileNotFound`.
- **Finding #10 (db_id validation & path traversal)**: `db_id` was unvalidated and could allow traversal. Resolved in Step D1: added `validate_db_id` enforcing regex `^[a-z][a-z0-9_]{1,39}$`, reject `..` / absolute paths, and assert path stays within `DATABASES_DIR`.

### Test Sensitivity (Step E1)
- **Finding #11 (`test_telemetry_latency_under_5ms_guardrail_4` concurrency jitter)**: `tests/test_dashboard_telemetry_api.py::test_telemetry_latency_under_5ms_guardrail_4` asserts mean HTTP request latency against in-memory aggregator is < 5.0ms. Under heavy full-test-suite runs (600 tests), Windows CPU scheduling can measure 5.2-6.4ms on cold ASGI dispatch; passes consistently (< 2ms) when run standalone.


---

## 2. Assumptions & Decisions Made

- **Branch**: Working directly on `restructure` branch.
- **Default Database ID**: `erp_main` is the default `db_id` for the pre-existing ERP database. If a query does not supply `db_id`, it resolves to `erp_main`.
- **Secrets & Gitignore**: `config/connections.json` and `data/sqlite/` are strictly gitignored and excluded from Docker image builds.
- **Preservation of Document Access**: Granular document access control (`allowed_users` in `ingestion_registry`, retrieval filtering, `/documents/{id}/access`, `require_admin`) remains untouched.
- **Freeze**: Do not regenerate `databases/erp_main` knowledge files or the legacy `config/` copies until Phase I (parity test).
