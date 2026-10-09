# Antarkosh Multi-DB Restructure — Step-by-Step Execution Plan

Picks up from branch `restructure` at commit `879363f` (Phases A, B and C done). The target design in `docs/MULTI_DB_STRUCTURE.md` is **unchanged**. Every requirement of the earlier plan (`AGENT_REFACTOR_PROMPT.md`) still applies. This file only (1) records what was executed, (2) lists what is not correct yet, and (3) breaks the remaining work into small steps, one prompt per step.

---

## 1. What has been executed (verified by reading the branch)

| Phase | Commits | Result |
|---|---|---|
| A: safety net | `64ff583` | Baseline 537 passed, 17 skipped, 1 xfailed. Tracking docs created |
| B: path constants | `cf71068`, `8e0db22` | `DATABASES_DIR`, `SQLITE_DIR` in `src/core/config.py`; all `__file__` math in `src/` replaced; `scripts/_paths.py` added; 12 scripts/evals wired |
| C: knowledge packs | `04a64d8`, `879363f` | `databases/_template` and `databases/erp_main` created; 5 files copied byte-identical; `src/sql/knowledge/loaders.py` with legacy fallback; s12b, schema_ingestion, drift-validator schema path switched; 6 build scripts take `--db`; parity test; `.gitignore`, `.dockerignore`, CI updated. Reported: 546 passed |

Not re-run by me: the test suite. The numbers above are from the agent's own reports. I verified the code and file state by reading them.

---

## 2. What is not correct yet (and which step fixes it)

| # | Finding | Why it matters | Fixed in |
|---|---|---|---|
| 1 | `docs/refactor/REFERENCE_INVENTORY.md` is UTF-16 (git shows it as binary) | Unreadable in diffs, and the agent cannot grep it | C2-1 |
| 2 | `FOUND_ISSUES.md` says paths were centralized in `src/core/paths.py` (it is `src/core/config.py`) and says dialect stubs "were created" (they are planned for Phase E) | Misleading record | C2-1 |
| 3 | Phase C report says drift validator `RELATIONSHIPS_FILE` was switched. The code still reads `config/sql_relationships.json`; `GLOSSARY_FILE` too | Inaccurate report, and a reader still on the old path | C2-2 |
| 4 | Three more readers still use the old `config/` files directly: `src/core/sql_column_registry.py` (L34–35, L407–418), `src/utils/sql_safety.py` (L24–25, L78–90), `src/core/sql_drift_validator.py` (L23–24) | Phase I deletes `config/sql_*.json`; these would break or silently load nothing | C2-2 |
| 5 | `scripts/verify_atlas.py` (ATLAS_PATH, SCHEMA_PATH), `scripts/build_sql_relationships.py` (`DEFAULT_OUT`, docstrings), `evals/Antarkosh/run_eval.py` (`SCHEMA`), `evals/Antarkosh/baseline_v2/run_full_eval.py` (schema path) still point at legacy files | Same deletion risk. CI runs `run_eval.py --offline` | C2-3 |
| 6 | `tests/test_sql_retrieval.py:52` silently **skips** when the relationships file is missing | Deleting the file in Phase I would hide a regression instead of failing | C2-3 |
| 7 | `databases/erp_main/semantics/routing_hints.json` was never created (the loader registers the kind); `_template/` has only `.gitkeep` files, no skeleton JSON | Phase F needs the file; onboarding needs the skeletons | C2-1 |
| 8 | No guard stops new code from re-introducing legacy paths | Regression risk through G and H | C2-4 |
| 9 | **Cross-database contamination in the loader:** `get_database_knowledge_path` falls back to the ERP legacy files for **any** `db_id` whose file is missing. The test `test_get_database_knowledge_path_fallback` asserts exactly that for `non_existent_db`. A second database with an incomplete folder would silently get ERP knowledge, which is the hallucination source this refactor exists to remove | Critical for multi-DB | D1 |
| 10 | `db_id` is not validated anywhere. Once it comes from the API, `db_id="../x"` becomes a path traversal | Security | D1 |
| 11 | **Correction to my earlier plan:** I said to remove the global cache wipe in `db_settings._apply_runtime` in Phase D. That is unsafe: the wipe also protects the case where the **connection** behind the same `db_id` changes, and per-database connections only arrive in Phase E | Stale answers after a connection switch | Moved to E5 |
| 12 | **Clarification:** the Phase E rule "dialect lookup falls back to current behavior" means `mssql`/`oracle` stubs must **not** be registered in `DIALECTS`, so `get_dialect_profile("mssql")` still raises `ValueError` exactly as today | Avoids a silent behavior change | E2 |
| 13 | **Gap in my earlier phases:** the target tree splits `src/api/ui.py` into `api/ui/` routers, but no phase did it | Plan point would be missed | H6 |
| 14 | `.dockerignore` excludes `databases/*/learned/*`, which also drops the `.gitkeep`; the `learned/` folder will not exist in the image | Code writing learned files would fail in production | G6 (create dirs on write) |
| 15 | `config/` knowledge files and their `databases/erp_main/` copies must stay byte-identical (parity test). Regenerating one side breaks the test | Confusing failures | Rule: **freeze knowledge files until Phase I** |

