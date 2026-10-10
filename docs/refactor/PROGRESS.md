# Restructure Progress

Branch: `restructure`. Last recorded test result: 742 passed, 14 skipped, 8 deselected, 1 xfailed, 0 failed.
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
- `src/stages/s12b_sql_retrieval.py` re-exporting `detect_soft_delete_intent`, `_get_tables_with_soft_delete`, `_clear_soft_delete_tables_cache`, `enforce_soft_delete_filter`, `_SOFT_DELETE_TABLES_CACHE`, `FALLBACK_SOFT_DELETE_TABLES` from `src.sql.soft_delete` (removed in Phase I)
- `src/stages/s12b_sql_retrieval.py` re-exporting `extract_analytical_intent` from `src.sql.intent` (removed in Phase I)
- `src/stages/s12b_sql_retrieval.py` re-exporting `load_routing_hints`, `route_tables_for_query`, `route_anchor_tables`, `_clear_routing_hints_cache`, `_ROUTING_HINTS_CACHE` from `src.sql.table_router` (removed in Phase I)
- `src/stages/s12b_sql_retrieval.py` re-exporting `extract_schema_table_names`, `extract_table_ddl_map`, `get_1hop_neighbors`, `format_scoped_relationships`, `build_scoped_schema_fallback`, `retrieve_schema_from_qdrant` from `src.sql.schema_retrieval` (removed in Phase I)
- `src/stages/s12b_sql_retrieval.py` re-exporting `get_raw_relationships`, `load_relationships`, `load_glossary`, `get_raw_column_glossary`, `get_raw_behavioral_atlas`, `stem_word`, `matches_glossary_candidate`, `build_column_glossary_for_query`, `build_behavioral_atlas_for_query`, `get_scoped_readability_rules`, `OUTPUT_READABILITY_RULES`, `build_sql_prompt`, and prompt caches from `src.sql.prompt_builder` (removed in Phase I)
- `src/stages/s12b_sql_retrieval.py` re-exporting `generate_sql`, `execute_with_retry`, `execute_with_delta_repair`, `is_safe_read_query`, `UnsafeQueryError`, `DANGEROUS_FUNCTIONS`, `unwrap_sql`, `extract_cot_and_sql`, `format_schema_rows`, `format_fk_rows`, `format_rows_as_markdown`, `sanitize_rows`, `filter_display_rows`, `is_all_null`, `is_aggregate_over_zero_rows`, `extract_table_names`, and `fetch_sqlite_foreign_keys` from `src.sql.generation` (removed in Phase I)
- `src/stages/s12b_sql_retrieval.py` re-exporting `SQLRetriever`, `clear_knowledge_caches`, and `MAX_RESULT_CACHE_ENTRIES` from `src.sql.pipeline` (removed in Phase I)
- `src/core/join_graph.py` re-exporting `ForeignKey`, `JoinPath`, `JoinGraphBuilder` from `src.sql.safety.join_graph` (removed in Phase I)
- `src/core/schema_monitor.py` re-exporting `SchemaDrift`, `SchemaMonitor`, path constants from `src.sql.learning.schema_monitor` (removed in Phase I)
- `src/core/ab_test_engine.py` re-exporting `ExperimentEvent`, `ABTestEngine`, path constants from `src.sql.learning.ab_test_engine` (removed in Phase I)
- `src/core/context_gatekeeper.py` re-exporting `ContextAction`, `ConversationState`, `ContextGatekeeper` from `src.pipeline.context_gatekeeper` (removed in Phase I)
- `src/core/sql_drift_validator.py` re-exporting validator functions and path constants from `src.sql.safety.drift_validator` (removed in Phase I)
- `src/core/result_validator.py` re-exporting validator classes and enums from `src.sql.safety.result_validator` (removed in Phase I)
- `src/core/confidence_scorer.py` re-exporting `ConfidenceBreakdown`, `ConfidenceScorer` from `src.sql.safety.confidence_scorer` (removed in Phase I)
- `src/core/pattern_learner.py` re-exporting `LearnedPattern`, `PatternLearner`, path constants from `src.sql.learning.pattern_learner` (removed in Phase I)
- `src/utils/sql_privacy.py` re-exporting `sanitize_assistant_turn` and privacy regexes from `src.sql.safety.sql_privacy` (removed in Phase I)
- `src/core/pipeline_metrics.py` re-exporting `CURRENT_DB_ID`, `METRICS_FILE`, `PipelineEvent`, `log_event`, `get_score_summary`, `get_recent_events` from `src.sql.learning.pipeline_metrics` (removed in Phase I)
- `src/core/sql_column_registry.py` re-exporting `ColumnRegistry`, `COLUMN_GLOSSARY_PATH`, `GLOSSARY_PATH` from `src.sql.safety.column_registry` (removed in Phase I)
- `src/utils/fast_path.py` re-exporting `DISABLED_TEMPLATES`, `fast_path_format`, `format_aggregate_fast_path`, `format_list_fast_path` from `src.sql.fast_path` (removed in Phase I)
- `src/utils/semantic_cache.py` re-exporting `SemanticCache`, `get_semantic_cache`, `CachedEntry` from `src.sql.semantic_cache` (removed in Phase I)
- `src/utils/empty_result_classifier.py` re-exporting `classify_empty_result`, `VALID_EMPTY`, `SUSPICIOUS_EMPTY` from `src.sql.safety.empty_result_classifier` (removed in Phase I)
- `src/stages/sql_repair.py` re-exporting `MAX_DELTA_REPAIR_ATTEMPTS`, `attempt_delta_repair`, `extract_schema_context_from_ddl`, `extract_sql_from_response` from `src.sql.repair.sql_repair` (removed in Phase I)
- `src/prompts/delta_repair.py` re-exporting `build_delta_repair_payload`, `count_tokens`, `format_compact_schema`, `DELTA_REPAIR_SYSTEM_PROMPT` from `src.sql.repair.delta_repair` (removed in Phase I)
- `src/utils/schema_compactor.py` re-exporting `compact_ddl`, `extract_join_hints`, `AUDIT_COLUMNS` from `src.sql.schema_compactor` (removed in Phase I)
- `src/utils/schema_budget.py` re-exporting `DEFAULT_SCHEMA_TOKEN_BUDGET`, `select_schema_within_budget` from `src.sql.schema_budget` (removed in Phase I)
- `src/utils/schema_token_estimator.py` re-exporting `estimate_schema_tokens` from `src.sql.schema_token_estimator` (removed in Phase I)
- `src/utils/failure_capture.py` re-exporting `DEFAULT_FAILURE_LOG_FILE`, `capture_sql_failure` from `src.sql.learning.failure_capture` (removed in Phase I)
- `src/core/metadata_store.py` re-exporting `MetadataBackend`, `JsonMetadataBackend`, `QdrantMetadataBackend`, `create_metadata_backend`, `migrate_registry` from `src.rag.metadata_store` (removed in Phase I)
- `src/utils/sql_safety.py` re-exporting `validate_sql_safety`, `is_destructive_sql`, `check_cartesian_explosion`, `check_dangerous_patterns`, `DANGEROUS_FUNCTIONS` from `src.sql.safety.sql_safety` (removed in Phase I)
- `src/utils/query_classifier.py` re-exporting `QueryType`, `TTL_BY_QUERY_TYPE`, `classify_query`, `classify_query_intent` from `src.sql.query_classifier` (removed in Phase I)
- `src/core/ingestion_registry.py` re-exporting `IngestionRegistry`, `RegistryStatus`, `RegistryCheckResult` from `src.rag.ingestion_registry` (removed in Phase I)
- `src/stages/s01_file_detection.py` through `s11_vector_store.py` (10 shims) re-exporting from `src.rag.stages` (removed in Phase I)
- `src/stages/s12_s13_s14_retrieval.py` re-exporting from `src.rag.retrieval` with `_ShimModule` setattr mirroring (removed in Phase I)
- `src/pipeline/ingestion.py` re-exporting from `src.rag.ingestion` with `_ShimModule` (removed in Phase I)
- `src/pipeline/folder_ingestion.py` re-exporting from `src.rag.folder_ingestion` with `_ShimModule` (removed in Phase I)
- `src/pipeline/schema_ingestion.py` re-exporting from `src.sql.schema_retrieval` with `_ShimModule` (removed in Phase I)

## Open issues
See `docs/refactor/FOUND_ISSUES.md`.

