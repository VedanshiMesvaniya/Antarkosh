"""Phase 6 / step 6b: the catalog graph and column-type families."""
# ruff: noqa: F811

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.sql.relationships.model import (
    Column,
    graph_from_documents,
    load_graph,
    type_family,
    types_compatible,
)
from tests.sql.test_catalog_extraction import (  # noqa: F401  (shared fixtures)
    _sync_args,
    dbs,
    sqlite_db,
)


def col(name="x", type_="INTEGER") -> Column:
    return Column(name, type_, type_family(type_))


# ================================================================= type families


@pytest.mark.parametrize(
    "raw,family",
    [
        ("INTEGER", "integer"), ("int", "integer"), ("BIGINT", "integer"), ("bigint unsigned", "integer"), ("SERIAL", "integer"),
        ("tinyint(1)", "integer"), ("NUMERIC(10, 2)", "numeric"), ("DECIMAL(18,2)", "numeric"), ("NUMBER(10)", "numeric"),
        ("MONEY", "numeric"), ("double precision", "numeric"), ("VARCHAR(50)", "string"), ("NVARCHAR(MAX)", "string"),
        ("character varying", "string"), ("TEXT", "string"), ("VARCHAR2(30 CHAR)", "string"), ("UNIQUEIDENTIFIER", "uuid"),
        ("uuid", "uuid"), ("bit", "bool"), ("BOOLEAN", "bool"), ("DATE", "date"), ("DATETIME2", "datetime"),
        ("TIMESTAMP WITHOUT TIME ZONE", "datetime"), ("timestamp with time zone", "datetime"), ("TIME", "time"),
        ("jsonb", "json"), ("BYTEA", "binary"), ("varbinary(max)", "binary"), ("geometry", "other"), ("", "other"), (None, "other"),
    ],
)
def test_type_families(raw, family):
    assert type_family(raw) == family


def test_which_types_can_reference_each_other():
    assert types_compatible(col(type_="INT"), col(type_="BIGINT"))
    assert types_compatible(col(type_="INTEGER"), col(type_="NUMBER(10)"))  # Oracle NUMBER is used for ids
    assert types_compatible(col(type_="VARCHAR(10)"), col(type_="TEXT"))
    assert not types_compatible(col(type_="INTEGER"), col(type_="VARCHAR(10)"))
    assert not types_compatible(col(type_="UUID"), col(type_="VARCHAR(36)"))
    assert not types_compatible(col(type_="DATE"), col(type_="DATETIME2"))
    assert types_compatible(col(type_="geometry"), col(type_="GEOMETRY"))  # unknown types match only identical text
    assert not types_compatible(col(type_="geometry"), col(type_="polygon"))
    assert not types_compatible(col(type_=""), col(type_=""))


# ================================================================= the graph

