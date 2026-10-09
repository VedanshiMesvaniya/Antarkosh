# Agent Instructions: Restructure Antarkosh for Multi-Database

How to use this file:

1. Put `MULTI_DB_STRUCTURE.md` in the repo at `docs/MULTI_DB_STRUCTURE.md` (the agent reads it as the target design).
2. Work on branch **`restructure`** (already created from `sql_update`). The agent never works on `sql_update` or `main` directly.
3. Paste **Section 2 (Master Prompt)** as the first message to the agent. The decisions the agent used to ask about are already answered inside it.
4. Then either paste **Section 2b (Run-All prompt)** to let the agent work through Phases A to H without stopping, or paste **one phase from Section 3 at a time** if you want to review between phases. Phase I (deleting old files and shims) always waits for your approval.

---

## 1. Approach (and why not a big-bang move)

**Use an incremental "strangler" migration, not one big move.**

- Moving ~100 modules in one go breaks imports in places a compiler cannot see. This repo has 51 dynamic-import or `patch("src....")` strings, many function-level imports (for example `db_settings._apply_runtime` imports `SQLRetriever` inside a function), and ~15 files that compute paths with `parents[2]` / `.parent.parent.parent`. These fail at runtime or silently, not at startup.
- So: **add the new thing, make old and new work together, move callers over, then delete the old thing.** Every step leaves the app working and tests green.
- **Moves and behavior changes are never in the same commit.** A "move" commit changes only locations and references. A "behavior" commit (db_id, new caches, new filters) changes logic and nothing else.
- **Leaf-first order.** Move modules with the fewest importers first, and `s12b_sql_retrieval.py` (22 importers) last.
- **Compatibility shims** at old locations (`from src.new.place import *  # noqa`) keep unmoved callers working until the last phase removes them.
- Copy-then-switch for data files: copy knowledge files to `databases/<id>/`, make the loader prefer the new path and fall back to the old one, and only delete the old files at the end after you approve.

---

## 2. MASTER PROMPT (paste this first)

