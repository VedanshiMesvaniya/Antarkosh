# Release 4: evidence-based soft-delete protection

Two flags, both per database and both starting **off**, enabled in this order:

| Stage | Flag | What changes |
| --- | --- | --- |
| 1. Shadow | `auto_soft_delete_evidence_apply` (needs `auto_catalog`) | The sync asks the database aggregate counts about likely soft-delete columns and writes `semantics/soft_delete_evidence.json`. At question time the guard only **logs** what it would have done (`soft_delete_guard_shadow`). No answer changes. |
| 2. Enforce | `auto_soft_delete_guard_apply` (needs stage 1) | The data's verdicts take effect (below). |

With both flags off nothing runs and every file, prompt and answer is exactly as before. Name+type discovery is unchanged and remains the
fallback; declared (`db.yaml`), curated (`semantics/soft_delete.json`) and atlas specs are facts: never queried, never demoted.

## What is asked of the database (aggregate counts only)

For each likely marker column (`deleted_at`, `deleted_on`, `is_deleted`, `record_deleted`, `deleted`, `removed`, `rescinded` and their usual
spellings; never a key, a foreign key or a view), over a bounded sample (`SAMPLE_ROWS` 50,000), ONE statement returns four or five integers:
rows read, not NULL, distinct values, and how many equal the usual "deleted" and "live" spellings of the column's type (`'Y'`/`'N'`,
`1`/`0`, `TRUE`/`FALSE`). No identifier, key or row value is selected, stored, logged or sent to a model. Errors are stored by exception class
name only, and the database client's error log is redacted while a probe of this phase runs.

## How a table is classified

| Class | Meaning | Examples of evidence |
| --- | --- | --- |
| known active-row condition | declared, curated, atlas, name+type, or **verified** by the data | only `'N'`/`'Y'` (some live) -> `col IN ('N','n') OR col IS NULL`; only `0`/`1` -> `col = 0 OR col IS NULL`; a nullable timestamp with some NULLs -> `col IS NULL` |
| no soft-delete marker | no likely column, or the data shows it is not one | a counter named `deleted` (many distinct values); too few rows (`MIN_ROWS` 20) to say |
| uncertain possible marker | a likely column the data does not prove | a flag holding a few other codes (<= `MAX_CODE_DISTINCT` 6 distinct values); a date held as text with NULLs |

A name+type guess the data contradicts is also set aside: a "deleted" timestamp that is never NULL (`IS NULL` would drop every row), a flag
where every row is "deleted", or a flag with unreadable codes.

## What enforcing does (stage 2)

* A column the counts **verified** and name+type never found (`source = "verified"`) becomes a filter like any other, with the same conditions.
* A name+type guess the counts contradict is no longer enforced.
* A query on a table whose marker is only a possibility (or a contradicted guess) is **refused** with a plain message ("I can't answer this
  reliably yet ... ask an administrator to confirm that column's convention") instead of risking deleted rows or dropping valid ones.
  Audit/history questions add no filter and are never refused.

## Safeguards

Bounded per sync: `MAX_QUERIES` 200, `DATA_BUDGET_SECONDS` 60, `QUERY_TIMEOUT_SECONDS` 15, stop after 5 failures in a row. Cached by a
structural fingerprint of each table (14 days, failures 24 hours): an unchanged schema costs no query. A file this phase did not write is never
read or replaced. Every failure becomes a report and leaves the specs exactly as they were.

## Rollout and monitoring

1. Turn on `auto_soft_delete_evidence_apply` for one database, run a sync, read `semantics/soft_delete_evidence.json` and the sync report's
   `soft_delete` section (`supported`, `uncertain`, `contradicted`, `verified`, `contradicted_guesses`; counts only).
2. Ask normal questions and read `soft_delete_guard_shadow` telemetry: `would_refuse` (tables the guard would stop) and `would_filter` (tables
   that would gain a filter). Compare with what answers do today.
3. When satisfied, turn on `auto_soft_delete_guard_apply` for that database (`soft_delete_guard_applied` records `refused`).
4. To settle an uncertain table for good, declare it in `semantics/soft_delete.json`; a curated spec is a fact and ends the question.

To undo, set the flags back to `false`. The evidence file can be deleted at any time; the next sync recreates it.

## Limits

The sample is the first rows the engine returns, not a random sample. Codes are never read from their character alone: a flag is verified
only when every non-NULL value is one of the two usual spellings. A convention that is not Y/N, 1/0 or TRUE/FALSE stays uncertain until a
person declares it.
