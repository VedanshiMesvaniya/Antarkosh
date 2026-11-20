# Release 1: metadata retrieval (extra query words and column-level table matches)

Flag: `auto_retrieval_apply` (per database, starts **false**). With it off, retrieval is exactly as before.

## What it does (flag on)

1. **Extra query words.** If the question contains a word from `lexicon.QUERY_ALIASES` (`item` -> product, material; `facility` ->
   plant, site; `purchase value` -> purchase amount) or a synonym listed in the database's own `semantics/glossary.json`, those
   words are appended to the text the question is **searched** with (embedding and hybrid search). The question itself, the prompt
   and the SQL are not changed.
2. **Column-level matches.** `src/sql/retrieval_terms.py` builds, per database, one small record per column from
   `schema/schema.json` (name words, catalog comment), `semantics/column_dictionary.json` (generated meanings, phrases) and
   `semantics/column_glossary.json` (synonyms). A table is added when one of its columns uses words of the question that few other
   tables use (a word counts `1 / tables-that-use-it`; words used by more than 6 tables are ignored). At most 3 tables are added,
   after the vector and anchor tables, and the existing token budget still decides what the prompt shows.

## Safety

* Metadata only. No row, sample value or query result is read, embedded or sent.
* Per database: every file is read from `databases/<db_id>/`; nothing from another database can appear. The vector search keeps its
  `db_id` filter and its post-filter.
* No new vector points and no new chunk type: document search and sync cost are untouched.
* Only real tables (not views, look-alike copies, test/backup/temp tables, or tables whose bare name two schemas share).
* Missing or unreadable files mean no extra help, never an error. `erp_main` is always off.

## Turning it on and checking it

Set `auto_retrieval_apply: true` for the database in `config/feature_flags.yaml`, ask a question that uses an everyday word, and read
the `retrieval_terms_applied` telemetry record: `extra_terms` (the added words), `added` (tables) and `matches` (table, column, score,
words). To undo, set the flag back to `false`.

## Already in place (not part of this release)

* Table chunks carry `db_id` and are embedded with their column meanings (Phase 9c).
* Changed tables are re-embedded on every sync (stable chunk ids); removed tables lose their chunk when `auto_stale_chunk_cleanup` is on.
* The schema token budget (`DEFAULT_SCHEMA_TOKEN_BUDGET`) bounds the prompt.
