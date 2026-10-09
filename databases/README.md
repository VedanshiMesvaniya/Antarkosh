# Databases Knowledge Packs

Each directory under `databases/` represents an isolated database knowledge pack configured for Antarkosh.

## Directory Layout per Database

```
databases/<db_id>/
├── db.yaml                     # Manifest: engine, connection ref, soft delete, access control
├── schema/
│   ├── schema.json             # Introspected table and column metadata
│   └── relationships.json      # Inferred and verified foreign keys & join relationships
├── semantics/                  # Curated domain knowledge (committed)
│   ├── glossary.json           # Business terms and acronym mappings
│   ├── column_glossary.json    # Column semantic descriptions
│   ├── behavioral_atlas.json   # Query patterns, common filters, nuances
│   └── routing_hints.json      # Keyword-to-table routing hints
├── learned/                    # Machine-learned state (gitignored runtime state)
│   ├── patterns.jsonl          # Discovered query patterns from successful repairs
│   ├── pattern_metrics.json
│   ├── schema_snapshot.json    # Periodic schema snapshots for drift detection
│   ├── drift_log.jsonl
│   ├── enums_harvested.json    # Harvested column enum values
│   └── failed_queries.jsonl    # Logged failures for offline review
└── evals/                      # Database-specific evaluation datasets
    ├── questions.jsonl
    └── golden_cases.json
```

## Adding a New Database

1. Copy `databases/_template/` to `databases/<new_db_id>/`.
2. Configure `db.yaml` with appropriate `display_name`, `engine`, and `allowed_users`.
3. Add the connection credentials in `config/connections.json` (gitignored).
4. Run schema introspection and knowledge builders (`scripts/db/`).