```
You are restructuring the Antarkosh repository so it supports multiple databases cleanly.
WORK BRANCH: `restructure` (already checked out; it was created from sql_update). Use it
exactly as it is. Do NOT create, rename or switch branches, and do not stop to ask about the
branch name. The local folder name (for example Global_Mind) does not matter.

TARGET DESIGN: read docs/MULTI_DB_STRUCTURE.md fully before doing anything. It is the
source of truth for the target tree, db.yaml, connections.json, and the file-by-file
placement table. If this prompt and that file disagree, follow this prompt's DECISIONS and
rules, record the difference in FOUND_ISSUES.md, and continue.

YOUR JOB IS TO MOVE AND WIRE, NOT TO IMPROVE. The app must behave exactly the same after
every commit. Do not fix unrelated bugs, rename things "while you're there", reformat
files, change prompts, change model/provider routing, or touch the frontend (Antarkosh_UI/,
frontend/). If you notice a bug, write it in docs/refactor/FOUND_ISSUES.md and continue.

WORK IN PHASES A to H (phase prompts below). When a phase is done, append its report (format
below) to docs/refactor/PHASE_REPORTS.md, commit it, and CONTINUE to the next phase if I
asked you to run all phases; otherwise stop and wait. Never stop for naming, formatting or
minor ambiguity: pick the option closest to docs/MULTI_DB_STRUCTURE.md, record the choice in
FOUND_ISSUES.md under "Assumptions", and keep going. Phase I is never started without me
writing the word "approved".

------------------------------------------------------------------
HARD RULES
------------------------------------------------------------------
1. Never work on sql_update/main. Stay on the `restructure` branch. One commit per
   logical step, message "refactor(move): ..." or "refactor(wire): ...". Never mix a move
   with a logic change.
2. Use `git mv` for every move so history is kept. Never copy-and-delete code files.
   (Data/knowledge JSON files in the knowledge-pack phase are COPIED, originals stay until I
   approve deletion.)
3. Never print, open, commit, or log: .env, config/db_connection.json,
   config/connections.json, config/ingestion.json, or any value of DB_READONLY_PASSWORD or
   API keys. If a file might contain secrets, check only that it is gitignored.
4. Never delete a file or a shim unless the current phase tells you to. Never use
   `git reset --hard`, `git clean -fd`, force-push, or rewrite history.
5. Do not invent behavior. In particular do NOT write SQL Server or Oracle dialect SQL
   (schema/FK queries, LIMIT/TOP/FETCH rules) from memory. Those need a real instance to
   validate; use the stubs decided in DECISIONS ALREADY MADE.
6. Do not rename public API routes, JSON keys, env var names, config file names that the
   UI or deployment already uses, Qdrant collection names, or data/*.json file names unless
   the phase explicitly says so.
7. Every changed path must be recorded in docs/refactor/PATH_CHANGES.md (format below)
   in the same commit as the change.
8. If any check fails and you cannot fix it within the same step by correcting your own
   edit, revert that step (`git revert` or `git restore` on files you changed), stop, and
   report. Never "fix" a failing test by editing the test's expectations unless the test only
   encodes an old path/module name.

------------------------------------------------------------------
FACTS ABOUT THIS REPO THAT WILL BITE YOU (verified)
------------------------------------------------------------------
A. PATH DEPTH. These files compute locations from __file__. If the file moves to a
   different depth, the path silently points to the wrong place:
   src/main.py, src/core/config.py (PROJECT_ROOT), src/stages/s12_s13_s14_retrieval.py
   (config/acronym_expansions.json), src/stages/s12b_sql_retrieval.py (parents[2] for
   sql_relationships / sql_glossary / sql_column_glossary; parent.parent.parent for
   behavioral_schema_atlas), src/pipeline/schema_ingestion.py (evals/Antarkosh/
   Antarkosh_schema.json and config/sql_relationships.json), src/core/sql_drift_validator.py,
   src/core/sql_column_registry.py, src/core/db_config_file.py, src/utils/sql_safety.py,
   src/utils/feature_flags.py, src/utils/telemetry.py, src/utils/failure_capture.py,
   core/pattern_learner.py (12 path refs), plus ~25 files under scripts/ and evals/
   (scripts/db/* will be one level deeper than scripts/*), and evals/Antarkosh/run_eval.py
   (REPO = HERE.parents[1]), evals/Antarkosh/baseline_v2/run_full_eval.py (parents[3]).
   RULE: before moving any file, replace its __file__-based path math with constants from one
   place (see Phase B). After a move, grep the file for "parents[", ".parent", "__file__".

B. IMPORT FORMS TO SEARCH. A move is not finished until ALL of these forms return zero
   hits for the old name (replace the module name accordingly):
     from src.core.db_client import ...
     from src.core import db_client
     import src.core.db_client
     "src.core.db_client"  /  'src.core.db_client'      (patch/monkeypatch/importlib strings)
     src/core/db_client.py                               (paths in docs, CI, Dockerfile, README)
     relative imports: from . import db_client, from .db_client import ...
   Also search function-level imports (indented `from src...`), not only module top.
   Importer counts (files): s12b_sql_retrieval 22, s12_s13_s14_retrieval 13, ingestion_registry 12,
   db_client 10, query_classifier 10, sql_safety 8, sql_dialects 5, schema_compactor 5,
   schema_budget 5, schema_token_estimator 5, failure_capture 5, metadata_store 5.

C. TEST PATCH TARGETS. 59 monkeypatch/patch strings reference src.core / src.utils /
   src.stages / src.pipeline. A patch string pointing at a shim does NOT patch the code that
   actually runs after the move (the real module looks the name up in ITS OWN namespace).
   After each move, update patch targets to the module where the name is looked up.

D. PACKAGING. pyproject.toml uses setuptools packages.find include=["src*"]. Every new
   directory under src/ needs an __init__.py or it will not be packaged/importable the same
   way.

E. DOCKER / DEPLOY. .dockerignore excludes docs/, scripts/, tests/, data/,
   config/db_connection.json, config/ingestion.json. Therefore: databases/ must NOT be
   excluded (curated knowledge must ship in the image); add config/connections.json to
   .dockerignore; databases/*/learned/ is runtime state and needs a persistent volume in
   production. Dockerfile does COPY src ./src then COPY . . ; render.yaml uses
   ./Dockerfile. Verify both still work after each phase that touches layout.

F. CI (.github/workflows/ci.yml) runs: JSON/YAML validation of config/*.json and
   config/*.yaml; `python src/core/sql_drift_validator.py`;
   `python evals/Antarkosh/run_eval.py --offline`; `ruff check src tests scripts --select
   E9,F63,F7,F82` (catches undefined names = broken imports); `pytest -v -m "not live"
   --ignore=tests/golden`; a Qdrant integration test; a frontend job. Any path you change
   that CI uses must be updated in the SAME commit.

G. SINGLE ACTIVE DATABASE TODAY. db_settings._apply_runtime overwrites settings.db_* and
   clears SQLRetriever and SemanticCache caches on every switch. SQLRetriever has class-level
   caches (_full_schema_cache, _column_registry, _result_cache) and module-level lru_cache /
   globals (_load_glossary, _get_raw_relationships, _BEHAVIORAL_ATLAS_CACHE,
   _SOFT_DELETE_TABLES_CACHE). Schema chunks in Qdrant all use document_id="live_db_schema".

H. SQL SERVER / ORACLE ARE HALF WIRED. db_client, db_settings, config.py and the UI know
   mssql/oracle, but core/sql_dialects.py DIALECTS has only sqlite, mysql, postgresql. Also
   three dialect spellings exist: engine keys (postgresql, mssql), sqlglot names (postgres,
   tsql), and ad-hoc tuples such as ("mysql","postgres","tsql","oracle") in
   sql_column_registry.py. Do not "fix" this except as a phase tells you.

I. SHARED TELEMETRY FILE. data/pipeline_metrics.jsonl is read by scripts/run_shadow_audit.py,
   analytics scripts, and the dashboard telemetry API. Do NOT relocate it per database in this
   refactor; add a db_id field to records in the phase that introduces db_id.

J. DOCUMENT ACCESS CONTROL (allowed_users in ingestion_registry, retrieval filtering,
   /documents/{id}/access, require_admin) was just added and is covered by
   tests/test_alpha_auth_and_isolation.py. It must not change behavior at any point.

------------------------------------------------------------------
THE LOOP YOU RUN FOR EVERY MOVE OR PATH CHANGE
------------------------------------------------------------------
Step 1  DISCOVER. Before touching anything, list every reference to the thing being moved:
        - grep all import forms in B, in src/, scripts/, tests/, config/, evals/
        - grep literal path strings and file names across the WHOLE repo including *.md,
          *.yaml, *.yml, *.toml, Dockerfile, .dockerignore, render.yaml, .github/, .env.example
        - grep __file__-based path math inside the moving file and inside files that
          resolve it
        Write the full list into PATH_CHANGES.md as "pending" with file:line for each.
Step 2  READ every file on that list around the reference (not just the matched line) so you
        understand what the path is used for: read vs write, relative vs absolute, cwd-dependent,
        string vs Path.
Step 3  MOVE with git mv (or create the new file for copy-phase items).
Step 4  UPDATE every reference from Step 1. Add an __init__.py for new packages. If other
        callers have not been migrated yet, leave a one-line shim at the OLD location:
            from src.new.location import *  # noqa: F401,F403  (shim, removed in Phase I)
        and also re-export private names that others import (underscore names are not
        exported by *; list them explicitly).
Step 5  VERIFY (all must pass, in this order):
        a. grep for the OLD path/module in every form from B -> only the shim line (if any)
           remains, and only in the shim file.
        b. python -m compileall -q src scripts tests
        c. ruff check src tests scripts --select E9,F63,F7,F82
        d. python -c "import src.main"
        e. pytest -m "not live" --ignore=tests/golden -x -q   (compare with the baseline from
           Phase A: same pass count, no new failures)
        f. the two CI script steps: python src/core/sql_drift_validator.py (or its new path)
           and python evals/Antarkosh/run_eval.py --offline (or its new path)
        g. start the app (python -m uvicorn src.main:app) and call the overview/health
           endpoint and one SQL query endpoint if a local DB is available; stop the server.
Step 6  RECORD. Mark the PATH_CHANGES.md rows "done", with the commit hash.
Step 7  COMMIT. One commit. If Step 5 fails and you cannot fix your own edit, revert and stop.

------------------------------------------------------------------
PATH_CHANGES.md FORMAT (docs/refactor/PATH_CHANGES.md)
------------------------------------------------------------------
| # | old path / module | new path / module | kind (code, data, config, doc, CI) | references updated (file:line) | verified (checks a-g) | commit |

Also keep docs/refactor/FOUND_ISSUES.md (bugs/oddities you noticed but did NOT fix).

------------------------------------------------------------------
DECISIONS ALREADY MADE (do not ask again)
------------------------------------------------------------------
- Work branch: `restructure`.
- Default db_id for the existing ERP database: `erp_main`. When a query has no db_id, the
  default is `erp_main` (the only enabled database).
- mssql / oracle: LEAVE THE UI AND ENGINE SPEC AS THEY ARE (ready stays True). Do not write
  mssql/oracle dialect SQL. Do not hide or disable them. Only (1) log the gap in
  FOUND_ISSUES.md ("mssql/oracle have no SQLDialectProfile; get_dialect_profile raises
  ValueError") and (2) when you create src/sql/dialects/, create mssql.py and oracle.py as
  clearly marked stubs that raise NotImplementedError with the message "needs validation
  against a real instance", and make the dialect lookup fall back to the current behavior.
  Behavior must stay exactly what it is today.
- Baseline failures (tests that already fail before any change): record in BASELINE.md, do
  not fix, do not stop.
- Repo state: the repo may be newer than the design doc (it was written against commit
  1640431; your tip may be later). Trust the repo. Re-run discovery, log any difference in
  FOUND_ISSUES.md, and adapt the move. Stop only if a difference changes the design itself
  (for example a module in the placement table no longer exists AND is imported elsewhere).

------------------------------------------------------------------
WHEN YOU MAY STOP (only these)
------------------------------------------------------------------
- A check in step 5 fails and your own fix attempt within the same step also fails: revert
  that step, write the failure into PHASE_REPORTS.md, and stop.
- You would have to read or commit a secret.
- Phase D needs an actual re-index of a DEPLOYED Qdrant: implement and test locally, write the
  exact re-sync instructions into PHASE_REPORTS.md, and continue (do not touch any deployed
  environment).
- You reach Phase I.
Everything else: decide, record the assumption, continue.

------------------------------------------------------------------
ENVIRONMENT NOTES (Windows / PowerShell)
------------------------------------------------------------------
- The developer machine is Windows with PowerShell. Use `git grep -n` (works in PowerShell
  and bash) instead of POSIX grep, use `;` not `&&` when chaining in older PowerShell, and
  quote regexes carefully. Use pathlib everywhere in code; never hardcode `/` or `\\` in
  paths you add, and do not introduce shell-specific scripts.
- Line endings: do not change line endings of files you only move. Keep UTF-8. Do not run
  formatters over moved files.
- Use `python -m ...` forms. If a command is unavailable (ruff, docker), say so in the report
  and continue with the checks you can run.

------------------------------------------------------------------
REPORT FORMAT AFTER EACH PHASE
------------------------------------------------------------------
1. What changed (commits list, one line each)
2. PATH_CHANGES rows added
3. Verification results (a-g) with the pass/fail counts vs baseline
4. Anything you left as a shim and why
5. Anything you were unsure about, FOUND_ISSUES additions
6. What the next phase will touch
(Reports go into docs/refactor/PHASE_REPORTS.md, not only into chat.)

Acknowledge these rules in one line, read docs/MULTI_DB_STRUCTURE.md, and start with the
first phase I give you.
```

