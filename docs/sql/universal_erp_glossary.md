# Universal ERP glossary and local concept mapping

Flags: the universal glossary widens retrieval with `auto_retrieval_apply` (Release 1, unchanged flag). The local concept mapping and
`semantic_review.json` run only with `auto_semantic_glossary_apply` (per database, starts **false**; needs `auto_column_meaning` and
`auto_prompt_inject`). With it off, the glossary files and every prompt are exactly as before. The master prompt this follows is in
`docs/sql/erp_semantic_compiler_prompt.md`.

## The four outputs

| Master prompt output | Where | Notes |
| --- | --- | --- |
| `universal_erp_glossary.json` | `config/universal_erp_glossary.json` (global, hand-reviewed data) | ~300 concepts over domains A to K; loaded and validated by `src/sql/semantics/universal.py`. A file that breaks a rule is not used at all. |
| `column_glossary.json` | `databases/<db>/semantics/column_glossary.json` | Existing generated entries stay as they are. With the flag, concept-linked entries are added with `concept_id`, `aliases_seen`, `mappings` (table, column, role, confidence, evidence, safe_for_prompt), `confidence`, `safe_for_prompt`. High confidence only. Legacy keys (`maps_to`, `note`, `synonyms`, `auto`) are unchanged, so every existing reader works. |
| `routing_hints.json` | unchanged (Phase 9d) | Local only; never sent to a model. Still generated from the small synonym lexicon and the database's own names. |
| `semantic_review.json` | `databases/<db>/semantics/semantic_review.json` | New. Uncertain mappings, name-only readings, ambiguous aliases, stored codes without two independent signals. Read by nobody at question time; never blocks anything. |

## The universal glossary

One entry per concept: `concept_id`, `label`, `domain`, `aliases` (strong, label included), `conditional_aliases`,
`do_not_equate_with`, `kind` (`entity document measure attribute event process role period query_intent state`).

* Strong aliases widen retrieval always. Conditional aliases count only when the database's own table, column or comment words use
  them ("item" widens to "material" only in a database that has material columns). Concepts in `do_not_equate_with` are never merged.
* Query-intent words (count, top, monthly ...) and state words (open, cancelled ...) are matched but never expanded and are never
  evidence for a table, a column, a join or a code value.
* Validator rules (`validate_document`, run on every load): lower-case plain business phrases only (no underscores, digits, symbols or
  single letters), no SQL or schema words, no code or interface words, no ambiguous abbreviations (`pi dc po so rm`), `sku` only as a
  conditional alias, strong aliases unique, do-not-equate lists real and symmetric and never sharing a word, intent words never
  entity aliases, states without aliases.
* To add a concept: append it to the JSON and run `pytest tests/sql/test_universal_glossary.py`.

## Local mapping (per database, metadata only)

Built from the column meaning cards the earlier phase already generated and checked, plus the catalog's structure. No new model call,
no data row.

* **Role**: `identifier` for a primary key, `foreign_key` for a declared or flagged foreign key, otherwise from the words left of the
  concept (name, number, amount, date, status). A role the column type cannot hold is refused.
* **Confidence**: `high` needs a high-confidence card AND a catalog comment, procedure/view evidence or key structure. A name alone is
  never high; it goes to the review file. Only high mappings become entries; nothing weaker reaches a prompt or a route.
* **Ambiguity**: a column naming two concepts the glossary keeps apart, or a concept spread over more than 3 tables, is not mapped.
* **Codes**: a phrase that is only a code (`y`, `0`, `+`) never becomes a synonym. A stored code's meaning is "supported" only with two
  independent metadata signals (for example a procedure's CASE plus a column comment or a lookup-table foreign key); otherwise
  "unknown" and unused. Code meanings are reported in the review file only; they are not injected into prompts.
* **Prompt side**: an entry with `safe_for_prompt: false` is never shown or used to route. The prompt still receives only the glossary
  entries matching the question (at most 15 lines), never the whole glossary.

## Master prompt sections and where they live

| Section | Status |
| --- | --- |
| Global glossary rules and coverage A to K, concept separations | New: `config/universal_erp_glossary.json` plus validator and tests that check every listed concept and every listed separation. |
| Local glossary rules (shape, confidence, evidence, safe_for_prompt) | New: `concepts.py`, `wiring.py` (additive fields). |
| Code/value rules | New guard: code phrases never synonyms; review status per the two-signal rule. |
| Soft-delete rules | Already in place, unchanged: per database and per table (`soft_delete_spec.py`: curated file, atlas, `db.yaml`, catalog discovery by name and type), three intents (active-only default, deleted-only, include-archived for audit/history). A char-flag marker found by column name is read as `N` = live; override with `semantics/soft_delete.json` where a database differs. |
| Relationship rules, boundary notice, NO_SQL | Already in place (Release 3): approved joins only, `No approved route connects these groups: ... Do not join across groups.`, NO_SQL. |
| LLM prompt rules (what is and is not sent) | Already in place: selected tables, matching glossary entries (capped), approved joins among selected tables, soft-delete conditions; never all tables, the whole glossary, full relationships, procedures, credentials or rows. |
| Dialect rules | Dialects stay out of both glossaries (validator rejects SQL words); the engine is used only for SQL generation and validation. |
| Automation rules 1 to 10 | Existing sync phases generate inventory, profiles, relationships, join paths, meanings and hints; resync refreshes on schema change; learned feedback lives in `learned/` and is never overwritten; no manual mapping is needed. |
