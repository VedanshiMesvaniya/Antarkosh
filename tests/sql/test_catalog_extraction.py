"""Phase 3: catalog extraction, table inventory, consistency gate.

Offline. PostgreSQL rows are REAL, recorded from a live server (tests/sql/fixtures/README.md).
SQLite runs for real against a temp file. MySQL, SQL Server and Oracle rows are hand-written from
the catalog documentation and the SQL there is only checked for shape, not run on a live server.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlglot
from sqlglot import exp

from src.core.config import settings
from src.sql import registry
from src.sql.dialects import DIALECTS, get_dialect_profile
from src.sql.introspection import catalog as cat_mod
from src.sql.introspection.catalog import (
    GENERATED_BY,
    build_catalog,
    check_consistency,
    inventory_document,
    run_catalog_phase,
    schema_document,
    write_catalog_files,
)
from src.sql.safety.sql_safety import check_cartesian_explosion

ROOT = Path(__file__).resolve().parents[2]
PG = json.loads((Path(__file__).parent / "fixtures" / "catalog_postgresql.json").read_text(encoding="utf-8"))

EXPECTED_ALIASES = {
    "column_query": ["table_name", "column_name", "data_type", "column_comment"],
    "table_query": ["table_name", "table_type", "table_comment"],
    "key_query": ["table_name", "constraint_type", "constraint_name", "column_name", "ordinal_position"],
    "row_estimate_query": ["table_name", "row_estimate"],
}
CATALOG_QUERIES = [
    (engine, name)
    for engine, profile in DIALECTS.items()
    for name in EXPECTED_ALIASES
    if getattr(profile, name)
]
# The only places a catalog query may read from.
_CATALOG_SCHEMAS = {"information_schema", "pg_catalog", "sys"}
_CATALOG_TABLES = {"sqlite_master", "pragma_table_info"}


def _pg_catalog():
    return build_catalog(
        "pg_live",
        DIALECTS["postgresql"],
        column_rows=PG["schema_query"],
        fk_rows=PG["fk_query"],
        table_rows=PG["table_query"],
        key_rows=PG["key_query"],
        estimate_rows=PG["row_estimate_query"],
    )


# ------------------------------------------------------------------ the SQL itself


@pytest.mark.parametrize("engine,name", CATALOG_QUERIES)
def test_catalog_query_is_one_read_only_catalog_select(engine, name):
    p = get_dialect_profile(engine)
    sql = getattr(p, name)
    tree = sqlglot.parse_one(sql, read=p.sqlglot_dialect)

    assert len(sqlglot.parse(sql, read=p.sqlglot_dialect)) == 1 and isinstance(tree, exp.Select)
    assert [e.alias_or_name for e in tree.expressions] == EXPECTED_ALIASES[name]
    assert not list(tree.find_all((exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Command)))
    assert not list(tree.find_all(exp.Count)), "a catalog query must never count data rows"
    assert check_cartesian_explosion(sql, dialect=p.sqlglot_dialect)[0] is False  # not clamped to 100 rows

    for table in tree.find_all(exp.Table):
        schema = (table.args.get("db").name if table.args.get("db") else "").lower()
        # sqlglot represents a table-valued function such as pragma_table_info(...) as a Table around a function node
        ident = (table.name or (table.this.name if isinstance(table.this, exp.Anonymous) else "")).lower()
        assert schema in _CATALOG_SCHEMAS or ident in _CATALOG_TABLES or ident.startswith("all_"), (
            f"{engine}.{name} reads from non-catalog object {table.sql()}"
        )


@pytest.mark.parametrize("engine,name", CATALOG_QUERIES)
def test_catalog_query_survives_the_row_cap_rewrite(engine, name):
    p = get_dialect_profile(engine)
    tree = sqlglot.parse_one(getattr(p, name), read=p.sqlglot_dialect)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(20000)))
    sqlglot.parse_one(tree.sql(dialect=p.sqlglot_dialect), read=p.sqlglot_dialect)


@pytest.mark.parametrize("engine", ["mssql", "oracle"])
def test_no_trailing_semicolon_where_the_driver_rejects_one(engine):
    p = get_dialect_profile(engine)
    for name in EXPECTED_ALIASES:
        assert not getattr(p, name).strip().endswith(";")


def test_profiles_without_the_new_fields_still_construct():
    from src.sql.dialects.base import SQLDialectProfile

    p = SQLDialectProfile(key="x", name="X", sqlglot_dialect="mysql", schema_query="", date_functions="", fk_query="")
    assert p.table_query == p.key_query == p.row_estimate_query == p.column_query == ""


# ------------------------------------------------------------------ real PostgreSQL rows


def test_real_postgres_rows_build_the_expected_catalog():
    c = _pg_catalog()
    assert len(c.tables) == 9 and c.warnings == [] and c.unavailable == []
    assert "mv" not in c.tables and "hidden" not in c.tables  # matview and the other schema stay out
    assert c.tables["v_orders"].kind == "view" and c.tables["customer"].kind == "table"
    assert c.tables["Order Items"].primary_key == ["order_id", "line_no"]  # mixed-case name, composite order
    assert c.tables["composite_parent"].unique_keys == [["a", "b"]]
    assert c.tables["customer"].primary_key == ["id"] and c.tables["customer"].unique_keys == [["email"]]
    assert c.tables["customer"].comment == "People who buy"
    assert {x["name"]: x["comment"] for x in c.tables["customer"].columns}["email"] == "Login e-mail"
    assert c.tables["customer"].row_estimate == 500 and c.tables["v_orders"].row_estimate is None
    assert len(c.foreign_keys) == 4
    assert {"from_table": "composite_child", "from_column": "b", "to_table": "composite_parent", "to_column": "b"} in c.foreign_keys


def test_real_postgres_documents_pass_the_consistency_gate():
    c = _pg_catalog()
    sd, inv = schema_document(c), inventory_document(c)
    chunks = {r["table_name"] for r in PG["schema_query"]}
    report = check_consistency(c, sd, inv, chunks)
    assert report == {"ok": True, "problems": [], "counts": {"catalog": 9, "inventory": 9, "schema.json": 9, "schema chunks": 9}}
    assert inv["counts"] == {"tables": 8, "views": 1, "total": 9}
    flags = {col["name"]: (col["is_primary_key"], col["is_foreign_key"]) for t in sd["tables"] if t["name"] == "orders" for col in t["columns"]}
    assert flags == {"id": (True, False), "customer_id": (False, True), "total": (False, False)}


def test_schema_document_has_every_key_the_curated_erp_main_file_has():
    """New-database schema.json must stay readable by code written against erp_main's file."""
    erp = json.loads((ROOT / "databases" / "erp_main" / "schema" / "schema.json").read_text(encoding="utf-8"))
    ours = schema_document(_pg_catalog())
    assert set(erp) <= set(ours)
    assert set(erp["tables"][0]) <= set(ours["tables"][0])
    assert set(erp["tables"][0]["columns"][0]) <= set(ours["tables"][0]["columns"][0])
    assert set(erp["relationships"][0]) <= set(ours["relationships"][0])
    assert isinstance(ours["domains"], dict)