---

## 2b. RUN-ALL PROMPT (paste after the Master Prompt to let the agent work without stopping)

```
Run Phases A through H from section 3 of this file, in order, without waiting for me between
phases. After each phase: run the full verification loop, append the phase report to
docs/refactor/PHASE_REPORTS.md, commit it, and start the next phase. Follow the "WHEN YOU MAY
STOP" list strictly; for everything else decide, record the assumption in FOUND_ISSUES.md,
and continue. Do not start Phase I. When Phase H is finished, write docs/refactor/SUMMARY.md
(what moved, what remains as shims, open issues, what I must do before merging: re-sync
schema chunks, update deployment env/volume for databases/*/learned and config/connections.json)
and stop.
```

Tradeoff: running all phases unattended is faster but gives you review only at the end. Because every step is its own commit and the report file records test counts per phase, you can still find and revert the exact step that went wrong (`git revert <hash>`). If a phase fails its checks, the agent stops on its own.

---

## 3. PHASE PROMPTS (paste one at a time, after reviewing the previous report)

### Phase A: Safety net (no code moves)

```
PHASE A: baseline and safety net. Change no source files.
1. Confirm the current branch is `restructure` (do not create or switch branches).
2. Create docs/refactor/PATH_CHANGES.md and FOUND_ISSUES.md with the headers from the rules.
3. Record the baseline: run the full verification (compileall, ruff select, import src.main,
   pytest -m "not live" --ignore=tests/golden, the two CI script steps). Save exact pass/fail
   counts and the names of any ALREADY failing tests in docs/refactor/BASELINE.md. Do not fix them.
4. Generate a complete reference inventory and save it as docs/refactor/REFERENCE_INVENTORY.md:
   (a) every file that computes a path from __file__ (parents[n], .parent chains), with the
       target it resolves to;
   (b) every literal reference to: sql_glossary, sql_column_glossary, sql_relationships,
       behavioral_schema_atlas, Antarkosh_schema, evals/Antarkosh, live_data.db,
       db_connection.json, ingestion.json, learned_patterns, learning_metrics,
       schema_drift_log, failed_queries, pipeline_metrics.jsonl, sql_pattern_library,
       schema_atlas.json, experiments.yaml, acronym_expansions;
   (c) every patch()/monkeypatch string and importlib/__import__ use that names src.*;
   (d) for each module in the placement table of docs/MULTI_DB_STRUCTURE.md: its importers
       (all import forms, including function-level).
5. Add a "Refactor rules" section to CLAUDE.md summarizing the hard rules (moves vs behavior,
   PATH_CHANGES, never touch secrets).
Done when: baseline numbers recorded, inventory committed, no source file changed.
```