---

## 3. How to run it (small prompts, visible progress)

1. Copy `PROGRESS.md` and this file into the repo as `docs/refactor/PROGRESS.md` and `docs/refactor/EXECUTION_PLAN.md`. `PROGRESS.md` already marks A, B, C done. Commit both on `restructure` yourself before starting.
2. For each step: paste the **Step Preamble** below, then the one **step block**, wait for the short report, review `PROGRESS.md` and `git log`, then paste the next.
3. Each step is one to a few commits, never mixes a move with a logic change, and ends with a report of at most 12 lines.

### Step Preamble (paste before every step block)

```
Repo: branch `restructure`. Rules: docs/refactor/AGENT_REFACTOR_PROMPT.md section 2 (Master
Prompt) including the per-change loop. Design: docs/MULTI_DB_STRUCTURE.md.
Do ONLY the step below. Do not start the next step.
When finished: (1) run checks a-g; (2) tick the step in docs/refactor/PROGRESS.md with the
commit hash; (3) add rows to docs/refactor/PATH_CHANGES.md; (4) append a SHORT report to
docs/refactor/PHASE_REPORTS.md; (5) commit; (6) print the report; STOP.
Report (max 12 lines): step id | commits | files changed | what changed (one paragraph) |
checks a-g with pytest counts vs the last recorded count | shims left | issues found (also
added to FOUND_ISSUES.md) | next step id.
If a check fails and you cannot fix your own edit inside the step: revert the step, report
the failure, stop. Never print or commit secrets. Windows/PowerShell: use `git grep -n`,
`;` for chaining, pathlib in code.
```

### Checks a–g (the agent runs these after every step)

```
a. git grep -n for the old name/path in every form (from X import, from pkg import X, import,
   "dotted.string" in patch/monkeypatch, file paths in docs/CI/Docker): only a shim may remain
b. .venv\Scripts\python.exe -m compileall -q src scripts tests
c. .venv\Scripts\ruff.exe check src tests scripts --select E9,F63,F7,F82
d. .venv\Scripts\python.exe -c "import src.main"
e. .venv\Scripts\python.exe -m pytest -m "not live" --ignore=tests/golden -q --tb=short
   (must be >= last recorded passed count, 0 failed)
f. .venv\Scripts\python.exe src/core/sql_drift_validator.py   and
   .venv\Scripts\python.exe evals/Antarkosh/run_eval.py --offline
   (use the new paths once those files move)
g. start the app, GET /health and /api/overview return 200, stop it
```

---

## 4. Step list at a glance

| Step | What | Type |
|---|---|---|
| C2-1 | Housekeeping: encoding, record fixes, routing_hints, template skeletons | docs/data |
| C2-2 | Switch remaining `src/` knowledge readers to the loaders | wire |
| C2-3 | Switch scripts, evals and the silent-skip test to the loaders | wire |
| C2-4 | Guard test: no direct legacy knowledge paths in `src/` | test |
| D1 | Loader hardening: `db_id` validation, fallback only for the default DB | behavior |
| D2 | `Engine` enum, one dialect vocabulary | wire |
| D3 | `DatabaseContext` (not used by the pipeline yet) | new code |
| D4 | Key knowledge caches by `db_id` | behavior |
| D5 | Key SQLRetriever and semantic caches by `db_id` | behavior |
| D6 | Qdrant schema chunks tagged and filtered by `db_id` | behavior |
| D7 | Thread optional `db_id` through API and pipeline, metrics field | behavior |
| E1 | Connectors split out of `db_client` | move |
| E2 | Dialects package, mssql/oracle stubs (not registered) | move |
| E3 | `registry.py`: `connections.json` and `db.yaml` | new code |
| E4 | Move `db_settings` engine spec and tests into connectors/registry | move |
| E5 | Per-database connection at runtime, remove global cache wipe | behavior |
| E6 | Database access control and admin endpoints | new code |
| F1–F7 | Split `s12b_sql_retrieval.py` one concern at a time | move |
| G1–G6 | Move remaining modules leaf-first, learned files per database | move |
| H1–H6 | scripts, tests, evals, docs, Docker/CI, `api/ui.py` split | move |
| I | Cleanup (only after you write "approved") | delete |