SCHEMA = {
    "engine": "postgresql",
    "default_schema": "public",
    "multi_schema": True,
    "generated_at": "2026-01-01T00:00:00",
    "tables": [
        {"name": "customer", "schema": "sales", "qualified_name": "sales.customer", "kind": "table", "primary_key": ["id"],
         "unique_keys": [["email"]], "indexes": [{"name": "ix_name", "columns": ["name", "tier"], "unique": False}],
         "comment": "People who buy", "columns": [
             {"name": "id", "type": "INTEGER", "nullable": False, "is_primary_key": True},
             {"name": "email", "type": "VARCHAR(100)"}, {"name": "name", "type": "TEXT"}, {"name": "tier", "type": "TEXT"}]},
        {"name": "orders", "schema": "sales", "qualified_name": "sales.orders", "kind": "table", "primary_key": ["id"],
         "indexes": [{"name": "ix_cust", "columns": ["customer_id"], "unique": False}, {"name": "ux_ref", "columns": ["ref"], "unique": True}],
         "columns": [
             {"name": "id", "type": "INTEGER", "nullable": False, "is_primary_key": True},
             {"name": "customer_id", "type": "INTEGER", "is_foreign_key": True, "comment": "Reference to the customer"},
             {"name": "ref", "type": "TEXT"}]},
        {"name": "order_items", "schema": "sales", "qualified_name": "sales.order_items", "kind": "table", "primary_key": ["order_id", "line_no"],
         "columns": [{"name": "order_id", "type": "INTEGER"}, {"name": "line_no", "type": "INTEGER"}]},
        {"name": "v_orders", "schema": "sales", "qualified_name": "sales.v_orders", "kind": "view", "is_view": True,
         "columns": [{"name": "id", "type": "INTEGER"}]},
        {"name": "orders", "schema": "archive", "qualified_name": "archive.orders", "kind": "table", "columns": [{"name": "id", "type": "INTEGER"}]},
    ],
    "relationships": [
        {"from_schema": "sales", "from_table": "orders", "from_column": "customer_id", "to_schema": "sales", "to_table": "customer", "to_column": "id", "type": "fk"},
        {"from_schema": "sales", "from_table": "order_items", "from_column": "order_id", "to_schema": "sales", "to_table": "orders", "to_column": "id", "type": "fk"},
        {"from_schema": "sales", "from_table": "ghost", "from_column": "x", "to_schema": "sales", "to_table": "orders", "to_column": "id", "type": "fk"},
    ],
}
INVENTORY = {
    "answering_scope": {"schema": "sales", "tables_outside_scope": 1},
    "tables": [{"qualified_name": "sales.orders", "row_estimate": 1200}, {"qualified_name": "sales.customer", "row_estimate": None}],
}
EVIDENCE = {"join_edges": [
    {"a": {"table": "sales.order_items", "column": "order_id"}, "b": {"table": "sales.orders", "column": "id"}, "statements": 4, "routines": 3, "confidence": "high", "declared_fk": True},
    {"a": {"table": "sales.orders", "column": "customer_id"}, "b": {"table": "sales.customer", "column": "id"}, "statements": 1, "routines": 1, "confidence": "low", "declared_fk": False},
    {"a": {"table": "sales.orders", "column": "nope"}, "b": {"table": "sales.customer", "column": "id"}, "statements": 1, "routines": 1, "confidence": "low", "declared_fk": False},
]}


def graph():
    return graph_from_documents(SCHEMA, INVENTORY, EVIDENCE, db_id="d")


def test_tables_columns_keys_indexes_and_row_estimates_are_read():
    g = graph()
    cust, orders = g.table("sales.customer"), g.table("sales.orders")
    assert (cust.schema, cust.kind, cust.comment, cust.primary_key, cust.unique_keys) == ("sales", "table", "People who buy", ("id",), (("email",),))
    assert cust.column("EMAIL").family == "string" and cust.column("id").nullable is False and cust.column("nope") is None
    assert orders.row_estimate == 1200 and cust.row_estimate is None
    assert orders.column("customer_id").is_foreign_key and orders.column("customer_id").comment == "Reference to the customer"
    assert g.engine == "postgresql" and g.default_schema == "public" and g.multi_schema is True


def test_how_strongly_a_column_identifies_a_row_and_whether_it_is_indexed():
    g = graph()
    cust, orders, items = g.table("sales.customer"), g.table("sales.orders"), g.table("sales.order_items")
    assert cust.key_strength("id") == "primary" and cust.key_strength("EMAIL") == "unique"
    assert orders.key_strength("ref") == "unique_index" and orders.key_strength("customer_id") is None
    assert items.key_strength("order_id") is None  # half of a composite key does not identify a row
    assert orders.is_indexed("customer_id") and orders.is_indexed("id") and not orders.is_indexed("ref") is False
    assert cust.is_indexed("name") and not cust.is_indexed("tier")  # only the FIRST column of an index counts
    assert items.is_indexed("order_id") and not items.is_indexed("line_no")


def test_views_are_marked_and_table_lookup_by_qualified_and_unique_bare_name():
    g = graph()
    assert g.table("sales.v_orders").is_view and not g.table("sales.orders").is_view
    assert g.table("SALES.Customer").qualified == "sales.customer" and g.table("customer").qualified == "sales.customer"
    assert g.table("orders") is None  # two schemas have an "orders" table: a bare name is not enough
    assert g.table("archive.orders").qualified == "archive.orders" and g.table("nope") is None
    assert g.name_collisions() == {"orders": ["archive.orders", "sales.orders"]}