### Phase B: One source of truth for paths (still no moves)

```
PHASE B: centralize path constants. Behavior must be identical.
1. In src/core/config.py (it already defines PROJECT_ROOT and CONFIG_DIR) add constants:
   DATABASES_DIR = PROJECT_ROOT / "databases", SQLITE_DIR = DATA_DIR / "sqlite" (do not create
   or use it yet), and keep every existing constant unchanged.
2. Replace EVERY __file__-based path computation listed in REFERENCE_INVENTORY (a) inside src/
   with constants imported from src.core.config. Keep the resolved absolute path identical to
   what it resolved to before (assert this with a one-off script that prints old vs new for each
   replaced expression; do not commit the script).
3. For scripts/ and evals/: add a tiny helper (scripts/_paths.py) that exposes REPO_ROOT by
   walking up until pyproject.toml is found, and use it in place of parent.parent math. This
   makes scripts safe to move one level deeper later.
4. Do not change any file location in this phase.
Verify with the full loop. Done when: grep for "parents\[" and "\.parent\.parent" in
src/ returns nothing except src/core/config.py itself, and baseline numbers are unchanged.
```

### Phase C: Knowledge packs (copy, load with fallback, no behavior change)

```
PHASE C: databases/ knowledge packs.
1. Unzip/create databases/_template and databases/erp_main exactly as in
   docs/MULTI_DB_STRUCTURE.md section 4 and 5 (the scaffold zip content). COPY these into
   databases/erp_main/ (originals stay where they are):
     evals/Antarkosh/Antarkosh_schema.json  -> databases/erp_main/schema/schema.json
     config/sql_relationships.json          -> databases/erp_main/schema/relationships.json
     config/sql_glossary.json               -> databases/erp_main/semantics/glossary.json
     config/sql_column_glossary.json        -> databases/erp_main/semantics/column_glossary.json
     config/behavioral_schema_atlas.json    -> databases/erp_main/semantics/behavioral_atlas.json
2. Create src/sql/knowledge/loaders.py (add src/__init__ style packages: src/sql/__init__.py,
   src/sql/knowledge/__init__.py) with functions that return a file's path or contents for a
   given (db_id, kind), preferring databases/<db_id>/... and FALLING BACK to the old location
   if the new file is missing. db_id defaults to "erp_main" for now.
3. Switch ONLY these loaders to the new function, nothing else: s12b _get_raw_relationships,
   _load_glossary, column glossary loader, behavioral atlas loader; schema_ingestion schema
   file and relationships path; sql_drift_validator SCHEMA_FILE.
4. Update the build scripts (build_sql_relationships, build_sql_glossary,
   build_behavioral_atlas, generate_behavioral_atlas, auto_harvest_metadata, verify_atlas) to
   accept --db <id> (default erp_main) and to read/write databases/<id>/... Keep the old CLI
   arguments working.
5. Add a test that asserts old and new copies are byte-identical while both exist.
6. Update .gitignore (databases/*/learned/*, !.gitkeep, config/connections.json,
   data/sqlite/), .dockerignore (add config/connections.json; make sure databases/ is NOT
   ignored), and CI JSON validation if it should also cover databases/**.json.
7. Update CI steps that call sql_drift_validator / run_eval only if their paths changed.
Do not delete the old files. Done when: all checks pass, old-vs-new identity test passes, and
the pipeline reads the new files (prove it by temporarily renaming one old file in a scratch
run and showing the app still loads, then restore it).
```