---

## 5. Step blocks

### C2-1: Housekeeping (no code changes)

```
STEP C2-1. Docs and data only.
1. Convert docs/refactor/REFERENCE_INVENTORY.md from UTF-16 to UTF-8 (verify with
   `git diff --stat` that git now treats it as text). Do not change its content.
2. Fix docs/refactor/FOUND_ISSUES.md: paths are centralized in src/core/config.py (not
   src/core/paths.py, which is path-safety); dialect stubs are PLANNED for Phase E (not created
   yet). Fix the Phase C report in PHASE_REPORTS.md: the drift validator's GLOSSARY_FILE and
   RELATIONSHIPS_FILE were NOT switched in Phase C (done in C2-2).
3. Create databases/erp_main/semantics/routing_hints.json containing {}.
4. In databases/_template/ add skeleton files next to the .gitkeep files (keep the .gitkeep
   files, the existing template test checks them): schema/schema.json {"tables": []},
   schema/relationships.json {"relationships": []}, semantics/glossary.json {},
   semantics/column_glossary.json {}, semantics/behavioral_atlas.json {"tables": {}},
   semantics/routing_hints.json {}. Make sure the CI JSON validator still passes.
5. Add FOUND_ISSUES entry: "Freeze: do not regenerate databases/erp_main knowledge files or
   the legacy config copies until Phase I (parity test)."
6. Ensure docs/refactor/PROGRESS.md exists (I will supply it); tick A, B, C and C2-1.
```

### C2-2: Remaining `src/` readers

```
STEP C2-2. Switch the remaining readers of legacy knowledge files to
src.sql.knowledge.loaders (get_knowledge_path / load_knowledge_json). No behavior change.
Files: src/core/sql_column_registry.py (GLOSSARY_PATH, COLUMN_GLOSSARY_PATH ~L34-35 and the
loads ~L407-418), src/utils/sql_safety.py (GLOSSARY_PATH, COLUMN_GLOSSARY_PATH ~L24-25 and
loads ~L78-90), src/core/sql_drift_validator.py (GLOSSARY_FILE, RELATIONSHIPS_FILE ~L23-24).
Also read src/core/join_graph.py (~L56) and src/core/result_validator.py (~L86): confirm
whether they read a file or go through another module, and report. Fix stale docstrings and
comments in src/stages/s12b_sql_retrieval.py (~L292, ~L568) and src/pipeline/schema_ingestion.py
(~L78, ~L170) that still name the old files.
BEFORE editing: git grep -n in tests/ and scripts/ for GLOSSARY_PATH, COLUMN_GLOSSARY_PATH,
SCHEMA_FILE, GLOSSARY_FILE, RELATIONSHIPS_FILE. Tests may monkeypatch these names, so keep the
module-level names and make them point at the loader result (keep type Path).
Done when: git grep for "sql_glossary.json|sql_column_glossary.json|sql_relationships.json|
behavioral_schema_atlas.json|Antarkosh_schema.json" in src/ shows only src/sql/knowledge/loaders.py.
```

### C2-3: Scripts, evals, the silent skip

```
STEP C2-3. Switch non-src readers to the loaders.
1. scripts/verify_atlas.py (ATLAS_PATH, SCHEMA_PATH), scripts/build_sql_relationships.py
   (DEFAULT_OUT and docstrings still say config/), and any other script from
   `git grep -n -E "config/sql_|behavioral_schema_atlas|Antarkosh_schema" -- scripts`: use the
   loaders / databases/<db>/ paths. Remember scripts use scripts/_paths.py.
2. evals/Antarkosh/run_eval.py (SCHEMA = HERE / ...) and evals/Antarkosh/baseline_v2/run_full_eval.py
   (schema path): read the schema through get_knowledge_path("schema"). CI runs
   `evals/Antarkosh/run_eval.py --offline`, so confirm it still passes (163/163).
3. tests/test_sql_retrieval.py: DO NOT change the existing skip test. ADD a new test that
   asserts get_knowledge_path("relationships").exists() and that _load_relationships() is
   non-empty, so a missing file fails instead of skipping.
Done when: git grep -n for the legacy names in scripts/ and evals/ shows only docs/README text
(those are Phase H).
```

