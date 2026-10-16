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
