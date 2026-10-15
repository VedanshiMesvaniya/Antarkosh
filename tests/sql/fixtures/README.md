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
