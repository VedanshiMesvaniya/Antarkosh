# Soft-deleted rows: never returned, in every dialect

Until now only a column literally named `deleted_at`, on the tables the behavioral atlas lists, was filtered out of generated SQL.
A database that marks deleted rows another way (`IsDeleted bit`, `is_deleted char(1)` with Y/N, `DeletedOn`...) was never filtered, and
a database with no atlas (every database but `erp_main`) was not filtered at all. `auto_soft_delete_apply` fixes that for any database.

## How the marker column is found

For each table, strongest source first:

1. `semantics/soft_delete.json` (optional, yours): `{"tables": {"orders": {"column": "is_removed", "kind": "char_flag"}}, "ignore": ["audit_log"]}`.
   `ignore` removes a table from everything below; `tables` forces a column. Kinds: `timestamp`, `boolean`, `int_flag`, `char_flag`.
2. The behavioral atlas: a table with a `deleted_at` column (as before).
3. `soft_delete.column` in `db.yaml` (the template declares `deleted_at`).
4. Discovery from the catalog (`schema/schema.json`, written by `auto_catalog`), by column name AND type together:

| Looks like | Kind | "Not deleted" condition |
|---|---|---|
| `deleted_at`, `DeletedOn`, `deletion_date`... on a nullable date/time | `timestamp` | `col IS NULL` |
| `is_deleted`, `deleted` on a native boolean (PostgreSQL, SQLite) | `boolean` | `(col = FALSE OR col IS NULL)` |
| `is_deleted`, `IsDeleted`, `del_flag` on bit / tinyint / int / NUMBER | `int_flag` | `(col = 0 OR col IS NULL)` |
| a flag on `char(1)`, an `enum('Y','N')`, or a varchar defaulting to `'N'` | `char_flag` | `(col IN ('N', 'n') OR col IS NULL)` |

Never taken for a marker: a key column, `DeletedBy`, `deleted_count`, a `NOT NULL` `deleted_at`, a text/int `deleted_at`, a bare `deleted`
on a plain integer, a varchar flag with no evidence. When a table has several candidates, one is used (timestamp first) and
`scripts/db/show_soft_delete.py` says which were skipped.

## What it does to a query

* **The SQL the model writes is corrected after the fact** (the guarantee), in MySQL, PostgreSQL, SQL Server, Oracle and SQLite:
  the condition is added for every table with a marker, in the dialect's own quoting. A condition the model already wrote is
  recognised and not repeated. An OR in the model's filter is parenthesised first, so a deleted row cannot slip through it.
* **A LEFT JOINed table's condition goes in its ON clause**, so a deleted match becomes "no match" instead of removing the other
  table's row. Other joins use WHERE.
* **"Deleted only"** questions get the opposite condition (and a contradicting one the model wrote is swapped); **history/audit**
  questions are left alone.
* **The prompt names the real columns** of the tables it shows, so the model stops guessing `deleted_at` where the column is `IsDeleted`.

## Turning it on

1. `python scripts/db/show_soft_delete.py --db-id <id>` lists the tables and columns that would be filtered. Put any table that must
   not be filtered under `ignore`. Add `--sql "SELECT ..."` to preview the rewrite.
2. Shadow first: with `auto_prompt_shadow` or `auto_prompt_inject` on, every query logs a `soft_delete_shadow` event (`would_add`)
   and nothing changes.
3. Set `auto_soft_delete_apply: true` for the database in `config/feature_flags.yaml`. Set it back to `false` to undo.

`erp_main` is pinned and keeps its existing behaviour. With the flag off nothing changes for any database, except one fix that applies
everywhere: the old `deleted_at` path no longer lets an OR in the model's WHERE swallow the condition.

## Limits

* Needs `schema/schema.json` (`auto_catalog`) or an atlas/`db.yaml`/curated entry; without them there is nothing to go on.
* Names are matched by a fixed list; an unusual marker name goes in `semantics/soft_delete.json`.
* A RIGHT/FULL JOIN puts the condition in WHERE (it still never returns deleted rows).
* Table names containing a space, hyphen, `$` or `#` are not handled by the older table-name helpers in several places.

## Same in every dialect

| | SQLite | MySQL | PostgreSQL | SQL Server | Oracle |
|---|---|---|---|---|---|
| Catalog, key, row-estimate, routine queries | yes (no row estimates, no routines) | yes | yes | yes | yes |
| sqlglot name used to rewrite SQL | `sqlite` | `mysql` | `postgres` | `tsql` | `oracle` |
| Native type names compact sensibly | yes | yes | yes | yes | yes |
| Soft-delete conditions valid SQL | yes | yes | yes | yes | yes |
| Soft-delete meaning checked by running it | yes | syntax only | syntax only | syntax only | syntax only |

Only SQLite could be run here, so MySQL, PostgreSQL, SQL Server and Oracle are checked by parsing what is written in each dialect.