# ------------------------------------------------------------------ other engines (hand-written rows)


def test_mysql_uppercase_result_keys_and_view_comment():
    c = build_catalog(
        "shop",
        DIALECTS["mysql"],
        column_rows=[{"TABLE_NAME": "orders", "COLUMN_NAME": "id", "DATA_TYPE": "int", "COLUMN_COMMENT": ""}],
        table_rows=[
            {"TABLE_NAME": "orders", "TABLE_TYPE": "BASE TABLE", "TABLE_COMMENT": "Customer orders"},
            {"TABLE_NAME": "v_orders", "TABLE_TYPE": "VIEW", "TABLE_COMMENT": "VIEW"},
        ],
        key_rows=[{"TABLE_NAME": "orders", "CONSTRAINT_TYPE": "PRIMARY KEY", "CONSTRAINT_NAME": "PRIMARY", "COLUMN_NAME": "id", "ORDINAL_POSITION": 1}],
        estimate_rows=[{"TABLE_NAME": "orders", "TABLE_ROWS": None, "ROW_ESTIMATE": 1200}],
    )
    assert c.tables["orders"].comment == "Customer orders" and c.tables["orders"].primary_key == ["id"]
    assert c.tables["orders"].row_estimate == 1200
    assert c.tables["v_orders"].kind == "view" and c.tables["v_orders"].comment is None  # the literal word VIEW dropped


