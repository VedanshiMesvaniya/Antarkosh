# Release 2: aggregate-only validation of likely joins

Flag: `auto_relationships_data_apply` (per database, starts **false**; needs `auto_relationships`). With it off, Phase 6 never
touches the data and every file is exactly as before.

## What it does (flag on)

After Phase 6 has proposed joins from names, keys and procedures (and before it writes), a **short list** of the proposed joins is
checked against the connected database. Per pair, one statement returns four integers and nothing else:

```text
rows read from the child column (bounded sample) | not null | distinct | distinct values that exist in the parent key
```

| Result | Meaning | Effect |
| --- | --- | --- |
| `supported` | at least 98% of the distinct child values exist in the parent key, from at least 20 distinct values (or the values cover at least half of a small parent) | adds the witness `data_match` (0.55); with the name evidence the edge reaches `relationships.json` |
| `contradicted` | fewer than 50% exist in the parent | a soft edge is held back (`held_back.data_contradicted`): review file only, no prompt, no route |
| `partial` | 50% to 98%, or too few distinct values to mean anything | no change |
| `insufficient` / `error` | nearly empty column, or the query failed or timed out | no change |

Declared foreign keys and procedure-derived joins are facts: never queried, never blocked. One column that could point at several
parents is cleared as ambiguous only when exactly one parent is supported and the data refuted every other.

## The short list (never every column against every other)

Edges another rule already proposed, whose parent column is a primary/unique key, whose types can be compared as keys, direction
known, both tables real (not views) and in the schema the prompts see; best score first.

## Budgets per sync

`MAX_CANDIDATES` 300 pairs, `MAX_QUERIES` 150, `DATA_BUDGET_SECONDS` 60, `QUERY_TIMEOUT_SECONDS` 15 per query, stop after
`MAX_CONSECUTIVE_ERRORS` 5 failures in a row, child sample `SAMPLE_ROWS` 50,000 (the first rows the engine returns, not random).

## Safety

* Aggregates only: no identifier, key or row value is selected, stored, logged or sent to a model. Errors are recorded by exception
  class name only, and the database client's error log is redacted while a probe of this phase runs.
* Read-only: the statement is built with sqlglot (identifiers quoted per dialect), checked to be a single SELECT, and run through
  `run_readonly_query` (read-only connection, hard timeout).
* Cached in `schema/relationship_evidence.json` by structural fingerprint of both tables (names, types, keys, indexes; not row
  counts). An unchanged schema costs no query. Entries expire after 14 days (failures after 24 hours). A file this phase did not
  write is never read or overwritten. The file holds counts only and is never read by prompts. It is generated: do not edit it.
* Failure-proof: any failure becomes a report and the edges stay exactly what Phase 6 produced without data.

## Turning it on and checking it

Set `auto_relationships_data_apply: true` for the database. With `auto_relationships_apply` off you get review mode: the data
facts appear in `schema/inferred_relationships.json` (`data_check`, `data_evidence`, `publishable`) and nothing is published.
Read them, then turn `auto_relationships_apply` on. The sync report carries `relationships.validation` (counts only). To undo, set
the flag back to `false`; the next sync rewrites the files without data evidence.

Once an edge is published, `schema/join_paths.json` builds multi-hop routes through it on the same sync (`auto_join_paths`).