### Phase D: db_id, caches, Qdrant filter (BEHAVIOR phase, small and careful)

```
PHASE D: introduce db_id. This is a behavior change phase; do not move files here.
1. Add src/sql/engine.py with an Engine enum (sqlite, mysql, postgresql, mssql, oracle) that
   has .sqlglot (sqlite, mysql, postgres, tsql, oracle) and .key. Do not remove the old
   spellings yet; make the old ad-hoc tuples derive from Engine instead.
2. Add src/sql/context.py: DatabaseContext (frozen dataclass) holding db_id, engine, paths,
   schema, relationships, glossary, column glossary, atlas, soft-delete tables; built once per
   db_id and cached IN A DICT KEYED BY db_id. Add DEFAULT_DB_ID = "erp_main" (decided).
3. Re-key every cache by db_id: SQLRetriever._result_cache -> (db_id, normalized question);
   _full_schema_cache, _column_registry, _load_glossary, _get_raw_relationships,
   _BEHAVIORAL_ATLAS_CACHE, _SOFT_DELETE_TABLES_CACHE; SemanticCache scope includes db_id.
4. Qdrant: schema chunks get payload db_id and document_id "schema:<db_id>". Retrieval of
   schema chunks ALWAYS filters on db_id. Provide a migration path for existing "live_db_schema"
   chunks (re-sync via the existing schema sync endpoint). Write exactly what must be
   re-synced on a deployed environment into docs/refactor/PHASE_REPORTS.md (do not touch any
   deployed environment) and continue.
5. Remove the global cache wipe in db_settings._apply_runtime ONLY after step 3 is proven by a
   new test: two db_ids, same question text, must not share any cache entry or schema chunk.
6. Add db_id to pipeline_metrics.jsonl records (do not move the file).
7. Thread an OPTIONAL db_id through the query API and pipeline; when absent, use the default.
Done when: the new two-database isolation tests pass and the old behavior with one database is
byte-for-byte unchanged in the golden/offline eval.
```