### C2-4: Guard test

```
STEP C2-4. Add tests/test_no_legacy_knowledge_paths.py: scan every .py under src/ and fail
if any file other than src/sql/knowledge/loaders.py contains one of the strings
sql_glossary.json, sql_column_glossary.json, sql_relationships.json,
behavioral_schema_atlas.json, Antarkosh_schema.json (comments and docstrings count; use a plain
text scan). Use PROJECT_ROOT from src.core.config. Done when the test passes and fails if you
temporarily add one of the strings (show this once, then revert).
```

### D1: Loader hardening (behavior)

```
STEP D1. Harden src/sql/knowledge/loaders.py. This changes behavior on purpose.
1. Add validate_db_id(db_id): lowercase letters, digits, underscore, 2-40 chars, must start
   with a letter (regex ^[a-z][a-z0-9_]{1,39}$). Call it in get_database_knowledge_path.
   Invalid -> ValueError. Also reject any relative_path containing ".." or an absolute path,
   and assert the resolved path stays inside DATABASES_DIR.
2. The legacy fallback applies ONLY when db_id == DEFAULT_DB_ID ("erp_main"). For any other
   db_id with a missing file, raise KnowledgeFileNotFound (subclass of FileNotFoundError) with
   a clear message; never read ERP files for another database.
3. load_knowledge_json: keep the "return {} on parse error" behavior for erp_main, but for other
   db_ids propagate KnowledgeFileNotFound.
4. Tests: change test_get_database_knowledge_path_fallback to assert the NEW contract (this
   test encoded the old unsafe behavior, so editing it is allowed here): non_existent_db now
   raises. Add: fallback still works for erp_main when the new file is hidden (use tmp/monkeypatch
   of DATABASES_DIR), traversal ids ("../x", "a/b", "") raise, valid ids pass.
5. Note in FOUND_ISSUES.md: finding #9 and #10 resolved here.
```

### D2: Engine enum

```
STEP D2. Create src/sql/engine.py: class Engine(str, Enum) with sqlite, mysql, postgresql,
mssql, oracle; properties .sqlglot ("sqlite","mysql","postgres","tsql","oracle"), .key.
Engine.from_value(str) must accept the existing spellings ("postgres", "tsql", "mariadb" only
if the repo already treats it so: check first) and raise ValueError otherwise.
Replace ad-hoc dialect tuples/strings with Engine-derived values in: src/core/sql_column_registry.py,
src/stages/s12b_sql_retrieval.py, src/pipeline/schema_ingestion.py (find them with
`git grep -n -E "\"tsql\"|\"postgres\"|\"mssql\"|\"oracle\"" -- src`). Values compared must
stay identical. Do not touch src/core/sql_dialects.py (Phase E). Add tests for the mapping.
```

### D3: DatabaseContext

```
STEP D3. Add src/sql/context.py: frozen dataclass DatabaseContext(db_id, engine, display_name,
description, paths for the five knowledge files, soft_delete_column) and
get_context(db_id=DEFAULT_DB_ID) cached in a dict keyed by db_id. Build it from
databases/<db_id>/db.yaml (PyYAML is already used by CI) via validate_db_id and the loaders.
Move DEFAULT_DB_ID here and re-export it from loaders (no import cycle: loaders must not import
context). Add clear_context_cache(db_id=None). Do NOT use it from the pipeline yet; only tests.
Tests: erp_main context loads; unknown db_id raises; two contexts are independent objects.
```

### D4: Knowledge caches keyed by db_id

