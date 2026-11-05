# Phase 9d - routing hints and synonym glossary from the sync, and the ERP rulebook kept out of other databases

## Why

`semantics/glossary.json` and `semantics/routing_hints.json` were empty seeds copied from the template. No sync phase filled them
for any database except the hand-written ERP one, so a new database always had empty routing and an empty synonym glossary.

## What a sync now does (step "routing_hints", after column meaning)

Reads only `schema/schema.json`, `table_inventory.json` and `table_profiles.json` (no database connection, no model call), then:

* finds the generic synonym groups (`src/sql/semantics/lexicon.py`: customer / client / buyer, vendor / supplier / seller ...) for
  which **exactly one word** appears in the database's own table or column names;
* writes that group to `glossary.json` (`"vendor": ["supplier", "seller", ...]`; the prompt shows it as `- vendor: supplier, seller`);
* writes `anchor_rules` and `fallback_rules` to `routing_hints.json`: the other words of the group are the keywords, the tables whose
  names contain the schema's word are the tables. A keyword never repeats a word the schema's names already contain.

Safety rules (each one tested): a group whose words both appear in the names is skipped; a concept matching more than 4 tables gets
no anchor rule (more than 8: no rule at all); test/backup/temp tables, look-alike copies, views and tables outside the answering
schema never appear; table names are written lower-case and bare, and only when the retrieval code can look them up (no spaces,
`$`, `#`, `-`); keywords are at least 4 letters; at most 60 rules.

Nothing a person wrote is touched. A rule without `"auto": "auto_routing_hints"` is yours; a keyword one of your rules already uses
is left to your rule; a glossary group is generated only while it is exactly what the sync wrote (recorded in
`semantics/auto_hints_state.json`), so an edited group becomes yours. Every sync rebuilds the generated part, so a dropped table
removes its rules. `erp_main` is never touched.

Switch: it follows `auto_prompt_inject`. With that flag off the generated part is removed again (like the generated column glossary).
It needs the catalog (`auto_catalog`); without a catalog the step reports `skipped`. Its failure never fails the sync and does not make
the sync record "untrusted". Review: open the two files, or read `routing_hints` in the sync report.

## The ERP rulebook in the generic prompt

`get_scoped_readability_rules` carried the ERP database's own rules (`financial_year.current_year`, `production.qty`, `purchase.pi_no`,
`'Y'/'N'` status flags, "CHANGABLE PACK" ...) and fired many of them from everyday words ("order", "number", "latest", "report"),
for every database. It now takes the database id and uses the gate `_erp_rule` already used: for a non-ERP database **in apply mode**
(`auto_prompt_inject` on) a rule needs its tables, not a keyword; the always-on ERP lines are dropped or made neutral; the
"Extracted Business Intent" temporal lines stop naming a `financial_year` table the database does not have.
`erp_main`, and every database not in apply mode, get byte-identical prompts (checked over every mode and a matrix of questions).

## Known limits

* The synonym lexicon is generic English business vocabulary; a database whose words are not in it gets no generated groups.
* Table names with spaces, `$`, `#` or `-` are still not handled by older retrieval helpers; they are left out of routing rules.
* A bigger step, not taken: move the ERP rulebook into a per-database knowledge file so other databases can carry their own rules.