### Phase E: Connectors, dialects, registry

```
PHASE E: split engine code. The mssql/oracle decision is already made (stubs only, see DECISIONS ALREADY MADE).
1. Create src/sql/connectors/{base,sqlite,mysql,postgresql,mssql,oracle}.py by MOVING the
   branches out of core/db_client._execute and the _test_* functions out of core/db_settings.py.
   Keep behavior identical: same timeouts, row caps, Oracle lower-cased column names, MSSQL
   odbc_driver handling. Keep core/db_client.run_readonly_query as a thin shim with the same
   signature for existing callers.
2. Create src/sql/dialects/{base,sqlite,mysql,postgresql}.py by MOVING core/sql_dialects.py
   content. Add mssql.py/oracle.py ONLY according to my decision from the DECISIONS list.
   Keep core/sql_dialects.get_dialect_profile as a shim.
3. Create src/sql/registry.py that reads/writes config/connections.json (atomic write, same
   safety as db_config_file). On first start, import an existing config/db_connection.json (or
   the DB_* env values) as the entry for the default db_id. Keep db_config_file as a shim until
   Phase I. Do not log secrets.
4. Add databases/<id>/db.yaml loading (id, engine, connection, enabled, allowed_users).
   Enforce allowed_users the same way documents do (admins always allowed).
5. Add admin-only endpoints for listing/saving/testing connections and editing a database's
   allowed_users, following the style of the document-access endpoints. Do not rename existing
   routes the UI uses.
Done when: all existing db_settings/db_config_file tests pass (updated only for moved paths),
and a connection saved from the UI still round-trips.
```

