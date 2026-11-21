# Release 3: the connected schema contract

Flag: `auto_schema_contract_apply` (per database, starts **false**). It needs the Phase 12 flags in apply mode
(`auto_prompt_inject`), the global `schema_compaction_enabled` switch, and approved joins in `relationships.json`. With any of those
missing, retrieval and the prompt are exactly as before. With the Phase 12 flags on but this flag off, it only logs
(`schema_contract_shadow`) what it would drop.

## What it does (flag on)

Built only from what the database already approved: `relationships.json` and the routes of `join_paths.json` that retrieval turned
into bridge tables. No data, no model call.

1. **Direct approved joins and bridges** are unchanged: `relationships.json` (scoped to the shown tables) and route bridges from
   `join_paths.json`, added whole or not at all, capped at 4.
2. **Drop weak, disconnected matches** (before the token budget is spent). The tables retrieval chose form groups connected by
   approved joins or routes. The group with the most relevant table is the main one. Another group is dropped unless one of its
   tables is there on purpose: a glossary/domain anchor, a bridge, the real table of a look-alike copy, or one of the question's
   top two vector matches (`KEEP_TOP_VECTOR`). The first table is never dropped.
3. **One line for what stays unconnected** (after the budget, on what is really shown): `No approved route connects these groups:
   (a, b) | (c). Do not join across groups.` A route cut by the budget is not a route.
4. **Unique keys** (single-column, not primary) are marked `UQ` next to `PK` and `FK->` in the compact table lines.
5. **The prompt** gets a `Schema contract:` block: use only the listed tables, columns and approved joins; do not invent tables,
   columns or joins; if no approved route is present, do not guess (answer from the connected tables, or `NO_SQL`). The whole-database
   relationship map is no longer offered in place of the joins among the shown tables; when several tables are shown and none is
   joined, the prompt says so.

Unchanged and still enforced after generation: read-only checks, real tables and columns, dialect, soft-delete enforcement, join
validation and repair.

## Safety

* A database with no approved relationship cannot be judged: nothing is dropped, the old wording stays.
* The note never contains `table <name>` and never starts with `word:`, the shapes the schema parsers read as tables.
* Every extra is wrapped: on any error retrieval and the prompt are what they were. `erp_main` is never affected.
* Size: the schema stays under the existing token budget; the note is at most 6 groups of 6 names; the grounding block is about 100
  tokens; relationships shown are only those among the shown tables.

## Turning it on and checking it

Set `auto_schema_contract_apply: true` for the database. First watch `schema_contract_shadow` records in telemetry (`dropped`,
`groups`, `approved_edges`); after turning it on, `schema_contract_applied` and `schema_compaction_applied.contract_note`. To undo,
set the flag back to `false`.

## Not part of this release

The repair stage can still show "unverified" inferred links (`repair/advisory_links.py`) after a failed or empty query. They are
labelled as unverified and never reach the first prompt; remove them separately if you want the repair prompt to be approved-only.
