# Catalog fixtures

`catalog_postgresql.json` holds the REAL rows returned by this project's PostgreSQL catalog queries
(`schema_query`, `fk_query`, `table_query`, `key_query`, `row_estimate_query`) against a real
PostgreSQL server, so the extractor tests run offline without one. The test database had composite
primary/unique/foreign keys, a quoted mixed-case table (`Order Items`), a view, a materialized view,
a partitioned table with a partition, an enum and array column, table/column comments, and a second
schema whose tables must NOT appear.

To regenerate: start any PostgreSQL, create the objects described above in the `public` schema, run
each query from `src/sql/dialects/postgresql.py` as `current_schema() = 'public'`, and save the rows
keyed by query name. The MySQL, SQL Server and Oracle rows used in tests are hand-written from the
catalog documentation, not recorded; replace them with real recordings once those servers are available.

`reflection_postgresql_multischema.json` holds the REAL output of this project's SQLAlchemy catalog reader
(`src/sql/sa/reflection.reflect_database`) against a real PostgreSQL as a read-only role: several schemas
(`sales`, `hr`, `public`, a quoted mixed-case one), a schema the role cannot USE (`locked`), an empty schema,
a table the role cannot SELECT (`sales.hidden_t`), a table name that exists in two schemas (`employee`),
cross-schema foreign keys, an enum type, a CHECK constraint, an index, comments, a view and a materialized view.
`all_schemas` is a run with no schema pinned, `pinned_sales` a run with the connection pinned to `sales`.
To regenerate, recreate those objects and call `reflect_database` through a SQLAlchemy async connection.


## relationships/

Five REAL catalogs for the Phase 6 relationship benchmark (`scripts/db/relationships_benchmark.py`,
`tests/sql/test_relationships_benchmark.py`): `<name>.schema.json` and `<name>.inventory.json` are the files this
project's own catalog phase (`run_catalog_phase`) wrote when run against a live server. Metadata only; no data rows.

| name | source (schema only) | server | style |
|---|---|---|---|
| pagila | devrimgunduz/pagila `pagila-schema.sql` | PostgreSQL 16 | snake_case, `_id` |
| northwind | pthom/northwind_psql `northwind.sql` | PostgreSQL 16 | snake_case, `_id` |
| chinook | lerocha/chinook-database `Chinook_PostgreSql.sql` | PostgreSQL 16 | PascalCase |
| sakila | jOOQ/sakila `mysql-sakila-schema.sql` | MariaDB 10.11 | snake_case, `_id` |
| employees | datacharmer/test_db `employees.sql` (tables only) | MariaDB 10.11 | `emp_no`, composite keys |

Changes needed to load them, none of which touch a table, column or key: Pagila's current master targets PostgreSQL 18, so
`uuidv7()` was replaced by `gen_random_uuid()`, `VIRTUAL` generated columns by `STORED`, and the pgvector column and
extension were dropped; its 49 monthly `payment_pYYYY_MM` partitions were removed from the fixture (the catalog does not mark
partitions, and each one repeats its parent's relationships). `generated_at` was removed so the files do not change on re-capture.

To add a database: load it into any server, run a sync with `auto_catalog` for it, copy `schema.json` and
`table_inventory.json` here as `<name>.schema.json` / `<name>.inventory.json`, then add its expected result to the benchmark test.