### Phase F: Split s12b_sql_retrieval.py (the big one)

```
PHASE F: split src/stages/s12b_sql_retrieval.py (~2.6k lines) WITHOUT changing behavior.
Do it in several commits, extracting one concern at a time, in this order, running the full loop
after each: soft_delete.py -> intent.py -> table_router.py (move the hardcoded ERP lists,
~L585, ~L898-906, ~L1807-1834, into databases/erp_main/semantics/routing_hints.json and make
the router read them; the router code itself must contain no table names) -> schema_retrieval.py
-> prompt_builder.py -> generation.py -> pipeline.py (thin SQLRetriever).
Rules specific to this phase:
- Public names other files import from s12b (SQLRetriever, fetch_sqlite_foreign_keys and any
  other name found in the importer inventory) stay importable from the old module via
  explicit re-exports until Phase I.
- Class-level state moves with its class; do not "improve" caching beyond Phase D.
- After each extraction, the offline eval and golden-set tests must give identical results.
Done when: s12b is a thin shim, every extracted module has no ERP table names, and baseline
test counts are unchanged.
```

### Phase G: Move the remaining modules, leaf-first

```
PHASE G: move modules into src/sql/ and src/rag/ as in docs/MULTI_DB_STRUCTURE.md section 6.
Use the loop for EACH module (one commit per module, shim at the old path). Order (importer
count in brackets; do fewest first):
 1. unused or single-importer: join_graph[0], schema_monitor[0], ab_test_engine, context_gatekeeper,
    sql_drift_validator[1], result_validator[1], confidence_scorer[1], pattern_learner[1], sql_privacy[1]
 2. pipeline_metrics[3], sql_column_registry[3], fast_path[3], semantic_cache[4],
    empty_result_classifier[4], sql_repair[4], delta_repair[4]
 3. schema_compactor[5], schema_budget[5], schema_token_estimator[5], failure_capture[5],
    metadata_store[5]
 4. sql_safety[8] (make dangerous-function lists per engine, keep the union identical for now),
    query_classifier[10], ingestion_registry[12] -> rag/
 5. s11, s01-s10 stages -> rag/stages/, s12_s13_s14_retrieval[13] -> rag/retrieval.py
Special handling:
 - Modules that wrote global learning files (pattern_learner, schema_monitor, failure_capture)
   must switch to databases/<db_id>/learned/ via a single helper; keep reading old global files
   as a fallback for data that already exists. They also referenced files that do not exist
   (sql_pattern_library.json, schema_atlas.json, experiments.yaml): keep that behavior, do not
   create those files.
 - Anything under src/core/ that is infrastructure (config, paths, file_lock, state,
   qdrant_service, embeddings, local_models, provider_client, rate_limiter, circuit_breaker,
   telemetry) STAYS in core/.
 - After moving a module that tests patch, update patch strings per fact C.
Done when: every module is at its target place and only shims remain at old locations.
```

