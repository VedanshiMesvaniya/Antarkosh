# Phase 14 - learning from usage

Real user phrasing ("QN", "supplier") is better evidence than a guess from column names. One accepted answer can still be a wrong
answer, so nothing is learned from a single success: a pair is **promoted only after repeated confirmations, can be undone, and lives in
its own `learned/` folder**, separate from the generated files.

## Flow

1. A logged-in user who owns the chat gives a thumbs up or down on a SQL answer. The rating is saved exactly as before; afterwards (in
   the background) one line is appended to `databases/<db_id>/learned/term_pair_evidence.jsonl`.
   Needs `auto_learning` (already on for registered databases). The line holds: the question's **phrases** (1-3 words, never the full
   question), the **tables and columns** the SQL used, the day, a hashed user, a hashed question. Answers with no rows, no SQL or
   unreadable SQL record nothing. Taking a rating back records a withdrawal.
2. `learned/term_pairs.json` is rebuilt from that ledger (a pure function of it). A phrase is promoted to a **table** when, over
   *every* confirmed answer that used the phrase: at least 3 distinct questions on at least 2 days used that table (`MIN_CONFIRMATIONS`,
   `MIN_DISTINCT_DAYS`), at least 80 % of the answers that used the phrase also used it, and thumbs-downs are at most 20 % of the votes.
   Thresholds are constants at the top of `src/sql/learning/term_pairs.py`.
3. With `auto_learning_apply` on (starts **off**), the prompt gets up to 5 extra lines under "Column mapping":
   `- "qn" -> quality_notification (learned from confirmed answers)`. They come after the curated/generated entries, never replace
   them, and are skipped if the table no longer exists in `schema/schema.json`. Table retrieval is not changed.

## What is deliberately not learned

* words the schema's own names already cover in that answer (a word satisfied by a column of the answer is never tied to another table);
* generic words, numbers, and anything that is a data value in the SQL;
* join-key and audit/common columns; phrases over three words;
* a phrase that goes equally well with two tables (a join companion, a twin table): nothing is promoted;
* columns (see Limits).

## Undo

* A user takes their rating back, or gives a thumbs-down: the pair drops below the threshold the next time the file is rebuilt.
* `python scripts/ops/term_pairs.py show --db-id <id>` lists promoted and pending pairs.
* `python scripts/ops/term_pairs.py revoke --db-id <id> "<phrase>" [--target table:<name>]` blocks a phrase; `restore` lifts it.
* Set `auto_learning_apply: false` to stop all use in prompts at once; delete `databases/<id>/learned/term_pair*` to forget everything.

## Isolation

`learned/` is git-ignored, excluded from the Docker image (set `LEARNED_DATA_DIR` to a persistent volume to keep it across deploys),
and is not in the generated-file list, so Sync Schema, kept versions and rollback never read, rewrite or delete it.

## Limits (known)

* Only tables are promoted; a column hint cannot be told apart from its table using co-occurrence alone.
* It needs real volume: a few users and a few weeks. On a small deployment most phrases stay "pending", which is the safe result.
* `MIN_DISTINCT_USERS` is 1; raise it once several real users rate answers.
* Hints reach the prompt only; they do not yet help table *retrieval* (that is a separate, riskier step).
* Evidence is recorded only while `auto_learning` is on for the database.