```
STEP D4. In src/stages/s12b_sql_retrieval.py the module-level caches
(_get_raw_relationships, _load_relationships, _load_glossary, _get_raw_column_glossary, the
behavioral atlas cache _BEHAVIORAL_ATLAS_CACHE, _SOFT_DELETE_TABLES_CACHE) are process-wide.
First: git grep -n "cache_clear\|_BEHAVIORAL_ATLAS_CACHE\|_SOFT_DELETE_TABLES_CACHE" in src/ and
tests/ and list every caller; keep zero-argument calls working.
Change each to take db_id: str = DEFAULT_DB_ID and cache per db_id (a wrapper that normalizes
the argument before the lru_cache so f() and f("erp_main") share one entry). Keep a
cache_clear() on the public names. Do not change what is loaded for erp_main.
Test: monkeypatch two db folders in tmp, load both, assert no sharing; erp_main output identical
to before (compare against the old function result captured with a golden snapshot made BEFORE
the change).
```

### D5: SQLRetriever and semantic caches

```
STEP D5. Key SQLRetriever._result_cache by (db_id, normalized question), and
_full_schema_cache and _column_registry by db_id; scope SemanticCache by db_id (its scope key
must include it). SQLRetriever gets db_id from a constructor argument defaulting to DEFAULT_DB_ID;
existing callers keep working. Do NOT remove the global cache wipe in
db_settings._apply_runtime (that moves to E5; the wipe also protects connection changes).
Tests: two retrievers with different db_ids and identical question text do not share entries;
the wipe still clears all.
```

### D6: Qdrant schema chunks

```
STEP D6. In src/pipeline/schema_ingestion.py (soon src/sql/schema_retrieval.py) tag schema
chunks with payload db_id and document_id "schema:<db_id>". In s12b schema retrieval ALWAYS
filter on db_id. Transition rule: legacy chunks (document_id "live_db_schema", no db_id) are
accepted ONLY for db_id == erp_main until re-synced. Work only against local Qdrant. Add a test
in the style of tests/test_local_qdrant_integration.py: two db_ids, same table name, a query
for one never returns the other's chunks. Write the exact re-sync steps for a deployed
environment into PHASE_REPORTS.md (admin schema-sync endpoint) and do not touch any deployed
environment.
```

### D7: Thread db_id through the API and pipeline

```
STEP D7. Add an OPTIONAL db_id to the query endpoint (src/api/query.py and the UI chat query
route) and pass it to src/pipeline/query.py -> SQLRetriever. Absent -> erp_main. Invalid ->
HTTP 400 using validate_db_id. Add db_id to every pipeline_metrics.jsonl record (do not move
or rename the file). Existing request bodies without db_id must behave exactly as today
(add a test). Do not rename routes or JSON keys.
```

### E1: Connectors

```
STEP E1. Create src/sql/connectors/ (base.py Protocol: test(), run_readonly(), introspect()
if db_client has introspection, else only test and run_readonly; sqlite.py, mysql.py,
postgresql.py, mssql.py, oracle.py) by MOVING the per-engine branches out of
src/core/db_client.py _execute and the _test_* helpers' connection logic (keep db_settings
untouched in this step). Identical timeouts, row caps, Oracle lower-cased columns, MSSQL
odbc_driver handling. src/core/db_client.run_readonly_query keeps its exact signature and
becomes a thin dispatcher. One commit per engine. Update tests that patch db_client internals
to the new module where the name is looked up.
```

### E2: Dialects package

```
STEP E2. Create src/sql/dialects/ (base.py with SQLDialectProfile, sqlite.py, mysql.py,
postgresql.py) by moving src/core/sql_dialects.py content; src/core/sql_dialects.py stays as a
shim re-exporting get_dialect_profile, DIALECTS and every name its 5 importers use.
Add mssql.py and oracle.py as stubs whose profile-building function raises
NotImplementedError("needs validation against a real instance"). DO NOT register them in
DIALECTS: get_dialect_profile("mssql") must still raise ValueError exactly as today (add a test
that pins this). Update the shim list from `git grep -n "sql_dialects"`.
```

### E3: Registry

```
STEP E3. Add src/sql/registry.py: list_databases(), get_database(db_id), save_connection(db_id,
fields), get_connection(db_id). Reads/writes config/connections.json (atomic write and the same
permission handling as src/core/db_config_file.py; never log values). On first use, if
connections.json is absent, import the existing config/db_connection.json (or the DB_* env
values from settings) as the entry for erp_main, once. db.yaml loading validates engine with
Engine. Keep src/core/db_config_file.py as a shim/delegate. Tests with tmp paths; assert
passwords never appear in log output.
```