### Phase H: Scripts, tests, evals, docs, CI, Docker

```
PHASE H: peripheral layout. One commit per group.
1. scripts/ -> scripts/db, scripts/eval, scripts/ops (analytics, golden, rollout stay).
   Every script must use scripts/_paths.py (Phase B). Update README, docs, CI and any shell
   commands that call a script by path.
2. tests/ -> mirror src (tests/sql, tests/rag, tests/core, tests/api, tests/golden stays).
   pyproject testpaths and CI pytest invocations (including --ignore=tests/golden and the
   explicit tests/test_local_qdrant_integration.py path) must be updated in the same commit.
   conftest.py fixtures must still be discovered.
3. evals/Antarkosh/* -> databases/erp_main/evals/ (questions, reports, baseline_v2, run_eval.py);
   leave a shared runner in evals/. Update run_eval.py's REPO/HERE math, run_full_eval.py
   (parents[3]), scripts/run_batch_eval.py (QUESTIONS_FILE, REPORT_FILE), CI's
   `python evals/Antarkosh/run_eval.py --offline` step, and evals/Antarkosh/README.md.
4. docs: move docs/ODBC_DRIVER_SETUP.md -> docs/databases/, SQL docs -> docs/sql/; fix every
   link in README.md and other docs.
5. Docker/deploy: confirm .dockerignore (databases/ NOT ignored; connections.json ignored),
   Dockerfile COPY layers, render.yaml, and that learned/ gets a persistent location in
   production. Build the image locally if Docker is available; otherwise tell me it was not
   built.
Do NOT rename Antarkosh_UI/ or frontend/ in this refactor.
Done when: CI-equivalent commands all pass locally.
```

### Phase I: Cleanup (only after I approve)

```
PHASE I: remove compatibility layers. Do not start until I say "approved".
1. For each shim: confirm with the grep forms from fact B that nothing imports the old path,
   then delete the shim (git rm). One commit per shim group.
2. Delete the old knowledge files (config/sql_*.json, config/behavioral_schema_atlas.json,
   evals/Antarkosh/Antarkosh_schema.json) and the fallback code in loaders, in separate commits,
   after confirming the identity test and a clean startup without them.
3. Remove db_config_file.py and the old db_settings leftovers once registry/connectors cover
   them; remove requirements.txt only after you check Dockerfile and CI no longer install from
   it (report first, do not delete without my approval).
4. Final: full verification loop, final PATH_CHANGES.md review (no "pending" rows), update
   README and docs/ARCHITECTURE.md to the new tree.
```

---

## 4. Your own checklist after every phase

- `git log --oneline` shows only `refactor(move)` or `refactor(wire)` commits, never mixed.
- `docs/refactor/PATH_CHANGES.md` has a "done" row for every moved or renamed thing, with a commit hash.
- Test counts equal `BASELINE.md` (same passes, no new failures).
- `python src/core/sql_drift_validator.py` (or its new path) and the offline eval still pass.
- `git diff sql_update --stat` contains no changes under `Antarkosh_UI/` or `frontend/`.
- No secrets in the diff (`config/connections.json`, `.env`, `config/db_connection.json` are not tracked).
- Run the app yourself and ask 2–3 real questions against your ERP database before you merge.

## 5. If something goes wrong

- Revert only the failing commit (`git revert <hash>`), never reset the branch.
- Every phase ends at a state where the app works, so you can stop after any phase and still have a good tree.
- The riskiest steps are Phase D (db_id and Qdrant re-sync) and Phase F (splitting `s12b`). Test those against a copy of your data before merging, and plan the schema re-sync for any deployed environment.
