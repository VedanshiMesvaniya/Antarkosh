# Phase 13: re-sync and drift

Your schema changes over time and the generated files go stale. Phase 13 makes a sync remember what it did, so the next one
can say what drifted, skip work that cannot have changed, and be undone after the fact. It needs the per-database
`auto_sync_orchestration` flag (Phase 11) and is **shadow by default**: nothing a sync does changes until you list a database.

| Part | What it does | Switch |
|---|---|---|
| 1. Drift and the record | After a committed sync, `databases/<db>/.sync/state.json` keeps a fingerprint per table (columns, types, declared foreign keys), a checksum of every generated file, and what each phase reported. The next sync's report has `drift`: tables added, dropped, changed. | on with the flag |
| 2. Kept versions | A copy of the generated files after every committed sync that changed them (`.sync/versions/v0001`, `v0002`, ...). The newest 5 are kept. | on with the flag |
| 3. Skip unchanged phases | Skips the catalog, relationships, table profiles and join paths when nothing they depend on changed. Never skips procedure reading/mining or column meaning. | shadow; `RESYNC_APPLY_DBS` applies |
| 4. Stale chunks | Removes the vector chunk of a table that was in the last good sync and is gone now. | shadow; `STALE_CHUNKS_APPLY_DBS` applies |

## When a phase is skipped

Only when all of these hold, otherwise it runs exactly as before: the last good sync is on record and clean and under 7 days old
(so row estimates refresh weekly); every table and foreign key hashes the same; the same phases are switched on with the same
rule versions; the phase's last result was ok; its own output files are byte-for-byte what the last sync left; and every file
it reads that an earlier phase writes is unchanged, checked just before it would run. Any doubt means it runs.

## Order of work

1. **Update and sync once.** The first sync after the update runs everything (there is no record yet) and writes the record and
   `v0001`. Look at the report in the sync response (`orchestration`): `drift`, `resync`, `version`, `stale_chunks`.
2. **Shadow.** Sync again with nothing changed. `resync.would_skip` lists what apply mode would have skipped; every phase still
   ran. After a real schema change, `drift` names the tables and `stale_chunks.would_remove` names the chunks that would go.
3. **Apply skipping.** Set `RESYNC_APPLY_DBS=<db>` and restart. A sync with nothing changed now reports `resync.skipped` and
   leaves every file byte-for-byte as it was. Remove the id to go back.
4. **Apply chunk removal.** After reviewing `would_remove` over a few syncs, set `STALE_CHUNKS_APPLY_DBS=<db>`. It refuses when the
   schema read may have been cut short, when there is no earlier record, or when more than half the tables vanish at once.

## Rolling back

```
python scripts/sync/manage_versions.py list --db-id <db>
python scripts/sync/manage_versions.py show --db-id <db> --version v0002
python scripts/sync/manage_versions.py rollback --db-id <db> --version v0002
```

A rollback refuses while a sync runs, if the version fails its checksums, or if an earlier sync was interrupted. It keeps the
current files as a new version first (so you can roll forward again), restores only files this project generated, never
overwrites a valid file a person put in their place, and marks the record untrusted so the next sync runs every phase.
**The vector store is not rolled back**: run a sync afterwards to rebuild it.

## Limits to know about

- Skipping the catalog means row estimates can be up to 7 days old.
- A sync whose catalog reports a consistency problem (for example a view that cannot be embedded) is "not clean": its record
  is kept, but the next sync runs every phase. That is the safe direction, not a fault.
- Versions hold only generated files, never curated ones. Each version is a full copy: five copies of `schema.json` and the
  other generated files per database.