### E4: db_settings into connectors/registry

```
STEP E4. Move the engine form spec (ENGINES) into src/sql/engine.py and the per-engine _test_*
functions into the matching connector's test(). src/core/db_settings.py keeps every public
function with identical signature and behavior, delegating. The admin UI save/test flow must
return the same JSON as today (compare with the existing tests/test_db_settings.py, which must
pass unmodified except for patch targets).
```

### E5: Per-database connection at runtime

```
STEP E5. run_readonly_query and SQLRetriever resolve their connection by db_id via the
registry (default erp_main = the active connection as today). Replace the GLOBAL cache wipe in
db_settings._apply_runtime with per-db_id invalidation (clear only that db_id's schema, column
registry, result, semantic and knowledge caches) when that db_id's connection changes. Test:
change connection for db A, caches of db B survive; caches of db A are cleared.
```

### E6: Database access control and admin endpoints

```
STEP E6. Enforce allowed_users from databases/<id>/db.yaml the same way documents do (admins
always allowed; reuse require_admin and the document-access helper patterns). Hide databases the
user cannot access from lists, routing and query results. Add admin-only endpoints: list
databases, save/test a connection, edit allowed_users. Additive routes only; do not rename
existing ones. Tests mirror tests/test_alpha_auth_and_isolation.py. Document access behavior
must not change (that test file must pass unmodified).
```

### F1–F7: Split `s12b_sql_retrieval.py` (one concern per step, in this order)

Preamble for every F step (add to the step block):

```
Rules for every F step: extract ONE concern into the new module under src/sql/, leave
re-exports (explicit, including underscore names) in src/stages/s12b_sql_retrieval.py, update
patch targets in tests to the module where the name is looked up, keep class-level state with
its class, no logic edits. Before extracting, capture a golden snapshot of the affected
functions' outputs for 5-10 representative inputs and compare after. Grep importers
(22 files import s12b) and list the names each uses.
```

| Step | Extract | New module | Extra |
|---|---|---|---|
| F1 | soft-delete detection and the fallback list | `src/sql/soft_delete.py` | list stays as data for now |
| F2 | intent detection | `src/sql/intent.py` | |
| F3 | table routing and keyword maps | `src/sql/table_router.py` | move the hardcoded ERP lists (~L898–906, ~L1807–1834) into `databases/erp_main/semantics/routing_hints.json`; router code has no table names; golden routing snapshot must be identical |
| F4 | schema retrieval from Qdrant | `src/sql/schema_retrieval.py` | merge with `pipeline/schema_ingestion.py` only in G |
| F5 | prompt building | `src/sql/prompt_builder.py` | |
| F6 | SQL generation, execution glue, repair calls | `src/sql/generation.py` | repair prompts move in G |
| F7 | thin `SQLRetriever` | `src/sql/pipeline.py` | s12b becomes a re-export shim; target < 400 lines in `pipeline.py` |

### G1–G6: Module moves (leaf-first, one commit per module, shim at the old path)

| Step | Modules (importer count) | Destination |
|---|---|---|
| G1 | `join_graph`(0), `schema_monitor`(0), `ab_test_engine`, `context_gatekeeper`→`pipeline/`, `sql_drift_validator`(1), `result_validator`(1), `confidence_scorer`(1), `pattern_learner`(1), `sql_privacy`(1) | `sql/safety/`, `sql/learning/`, `pipeline/` |
| G2 | `pipeline_metrics`(3), `sql_column_registry`(3), `fast_path`(3), `semantic_cache`(4), `empty_result_classifier`(4), `sql_repair`(4), `delta_repair`(4) | `sql/` subpackages per tree |
| G3 | `schema_compactor`(5), `schema_budget`(5), `schema_token_estimator`(5), `failure_capture`(5), `metadata_store`(5) | `sql/`, `rag/` |
| G4 | `sql_safety`(8, per-engine dangerous lists, union identical), `query_classifier`(10), `ingestion_registry`(12) | `sql/safety/`, `sql/`, `rag/` |
| G5 | `s01`–`s11` → `rag/stages/`, `s12_s13_s14_retrieval`(13) → `rag/retrieval.py`, `pipeline/ingestion.py`, `folder_ingestion.py` → `rag/`, `pipeline/schema_ingestion.py` → `sql/schema_retrieval.py` (merge with F4 module), delete the `query_pipeline.py` alias after updating importers | |
| G6 | Learned data per database: `pattern_learner`, `schema_monitor`, `failure_capture` write to `databases/<db_id>/learned/` through one helper that **creates the folder on write** (the image has no `learned/`), keep reading the old global files as fallback; do **not** create `sql_pattern_library.json`, `schema_atlas.json`, `experiments.yaml` | |