def test_oracle_uppercase_names_are_kept_and_unknown_statistics_are_none():
    c = build_catalog(
        "hr",
        DIALECTS["oracle"],
        column_rows=[{"table_name": "EMPLOYEES", "column_name": "EMPLOYEE_ID", "data_type": "NUMBER", "column_comment": "Primary key"}],
        table_rows=[{"table_name": "EMPLOYEES", "table_type": "TABLE", "table_comment": None}],
        key_rows=[{"table_name": "EMPLOYEES", "constraint_type": "PRIMARY KEY", "constraint_name": "EMP_PK", "column_name": "EMPLOYEE_ID", "ordinal_position": 1}],
        estimate_rows=[{"table_name": "EMPLOYEES", "row_estimate": None}],
    )
    assert list(c.tables) == ["EMPLOYEES"] and c.tables["EMPLOYEES"].primary_key == ["EMPLOYEE_ID"]
    assert c.tables["EMPLOYEES"].row_estimate is None


def test_mssql_negative_or_text_estimates_and_composite_key_order():
    c = build_catalog(
        "awdb",
        DIALECTS["mssql"],
        column_rows=[{"table_name": "Orders", "column_name": c_, "data_type": "int", "column_comment": None} for c_ in ("OrderID", "LineNo")],
        table_rows=[{"table_name": "Orders", "table_type": "BASE TABLE", "table_comment": None}],
        key_rows=[
            {"table_name": "Orders", "constraint_type": "PRIMARY KEY", "constraint_name": "PK_Orders", "column_name": "LineNo", "ordinal_position": 2},
            {"table_name": "Orders", "constraint_type": "PRIMARY KEY", "constraint_name": "PK_Orders", "column_name": "OrderID", "ordinal_position": 1},
        ],
        estimate_rows=[{"table_name": "Orders", "row_estimate": -1}],
    )
    assert c.tables["Orders"].primary_key == ["OrderID", "LineNo"] and c.tables["Orders"].row_estimate is None


def test_names_match_across_queries_even_when_case_differs():
    c = build_catalog(
        "x1",
        DIALECTS["mysql"],
        column_rows=[{"table_name": "Orders", "column_name": "id", "data_type": "int"}],
        table_rows=[{"table_name": "Orders", "table_type": "BASE TABLE"}],
        key_rows=[{"table_name": "orders", "constraint_type": "PRIMARY KEY", "constraint_name": "p", "column_name": "id", "ordinal_position": 1}],
    )
    assert list(c.tables) == ["Orders"] and c.tables["Orders"].primary_key == ["id"]


def test_columns_for_a_table_missing_from_the_table_list_are_kept_and_warned_about():
    c = build_catalog(
        "x1",
        DIALECTS["mysql"],
        column_rows=[{"table_name": "ghost", "column_name": "id", "data_type": "int"}],
        table_rows=[],
    )
    assert "ghost" in c.tables and c.warnings and "ghost" in c.warnings[0]


# ------------------------------------------------------------------ the gate


def test_gate_reports_exactly_which_tables_disagree():
    c = _pg_catalog()
    sd, inv = schema_document(c), inventory_document(c)
    inv["tables"] = [t for t in inv["tables"] if t["name"] != "orders"]  # inventory lost one
    report = check_consistency(c, sd, inv, {"customer", "ghost_chunk"})  # chunks miss 8, add 1
    assert report["ok"] is False
    text = " | ".join(report["problems"])
    assert "not in inventory: ['orders']" in text
    assert "in schema chunks but not in catalog: ['ghost_chunk']" in text
    assert "inventory counts.total" in text


# ------------------------------------------------------------------ writing files


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    shutil.copytree(ROOT / "databases" / "_template", base / "_template")
    shutil.copytree(base / "_template", base / "pg_live")
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    return base


def _docs():
    c = _pg_catalog()
    return schema_document(c), inventory_document(c)


def test_writes_both_files_into_a_fresh_template_folder(dbs: Path):
    sd, inv = _docs()
    result = write_catalog_files("pg_live", sd, inv)
    assert result == {"written": ["schema/schema.json", "schema/table_inventory.json"], "skipped": {}}
    assert json.loads((dbs / "pg_live/schema/schema.json").read_text())["table_count"] == 9
    assert [p.name for p in (dbs / "pg_live/schema").iterdir() if p.name.startswith(".") and p.suffix == ".tmp"] == []