def test_declared_foreign_keys_are_resolved_and_unresolvable_ones_are_counted_not_fatal():
    g = graph()
    assert [(f.from_table, f.from_column, f.to_table, f.to_column) for f in g.declared_fks] == [
        ("sales.orders", "customer_id", "sales.customer", "id"), ("sales.order_items", "order_id", "sales.orders", "id"),
    ]
    assert any("1 declared foreign keys" in w for w in g.warnings)


def test_procedure_edges_are_kept_only_when_both_columns_exist():
    g = graph()
    assert [(e.a_table, e.b_table, e.routines, e.confidence, e.declared_fk) for e in g.proc_edges] == [
        ("sales.order_items", "sales.orders", 3, "high", True), ("sales.orders", "sales.customer", 1, "low", False),
    ]


def test_the_answering_schema_is_the_only_schema_the_prompts_can_see():
    g = graph()
    assert g.scope_known and g.answering_schema == "sales" and g.tables_outside_scope == 1
    assert g.in_answering_scope(g.table("sales.orders")) and not g.in_answering_scope(g.table("archive.orders"))
    unknown = graph_from_documents(SCHEMA, None, None)
    assert unknown.scope_known is False and unknown.answering_schema is None  # nobody said which schema prompts see


def test_the_input_hash_ignores_timestamps_but_notices_real_changes():
    base = graph().inputs_hash
    again = json.loads(json.dumps(SCHEMA))
    again["generated_at"] = "2030-12-31T23:59:59"
    assert graph_from_documents(again, INVENTORY, EVIDENCE).inputs_hash == base
    changed = json.loads(json.dumps(SCHEMA))
    changed["tables"][0]["columns"].append({"name": "extra", "type": "TEXT"})
    assert graph_from_documents(changed, INVENTORY, EVIDENCE).inputs_hash != base
    assert graph_from_documents(SCHEMA, INVENTORY, None).inputs_hash != base  # the procedure evidence is an input too


def test_an_empty_schema_document_gives_no_graph():
    assert graph_from_documents({"tables": []}) is None and graph_from_documents({}) is None


# ================================================================= loading from disk


def _put(dbs: Path, name: str, doc) -> None:
    (dbs / "pg_live/schema" / name).write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


def test_load_graph_reads_the_three_files_and_works_without_the_optional_ones(dbs: Path):
    _put(dbs, "schema.json", SCHEMA)
    only_schema = load_graph("pg_live", dbs)
    assert only_schema is not None and only_schema.proc_edges == [] and only_schema.scope_known is False
    _put(dbs, "table_inventory.json", INVENTORY)
    _put(dbs, "sp_evidence.json", EVIDENCE)
    full = load_graph("pg_live", dbs)
    assert full.scope_known and len(full.proc_edges) == 2 and full.table("sales.orders").row_estimate == 1200


def test_no_usable_schema_json_means_nothing_to_infer_from(dbs: Path):
    assert load_graph("pg_live", dbs) is None  # the template's schema.json has no tables
    _put(dbs, "schema.json", "{not json")
    assert load_graph("pg_live", dbs) is None
    _put(dbs, "schema.json", "[1, 2]")
    assert load_graph("pg_live", dbs) is None
    assert load_graph("no_such_db", dbs) is None


def test_load_graph_validates_the_database_id(dbs: Path):
    with pytest.raises(ValueError):
        load_graph("../escape", dbs)


@pytest.mark.asyncio
async def test_a_catalog_generated_by_the_real_sync_loads_into_a_graph(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_catalog")
    assert (await sync_live_schema(db_id="pg_live", **_sync_args()))["status"] == "ok"
    g = load_graph("pg_live", dbs)
    assert g is not None and set(g.tables) >= {"customer", "orders", "order_items", "v_orders"}
    assert g.table("v_orders").is_view and g.table("order_items").primary_key == ("order_id", "line_no")
    # The native catalog path records primary and foreign keys only (no unique keys or indexes); rules must not assume them.
    assert g.table("customer").key_strength("id") == "primary" and g.table("customer").unique_keys == () and g.table("customer").indexes == ()
    assert {(f.from_table, f.from_column, f.to_table, f.to_column) for f in g.declared_fks} == {
        ("orders", "customer_id", "customer", "id"), ("order_items", "order_id", "orders", "id"),
    }
    assert g.table("orders").column("total").family == "numeric"