Step block for every G step:

```
STEP G<n>. Move the modules in the table row for this step. Per module: run the per-change
loop (discover all import forms and patch strings, git mv, update references, leave a shim at
the old path re-exporting public AND underscore names, add __init__.py for new packages,
verify, record in PATH_CHANGES, one commit). Infra stays in src/core/: config, paths,
file_lock, state, qdrant_service, embeddings, local_models, provider_client, rate_limiter,
circuit_breaker, telemetry, trace_*. If a module has a __file__ path, it must already use
PROJECT_ROOT/CONFIG_DIR/DATABASES_DIR from src.core.config.
```

### H1–H6: Peripheral layout

| Step | Do |
|---|---|
| H1 | `scripts/` → `scripts/db`, `scripts/eval`, `scripts/ops` (analytics, golden, rollout stay). All scripts use `scripts/_paths.py`; fix README, docs, CI and any shell command that names a script path |
| H2 | `tests/` → mirror `src` (`tests/sql`, `tests/rag`, `tests/core`, `tests/api`; `tests/golden` stays). Update `pyproject.toml` `testpaths`, CI pytest lines (incl. `--ignore=tests/golden` and `tests/test_local_qdrant_integration.py`), keep `conftest.py` discovery |
| H3 | `evals/Antarkosh/*` → `databases/erp_main/evals/`; shared runner stays in `evals/`. Update `run_eval.py`, `run_full_eval.py` (`parents[3]`), `scripts/run_batch_eval.py` (QUESTIONS_FILE, REPORT_FILE), the CI `run_eval.py --offline` step and its README |
| H4 | Docs: `docs/ODBC_DRIVER_SETUP.md` → `docs/databases/`, SQL docs → `docs/sql/`; fix every link in `README.md` and other docs |
| H5 | Docker/deploy: `.dockerignore` (`databases/` shipped, `learned/` and `connections.json` not), Dockerfile COPY layers, `render.yaml`, persistent location for `databases/*/learned`; build the image if Docker exists, otherwise say so |
| H6 | Split `src/api/ui.py` into `src/api/ui/` (`chats.py`, `documents.py`, `databases.py`, `settings.py`, `telemetry.py`). Same routes, same paths, same order of registration, same auth dependencies. Compare the full route table (method, path, name) before and after |

H-step block:

```
STEP H<n>. Do the row for this step from section 5 of docs/refactor EXECUTION_PLAN.md. One
commit per sub-move. Do not rename Antarkosh_UI/ or frontend/. After the step, list every file
that names a changed path (CI, Docker, README, docs) and confirm each was updated.
```

### I: Cleanup (do not start until you write "approved")

```
PHASE I. For each shim: git grep all import forms; if only the shim remains, git rm it
(one commit per group). Then delete the legacy knowledge files (config/sql_*.json,
config/behavioral_schema_atlas.json, evals/Antarkosh/Antarkosh_schema.json) and the loader's
legacy-fallback branch in separate commits, after confirming the parity test is replaced by a
"files exist in databases/erp_main" test and a clean start works without them. Remove
db_config_file.py leftovers once the registry covers them. Report on requirements.txt vs
pyproject/uv.lock and the Dockerfile before deleting anything. Final: PATH_CHANGES has no
pending rows; update README and docs/ARCHITECTURE.md to the new tree.
```

---

## 6. Review points for you (your part, after the steps that matter most)

- **After C2-4:** `git grep` for the five legacy filenames in `src/` shows only `loaders.py`.
- **After D1:** a made-up `db_id` raises instead of loading ERP files.
- **After D6:** run the schema sync locally and ask one question per `db_id`; check the schema chunks in local Qdrant carry `db_id`.
- **After E5:** switch the connection in Settings and confirm only that database's caches clear.
- **After F3:** the routing snapshot test is identical and `s12b` no longer contains `party`, `sales_order`, `financial_year`.
- **Before merge:** the schema re-sync note in `PHASE_REPORTS.md` and a persistent location for `databases/*/learned` and `config/connections.json` in production.