def test_regeneration_replaces_its_own_files(dbs: Path):
    sd, inv = _docs()
    write_catalog_files("pg_live", sd, inv)
    assert write_catalog_files("pg_live", sd, inv)["written"] == ["schema/schema.json", "schema/table_inventory.json"]


def test_a_curated_schema_json_is_never_replaced(dbs: Path):
    curated = dbs / "pg_live/schema/schema.json"
    curated.write_text(json.dumps({"tables": [{"name": "hand_made", "domain": "Sales"}]}), encoding="utf-8")
    before = curated.read_bytes()
    result = write_catalog_files("pg_live", *_docs())
    assert curated.read_bytes() == before
    assert "schema/schema.json" in result["skipped"] and result["written"] == ["schema/table_inventory.json"]


def test_unreadable_schema_json_is_left_alone(dbs: Path):
    (dbs / "pg_live/schema/schema.json").write_text("{not json", encoding="utf-8")
    assert "schema/schema.json" in write_catalog_files("pg_live", *_docs())["skipped"]
    assert (dbs / "pg_live/schema/schema.json").read_text() == "{not json"


def test_a_foreign_inventory_file_is_left_alone(dbs: Path):
    (dbs / "pg_live/schema/table_inventory.json").write_text(json.dumps({"mine": True}), encoding="utf-8")
    assert "schema/table_inventory.json" in write_catalog_files("pg_live", *_docs())["skipped"]


def test_missing_folder_and_pinned_database_write_nothing(dbs: Path):
    assert "does not exist" in write_catalog_files("no_folder_db", *_docs())["skipped"]["all"]
    assert not (dbs / "no_folder_db").exists()
    (dbs / "erp_main/schema").mkdir(parents=True)
    result = write_catalog_files("erp_main", *_docs())
    assert result["written"] == [] and list((dbs / "erp_main/schema").iterdir()) == []


# ------------------------------------------------------------------ running it


class _Runner:
    """Records every SQL string; returns the real PostgreSQL rows for the matching query."""

    def __init__(self, fail: set[str] = frozenset()):
        self.sql: list[str] = []
        self.fail = fail
        p = DIALECTS["postgresql"]
        self.by_sql = {getattr(p, n): PG[n] for n in ("table_query", "key_query", "row_estimate_query")}
        self.names = {getattr(p, n): n for n in ("table_query", "key_query", "row_estimate_query")}

    async def __call__(self, sql: str, max_rows: int | None = None, db_id: str | None = None):
        self.sql.append(sql)
        assert db_id == "pg_live" and max_rows == 20000
        if self.names[sql] in self.fail:
            raise PermissionError("permission denied for table pg_constraint")
        return self.by_sql[sql]


@pytest.mark.asyncio
async def test_run_executes_only_the_profiles_catalog_queries_and_reports(dbs: Path):
    runner = _Runner()
    p = DIALECTS["postgresql"]
    report = await run_catalog_phase(
        db_id="pg_live", dialect=p, runner=runner, schema_rows=PG["schema_query"], fk_rows=PG["fk_query"],
        chunk_names={r["table_name"] for r in PG["schema_query"]},
    )
    assert set(runner.sql) == {p.table_query, p.key_query, p.row_estimate_query}  # nothing else was sent
    assert report["ok"] is True and (report["tables"], report["views"], report["foreign_keys"]) == (8, 1, 4)
    assert report["unavailable"] == [] and report["files"]["written"] == ["schema/schema.json", "schema/table_inventory.json"]


@pytest.mark.asyncio
async def test_a_failing_optional_query_degrades_instead_of_failing(dbs: Path):
    report = await run_catalog_phase(
        db_id="pg_live", dialect=DIALECTS["postgresql"], runner=_Runner(fail={"key_query"}),
        schema_rows=PG["schema_query"], fk_rows=PG["fk_query"], chunk_names={r["table_name"] for r in PG["schema_query"]},
    )
    assert report["ok"] is True and report["unavailable"] == ["keys"]
    assert any("keys query failed: PermissionError" in w for w in report["warnings"])
    inv = json.loads((dbs / "pg_live/schema/table_inventory.json").read_text())
    assert inv["unavailable"] == ["keys"] and all(t["primary_key"] == [] for t in inv["tables"])
    assert len(inv["tables"]) == 9  # the inventory is still complete


