# Static policy mappings from local procedure scripts

## Why this exists

`databases/<db_id>/procedures/` is a safe place to keep local SQL procedure definitions that explain an application's schema. A
procedure file is **not deployed or executed** by Antarkosh. During sync it is read as text together with database routine
definitions, then discarded after generated metadata is written.

The special procedure `Policy_GetColumnValueMappings` can now carry small, authored value definitions that the SQL generator
needs. This closes the gap where a script correctly says `India = IND`, but a question such as “show suppliers from India” did
not receive that fact in its SQL prompt.

## Required shape

Keep the mappings as literal string rows in the procedure's `FROM (VALUES ...)` table, with these seven fields in this order:

```sql
(N'table_name', N'column_name', N'stored_value', N'meaning',
 N'CONFIRMED' /* or INFERRED / UNRESOLVED */, N'evidence', N'query guidance')
```

Example:

```sql
(N'gen_supplier_master', N'country', N'IND, CAN, DEU',
 N'Three-letter country codes: India = IND, Canada = CAN, Germany = DEU.',
 N'INFERRED', N'Policy notes', N'Filter with country = ''IND'' for India.')
```

The table and column must exist in the generated `schema/schema.json`. Unknown names and malformed rows are ignored. Rows marked
`UNRESOLVED` are retained in the generated metadata for review but are never sent to the model as query guidance.

## What a sync does

```text
procedures/Policy_SP.sql (local text)
  -> literal Policy_GetColumnValueMappings VALUES rows only
  -> catalog validation (known table + known column)
  -> schema/column_hints.json.policy_mappings
  -> relevant, bounded SQL-prompt guidance
```

Only a folder script that defines `Policy_GetColumnValueMappings` is eligible. Antarkosh does not execute that procedure,
`Policy_GetLookupValues`, `Policy_ProfileUnresolvedColumns`, or any other procedure. It does not connect to the database again
for this feature and does not collect `SELECT DISTINCT`, profile, or lookup result rows.

For a question about a shown table, only mappings whose wording matches the question are inserted. At most eight `CONFIRMED` or
`INFERRED` rows are included. A mapping is explicitly labelled as local static policy metadata; it is not a soft-delete rule.

## QTS country example

With the current `qts_ais` policy entries, a retrieved `gen_supplier_master` or `gen_ship_point_master` schema plus a question
containing “India” receives the documented `India = IND` guidance. The SQL generator can therefore form a filter such as
`country = 'IND'` instead of guessing a full country name.

This does **not** make `gen_plant_master.country_name` an ISO-code column: its policy row is `UNRESOLVED` and says to use the
actual free-text form. It also does **not** make `active` a soft-delete flag. An explicit request for an active/inactive status
may use a documented status mapping; an ordinary operational query cannot acquire an `active` filter merely from this policy.

## Privacy boundary

No live data reaches the LLM through this feature. The only text it can receive is the static definition an administrator put in
the local policy file. Do not paste sensitive customer names, raw exports, or profile output into that file: static values written
there are still authored prompt metadata when relevant.

## Operating it

1. Edit `databases/<db_id>/procedures/Policy_SP.sql` locally. Do not need to deploy it to the connected database.
2. Run the normal schema sync for that database. Restarting the application alone does not regenerate the metadata.
3. Confirm the sync report has a `column_hints.policy_mappings` count greater than zero, and inspect
   `databases/<db_id>/schema/column_hints.json`.
4. Ask a question whose schema includes the mapped table, for example “show suppliers from India”.

If a policy edit needs to be undone after a sync, use the generated-version rollback described in
[Phase 13 re-sync and drift](phase13_resync.md#rolling-back), then run another sync to rebuild the vector state. The policy file
itself is authored input and is not changed by rollback; edit or restore that file separately.

## Limits

- This parser is intentionally narrow: it accepts literal strings in the seven-field `VALUES` table, not dynamic SQL, variables,
  joins, or procedure output.
- `INFERRED` is useful for a carefully documented observed convention, but should not be labelled `CONFIRMED` unless code or a
  controlled reference proves it.
- The mapping is SQL-generation guidance, not a hard SQL rewrite. Add explicit mappings for each value relationship the model
  must know; Antarkosh does not call an external country-code service.
