# Restructure Progress

Branch: `restructure`. Last recorded test result: 603 passed, 17 skipped, 8 deselected, 1 xfailed, 0 failed.
The agent ticks a step only after checks a-g pass, and writes the commit hash.

| Done | Step | What | Commit | Tests after |
|---|---|---|---|---|
| [x] | A | Safety net, baseline, inventory | 64ff583 | 537 passed |
| [x] | B | Path constants centralized | cf71068, 8e0db22 | 537 passed |
| [x] | C | Knowledge packs, loaders, parity test | 04a64d8, 879363f | 546 passed |
| [x] | C2-1 | Housekeeping: UTF-8 inventory, record fixes, routing_hints, template skeletons | 51b8037 | 546 passed |
| [x] | C2-2 | Remaining `src/` readers to loaders | 305bad1 | 546 passed |
| [x] | C2-3 | Scripts, evals, silent-skip test | 375a3df | 547 passed |
| [x] | C2-4 | Guard test: no legacy knowledge paths in `src/` | 3426347 | 548 passed |
| [x] | D1 | Loader hardening: db_id validation, fallback only for erp_main | e59165a | 552 passed |
| [x] | D2 | Engine enum | 5aa0a06 | 559 passed |
| [x] | D3 | DatabaseContext | a877abd | 564 passed |
| [x] | D4 | Knowledge caches keyed by db_id | 5954519 | 567 passed |
| [x] | D5 | SQLRetriever and semantic caches keyed by db_id | 69def1f | 571 passed |
| [x] | D6 | Qdrant schema chunks tagged and filtered by db_id | 5b58d8a | 574 passed |
| [x] | D7 | db_id through API and pipeline, metrics field | d4046a4 | 586 passed |
| [x] | E1 | Connectors split out of db_client | 00e709f, d4f3b35, 5aee3dc, 23f6af7, 481aac9 | 599 passed |
| [x] | E2 | Dialects package, mssql/oracle stubs (not registered) | 5b9ba01 | 603 passed |
| [x] | E3 | registry.py (connections.json, db.yaml) | 25a13bf | 618 passed |
| [ ] | E4 | db_settings into connectors/registry | | |
| [ ] | E5 | Per-database connection, remove global cache wipe | | |
| [ ] | E6 | Database access control and admin endpoints | | |
| [ ] | F1 | Extract soft_delete | | |
| [ ] | F2 | Extract intent | | |
| [ ] | F3 | Extract table_router, routing_hints.json | | |
| [ ] | F4 | Extract schema_retrieval | | |
| [ ] | F5 | Extract prompt_builder | | |
| [ ] | F6 | Extract generation | | |
| [ ] | F7 | Thin SQLRetriever (pipeline.py), s12b is a shim | | |
| [ ] | G1 | Move unused and single-importer modules | | |
| [ ] | G2 | Move 3-4 importer modules | | |
| [ ] | G3 | Move 5 importer modules | | |
| [ ] | G4 | Move sql_safety, query_classifier, ingestion_registry | | |
| [ ] | G5 | Move rag stages, retrieval, ingestion, schema_ingestion | | |
| [ ] | G6 | Learned data per database | | |
| [ ] | H1 | scripts/ reorganized | | |
| [ ] | H2 | tests/ mirror src | | |
| [ ] | H3 | evals into databases/erp_main/evals | | |
| [ ] | H4 | docs reorganized | | |
| [ ] | H5 | Docker/CI/render verified | | |
| [ ] | H6 | api/ui.py split | | |
| [ ] | I | Cleanup (needs the word "approved") | | |

## Shims currently in place
- `src/sql/knowledge/loaders.py` legacy fallback to `config/` and `evals/Antarkosh/` (removed in Phase I)
- `src/core/sql_dialects.py` re-exporting `DIALECTS`, `SQLDialectProfile`, `get_dialect_profile` from `src.sql.dialects` (removed in Phase I)
- `src/core/db_config_file.py` delegating `read`, `write`, `apply_to` to `src.sql.registry` for `erp_main` (removed in Phase I)

## Open issues
See `docs/refactor/FOUND_ISSUES.md`.
