# Restructure Progress

Branch: `restructure`. Last recorded test result: 713 passed, 13 skipped, 8 deselected, 1 xfailed, 0 failed.
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
| [x] | E4 | db_settings into connectors/registry | cba67de | 621 passed |
| [x] | E5 | Per-database connection, remove global cache wipe | 622dd28 | 625 passed |
| [x] | E6 | Database access control and admin endpoints | d9f6606 | 634 passed |
| [x] | F1 | Extract soft_delete | 816be00 | 652 passed |
| [x] | F2 | Extract intent | 4837b9b | 662 passed |
| [x] | F3 | Extract table_router, routing_hints.json | ac1fa08 | 675 passed |
| [x] | F4 | Extract schema_retrieval | F4 | 683 passed |
| [x] | F5 | Extract prompt_builder | F5 | 690 passed |
| [x] | F6 | Extract generation | F6 | 698 passed |
| [x] | F7 | Thin SQLRetriever (pipeline.py), s12b is a shim | F7 | 704 passed |
| [x] | G1 | Move unused and single-importer modules | G1 | 710 passed |
| [x] | G2 | Move 3-4 importer modules | 01a7a7f | 714 passed |
| [x] | G3 | Move 5 importer modules | 05e1792 | 720 passed |
| [x] | G4 | Move sql_safety, query_classifier, ingestion_registry | 82cc769 | 727 passed |
| [x] | G5 | Move rag stages, retrieval, ingestion, schema_ingestion | 98966bc | 735 passed |
| [x] | G6 | Learned data per database | a129765 | 742 passed |
| [x] | H1 | scripts/ reorganized | 59ab567, 0f9f882, 4b7b818, 9098c57, c843cb9 | 742 passed |
| [x] | H2 | tests/ mirror src | feeba3a, 1338266, 6daf4e8, 00b4ddd, c42c3e3 | 742 passed |
| [x] | H3 | evals into databases/erp_main/evals | 8c869c7, 01ce81e, 51bd408, 8b26a4b | 742 passed |
| [x] | H4 | docs reorganized | 44109f5, 2de3262, 34e511c | 742 passed |
| [x] | H6 | api/ui.py split | 988247a, fa82f91, 73a9b51, ca53e87, a8c1c34, 7508536, c37ccd1, de7afef | 741 passed |
| [x] | I | Cleanup: all shims removed, legacy files deleted, loaders clean, db_config_file removed | 3488c09, 6213958, 6b2e28e, 81ddece, aa2d800, 92d0f2f, 3dd4570, 01f5905, fb3cd6f | 713 passed |

## Shims currently in place
None (all compatibility shims cleanly removed across Groups G1–G5, s12b_sql_retrieval, sql_dialects, legacy knowledge loaders fallback, and db_config_file).

## Open issues
See `docs/refactor/FOUND_ISSUES.md`.

