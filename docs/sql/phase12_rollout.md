# Phase 12 rollout: prompt integration

Phase 12 is where the files built by earlier phases start to change what the model sees. It ships in small parts, each
shadow first, so a regression can be traced to one change.

| Part | What it does | Switch | Telemetry stages |
|---|---|---|---|
| 1. Route bridges | Adds the tables in the middle of an indirect join route, plus their join conditions (from `join_paths.json`) | `auto_prompt_inject` applies, `auto_prompt_shadow` only logs | `route_bridge_applied`, `route_bridge_shadow` |
| 2. Table authority | When retrieval chose a copy of a table, also offers the table that holds the real data (from `table_profiles.json`), and adds a short `Table notes` block when both are in the prompt | Shadow while the Phase 12 flags are on; applies only for databases listed in `PROMPT_HINTS_APPLY_DBS` (or `*`) | `table_authority_applied`, `table_authority_shadow` |
| 3. Compaction | Diagnostic only for now: `schema_compaction_applied` lists `uncompacted_tables` (tables the compactor could not parse) | none | `schema_compaction_applied` |

Turning on `auto_prompt_inject` (route bridges) never turns on part 2: it needs the database to be listed as well.
`erp_main` is pinned off for every part.

## Order of work for part 2

1. **Shadow.** Leave `PROMPT_HINTS_APPLY_DBS` empty. Ask questions as usual. In `data/telemetry_events.jsonl` look for
   `table_authority_shadow`: `would_add` and `pairs` (retrieval) and `notes` (prompt) show what part 2 would do. Check by hand
   that every pair really is a copy and its real table.
2. **A/B on the question suite.** Run the suite, switch the database to apply, restart, run it again, compare:

   ```
   python scripts/eval/run_question_suite.py --db-id <db> --questions <file> --out runs/before1.json
   python scripts/eval/run_question_suite.py --db-id <db> --questions <file> --out runs/before2.json   # the model varies: run the baseline twice
   (set PROMPT_HINTS_APPLY_DBS=<db> and restart)
   python scripts/eval/run_question_suite.py --db-id <db> --questions <file> --out runs/after.json
   python scripts/eval/compare_runs.py --baseline runs/before1.json runs/before2.json --candidate runs/after.json
   ```

3. **Enable per database.** Keep the database listed only if no question that worked got worse. To switch it off again, remove it
   from the list and restart; nothing else needs undoing.

Part 1 was turned on without the shadow and A/B stages. Run the suite with `auto_prompt_inject` off and on to confirm the
bridges fix failures without breaking questions that worked.