@pytest.mark.asyncio
async def test_engine_without_optional_queries_is_reported_not_guessed(dbs: Path):
    from src.sql.dialects.base import SQLDialectProfile

    bare = SQLDialectProfile(key="mysql", name="Bare", sqlglot_dialect="mysql", schema_query="", date_functions="", fk_query="")
    runner = AsyncMock()
    report = await run_catalog_phase(
        db_id="pg_live", dialect=bare, runner=runner, schema_rows=PG["schema_query"], fk_rows=[], chunk_names=None
    )
    runner.assert_not_awaited()
    assert report["unavailable"] == ["keys", "row_estimates", "tables"] and report["tables"] == 9 and report["views"] == 0


# ------------------------------------------------------------------ real SQLite, through the real sync


@pytest.fixture()
def sqlite_db(tmp_path: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customer(id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customer(id), total REAL);
        CREATE TABLE order_items(order_id INTEGER, line_no INTEGER, sku TEXT, PRIMARY KEY(order_id, line_no),
                                 FOREIGN KEY(order_id) REFERENCES orders(id));
        CREATE VIEW v_orders AS SELECT * FROM orders;
        INSERT INTO customer(email, name) VALUES ('a@x.com', 'A'), ('b@x.com', 'B');
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": "sqlite", "path": str(path)})
    monkeypatch.setattr(settings, "db_engine", "mysql")
    return path


def _sync_args():
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(side_effect=lambda chunks: ([[0.1] * 8] * len(chunks), [None] * len(chunks)))
    return {"embedding_service": emb, "vector_store": AsyncMock()}


@pytest.mark.asyncio
async def test_real_sqlite_sync_writes_the_catalog_files(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_catalog")
    result = await sync_live_schema(db_id="pg_live", **_sync_args())

    assert result["status"] == "ok"
    report = result["catalog"]
    assert (report["tables"], report["views"], report["foreign_keys"]) == (3, 1, 2)
    assert report["unavailable"] == ["row_estimates"]  # SQLite offers no cheap row estimate

    inv = json.loads((dbs / "pg_live/schema/table_inventory.json").read_text())
    by_name = {t["name"]: t for t in inv["tables"]}
    assert by_name["order_items"]["primary_key"] == ["order_id", "line_no"] and by_name["order_items"]["foreign_key_count"] == 1
    assert by_name["v_orders"]["kind"] == "view" and by_name["customer"]["column_count"] == 3
    # the sync embeds only real tables for SQLite, so the gate says so out loud rather than hiding the view
    assert report["ok"] is False and any("v_orders" in p and "schema chunks" in p for p in report["problems"])


@pytest.mark.asyncio
async def test_real_sqlite_catalog_query_never_reads_data_rows(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    seen: list[str] = []
    import src.core.db_client as dbc

    real = dbc.run_readonly_query

    async def spy(sql, *a, **kw):
        seen.append(sql)
        return await real(sql, *a, **kw)

    monkeypatch.setattr("src.sql.schema_retrieval.run_readonly_query", spy)
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_catalog")
    await sync_live_schema(db_id="pg_live", **_sync_args())

    allowed = {getattr(DIALECTS["sqlite"], n) for n in ("schema_query", "column_query", "table_query", "key_query")}
    assert seen and set(seen) <= allowed
    assert not any(" customer" in s.lower() and "from customer" in s.lower() for s in seen)


@pytest.mark.asyncio
async def test_sync_without_the_flag_is_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: False)
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert set(result) == {"status", "db_id", "document_id", "tables_synced", "table_names"}
    assert not (dbs / "pg_live/schema/table_inventory.json").exists()


@pytest.mark.asyncio
async def test_a_crash_in_the_catalog_phase_never_fails_the_sync(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag == "auto_catalog")
    monkeypatch.setattr(cat_mod, "run_catalog_phase", AsyncMock(side_effect=RuntimeError("boom")))
    result = await sync_live_schema(db_id="pg_live", **_sync_args())
    assert result["status"] == "ok" and result["catalog"]["ok"] is False and "boom" in result["catalog"]["error"]


def test_flag_is_known_and_erp_main_is_pinned_off():
    from src.utils.feature_flags import DB_FEATURE_FLAGS, is_db_feature_enabled

    assert "auto_catalog" in DB_FEATURE_FLAGS and is_db_feature_enabled("auto_catalog", "erp_main") is False
