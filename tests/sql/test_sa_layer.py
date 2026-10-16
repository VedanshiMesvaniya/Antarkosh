"""SQLAlchemy layer: engine factory, multi-schema catalog reader, and its place in a schema sync.

* The PostgreSQL reflection fixture is REAL output from a live server, read as a restricted role.
* SQLite runs for real through SQLAlchemy against a temp file.
* MySQL, SQL Server and Oracle logic is covered with a fake Inspector and URL checks only: nothing here
  proves those engines against a live server.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
import sqlglot
from sqlalchemy.dialects import postgresql as pg_dialect_mod
from sqlalchemy.dialects import sqlite as sqlite_dialect_mod
from sqlalchemy.engine import make_url
from sqlalchemy.engine.reflection import ObjectKind
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from src.core import db_client
from src.core.config import settings
from src.sql import registry
from src.sql.dialects import DIALECTS
from src.sql.introspection.catalog import (
    catalog_from_reflection,
    check_consistency,
    inventory_document,
    schema_document,
)
from src.sql.sa import engine_factory, reflection
from src.sql.sa.catalog_phase import requested_schemas, run_catalog_phase_sa
from src.sql.sa.engine_factory import SA_DRIVERS, build_sa_url, create_sa_engine, resolve_connection
from src.sql.sa.estimates import ROW_ESTIMATE_SQL
from src.sql.sa.reflection import is_system_schema, reflect_database, reflect_with_inspector

ROOT = Path(__file__).resolve().parents[2]
FIX = json.loads((Path(__file__).parent / "fixtures" / "reflection_postgresql_multischema.json").read_text(encoding="utf-8"))


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    shutil.copytree(ROOT / "databases" / "_template", base / "_template")
    shutil.copytree(base / "_template", base / "shop_db")
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    return base


# =================================================================== engine factory


def _cfg(**over: Any) -> dict[str, Any]:
    return {"host": "db.example", "port": 5432, "database": "sales", "username": "reader", "password": "p@ss:w/rd;{x}", **over}


def test_each_supported_engine_has_its_driver():
    assert SA_DRIVERS == {
        "mysql": "mysql+aiomysql", "postgresql": "postgresql+asyncpg", "mssql": "mssql+aioodbc",
        "oracle": "oracle+oracledb_async", "sqlite": "sqlite+aiosqlite",
    }


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_url_for_mysql_and_postgresql_keeps_special_characters_in_the_password(engine):
    url = build_sa_url(engine, _cfg(), "shop_db")
    assert (url.drivername, url.host, url.port, url.database, url.username) == (SA_DRIVERS[engine], "db.example", 5432, "sales", "reader")
    assert url.password == "p@ss:w/rd;{x}" and "p@ss" not in url.render_as_string(hide_password=True)
    assert make_url(url.render_as_string(hide_password=False)).password == "p@ss:w/rd;{x}"  # survives a round trip


def test_url_for_sql_server_uses_the_odbc_driver_and_trusts_the_certificate():
    url = build_sa_url("mssql", _cfg(port=1433), "shop_db")
    assert url.drivername == "mssql+aioodbc"
    assert url.query == {"driver": "ODBC Driver 18 for SQL Server", "TrustServerCertificate": "yes"}
    assert build_sa_url("mssql", _cfg(odbc_driver="ODBC Driver 17 for SQL Server"), "shop_db").query["driver"] == "ODBC Driver 17 for SQL Server"


def test_url_for_oracle_uses_the_service_name_or_the_database_alias():
    cfg = {"host": "h", "port": 1521, "username": "hr", "password": "p"}
    assert build_sa_url("oracle", {**cfg, "service_name": "XEPDB1"}, "hr_db").query == {"service_name": "XEPDB1"}
    assert build_sa_url("oracle", {**cfg, "database": "ORCL"}, "hr_db").query == {"service_name": "ORCL"}


def test_sqlite_url_is_read_only(tmp_path: Path):
    path = tmp_path / "x.db"
    url = build_sa_url("sqlite", {"path": str(path)}, "lite_db")
    assert url.drivername == "sqlite+aiosqlite" and url.query == {"mode": "ro", "uri": "true"}
    assert url.database == f"file:{path.resolve().as_posix()}"


def test_a_non_default_database_never_borrows_the_default_databases_credentials(monkeypatch):
    monkeypatch.setattr(settings, "db_readonly_password", "ERP_SECRET")
    monkeypatch.setattr(settings, "db_readonly_user", "erp_user")
    monkeypatch.setattr(settings, "db_host", "erp-host")
    other = build_sa_url("postgresql", {"host": "h", "database": "d", "username": "u"}, "shop_db")
    assert other.password == "" and other.username == "u" and other.host == "h"
    default = build_sa_url("postgresql", {}, "erp_main")  # the default database keeps its legacy fallback
    assert (default.username, default.password, default.host) == ("erp_user", "ERP_SECRET", "erp-host")


def test_unsupported_engine_is_refused():
    with pytest.raises(ValueError, match="Unsupported db_engine"):
        build_sa_url("db2", {}, "shop_db")


def test_an_incomplete_non_default_connection_is_refused_before_any_url_is_built(monkeypatch):
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": "postgresql", "host": "h"})
    with pytest.raises(db_client.DatabaseNotConfiguredError, match="missing: database, username"):
        resolve_connection("shop_db")
    with pytest.raises(ValueError):
        resolve_connection("Bad Name")


@pytest.mark.parametrize("engine", ["postgresql", "mysql", "oracle"])
def test_engines_are_async_and_never_pool(engine, monkeypatch):
    cfg = {"engine": engine, "host": "h", "port": 1, "database": "d", "service_name": "S", "username": "u", "password": "p"}
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: dict(cfg))
    eng = create_sa_engine("shop_db")
    assert isinstance(eng, AsyncEngine) and isinstance(eng.sync_engine.pool, NullPool)
    if engine == "postgresql":
        assert eng.sync_engine.dialect.driver == "asyncpg"


def test_sql_server_engine_needs_the_os_odbc_driver(monkeypatch):
    try:
        import pyodbc  # noqa: F401
    except ImportError:
        pytest.skip("the OS ODBC library is not installed here")
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": "mssql", "host": "h", "port": 1, "database": "d", "username": "u"})
    assert create_sa_engine("shop_db").sync_engine.dialect.driver == "aioodbc"


# =================================================================== system schemas


@pytest.mark.parametrize(
    "dialect,name,expected",
    [
        ("postgresql", "information_schema", True), ("postgresql", "pg_toast", True), ("postgresql", "sales", False),
        ("mysql", "mysql", True), ("mysql", "performance_schema", True), ("mysql", "shop", False),
        ("mssql", "sys", True), ("mssql", "INFORMATION_SCHEMA", True), ("mssql", "db_owner", True), ("mssql", "Sales", False),
        ("oracle", "SYS", True), ("oracle", "XDB", True), ("oracle", "HR", False),
        ("sqlite", "main", False),
    ],
)
def test_system_schemas_are_recognised_per_engine(dialect, name, expected):
    assert is_system_schema(dialect, name) is expected


# =================================================================== reflection logic with a fake Inspector

PG = pg_dialect_mod.dialect()


class FakeInspector:
    """Stands in for sqlalchemy's Inspector. ``data[schema][table]`` holds the pieces."""

    def __init__(self, data: dict[str | None, dict[str, dict[str, Any]]], default_schema: str | None, schemas: list[str] | None = None,
                 views: dict[str | None, list[str]] | None = None, fail: set[str] | None = None, dialect: Any = PG):
        self.data, self.default_schema_name, self._schemas = data, default_schema, schemas
        self._views, self.fail, self.dialect = views or {}, fail or set(), dialect
        self.schema_args: list[str | None] = []

    def _guard(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} blew up")

    def get_schema_names(self):
        return self._schemas if self._schemas is not None else [s for s in self.data if s is not None]

    def get_table_names(self, schema=None):
        self.schema_args.append(schema)
        return [t for t in self.data.get(schema, {}) if t not in self._views.get(schema, [])]

    def get_view_names(self, schema=None):
        return list(self._views.get(schema, []))

    def get_materialized_view_names(self, schema=None):
        raise NotImplementedError

    def _multi(self, schema, names, key, default):
        return {(schema, t): self.data[schema][t].get(key, default) for t in (names or self.data.get(schema, {}))}

    def get_multi_columns(self, schema=None, filter_names=None, kind=None):
        self._guard("columns")
        assert kind == ObjectKind.ANY
        return self._multi(schema, filter_names, "columns", [])

    def get_multi_pk_constraint(self, schema=None, filter_names=None):
        self._guard("pk")
        return self._multi(schema, filter_names, "pk", {"constrained_columns": []})

    def get_multi_unique_constraints(self, schema=None, filter_names=None):
        self._guard("unique")
        return self._multi(schema, filter_names, "unique", [])

    def get_multi_check_constraints(self, schema=None, filter_names=None):
        if "check_notimpl" in self.fail:
            raise NotImplementedError
        self._guard("check")
        return self._multi(schema, filter_names, "check", [])

    def get_multi_foreign_keys(self, schema=None, filter_names=None):
        self._guard("fk")
        return self._multi(schema, filter_names, "fk", [])

    def get_multi_indexes(self, schema=None, filter_names=None):
        self._guard("indexes")
        return self._multi(schema, filter_names, "indexes", [])

    def get_multi_table_comment(self, schema=None, filter_names=None, kind=None):
        self._guard("comment")
        return self._multi(schema, filter_names, "comment", {"text": None})


def _t(*cols: str, **extra: Any) -> dict[str, Any]:
    return {"columns": [{"name": c, "type": sa.Integer(), "nullable": True} for c in cols], **extra}


def test_system_and_empty_schemas_are_skipped_and_the_rest_are_all_read():
    insp = FakeInspector(
        {"sales": {"orders": _t("id")}, "hr": {"emp": _t("id")}, "empty_one": {}, "information_schema": {"tables": _t("x")}, "pg_toast": {}},
        "public",
    )
    out = reflect_with_inspector(insp, "postgresql")
    assert sorted(out["schemas"]) == ["hr", "sales"]
    assert out["skipped_schemas"] == {"information_schema": "system schema", "pg_toast": "system schema", "empty_one": "no readable tables or views"}
    assert out["default_schema"] == "public" and out["warnings"] == []


def test_requested_schemas_limit_the_read_and_match_oracle_case_insensitively():
    data = {"HR": {"EMPLOYEES": _t("ID")}, "SH": {"SALES": _t("ID")}}
    out = reflect_with_inspector(FakeInspector(data, "HR", dialect=PG), "oracle", ["hr", "nope"])
    assert list(out["schemas"]) == ["HR"]
    assert out["warnings"] == ["requested schema 'nope' was not found"]


def test_requested_schema_with_nothing_readable_warns():
    out = reflect_with_inspector(FakeInspector({"sales": {}}, "public"), "postgresql", ["sales"])
    assert out["schemas"] == {} and any("has no tables or views this account can read" in w for w in out["warnings"])


def test_unreadable_tables_are_dropped_and_a_wholly_unreadable_schema_is_skipped():
    data = {"sales": {"orders": _t("id"), "secret": _t("id")}, "locked": {"x": _t("id")}}
    accessible = {("sales", "orders")}
    out = reflect_with_inspector(FakeInspector(data, "public"), "postgresql", None, accessible)
    assert list(out["schemas"]) == ["sales"] and list(out["schemas"]["sales"]["tables"]) == ["orders"]
    assert out["skipped_schemas"]["locked"] == "no readable tables or views"
    pinned = reflect_with_inspector(FakeInspector(data, "public"), "postgresql", ["sales"], accessible)
    assert any("1 object(s) hidden" in w for w in pinned["warnings"])


def test_a_failing_inspector_call_becomes_a_warning_and_keeps_everything_else():
    data = {"sales": {"orders": _t("id", pk={"constrained_columns": ["id"]}, check=[{"name": "c", "sqltext": "id > 0"}])}}
    out = reflect_with_inspector(FakeInspector(data, "public", fail={"fk"}), "postgresql")
    table = out["schemas"]["sales"]["tables"]["orders"]
    assert table["primary_key"] == ["id"] and table["check_constraints"] == [{"name": "c", "sqltext": "id > 0"}]
    assert table["foreign_keys"] == [] and any(w.startswith("sales: foreign keys: RuntimeError") for w in out["warnings"])


def test_a_dialect_that_cannot_reflect_something_is_silent_not_a_warning():
    data = {"sales": {"orders": _t("id")}}
    out = reflect_with_inspector(FakeInspector(data, "public", fail={"check_notimpl"}), "postgresql")
    assert out["warnings"] == []


def test_columns_carry_nullability_defaults_comments_and_enum_values():
    mood = sa.Enum("ok", "bad", name="mood")
    cols = [
        {"name": "id", "type": sa.Integer(), "nullable": False, "default": None, "autoincrement": True, "comment": "key"},
        {"name": "m", "type": mood, "nullable": True, "default": "'ok'", "comment": "  "},
    ]
    out = reflect_with_inspector(FakeInspector({"s": {"t": {"columns": cols}}}, "public"), "postgresql")
    got = out["schemas"]["s"]["tables"]["t"]["columns"]
    assert got[0] == {"name": "id", "type": "INTEGER", "nullable": False, "default": None, "auto_increment": True, "comment": "key"}
    assert got[1]["enum_values"] == ["ok", "bad"] and got[1]["default"] == "'ok'" and got[1]["comment"] is None
    assert got[1]["type"] == "mood"  # the database's own type name, not a generic VARCHAR


def test_sqlite_is_one_schema_and_the_inspector_gets_no_schema_argument():
    insp = FakeInspector({None: {"t": _t("id")}}, "main", dialect=sqlite_dialect_mod.dialect())
    out = reflect_with_inspector(insp, "sqlite")
    assert list(out["schemas"]) == ["main"] and insp.schema_args == [None]


class _Conn:
    def __init__(self, rows: list[Any] | Exception):
        self.rows = rows

    def execute(self, stmt: Any):
        if isinstance(self.rows, Exception):
            raise self.rows
        return iter(self.rows)


def test_postgres_reflection_applies_the_privilege_check(monkeypatch):
    data = {"sales": {"orders": _t("id"), "secret": _t("id")}}
    monkeypatch.setattr(reflection, "inspect", lambda conn: FakeInspector(data, "public"))
    out = reflect_database(_Conn([("sales", "orders")]), "postgresql")
    assert list(out["schemas"]["sales"]["tables"]) == ["orders"]


def test_postgres_privilege_check_failure_is_reported_not_fatal(monkeypatch):
    data = {"sales": {"orders": _t("id")}}
    monkeypatch.setattr(reflection, "inspect", lambda conn: FakeInspector(data, "public"))
    out = reflect_database(_Conn(RuntimeError("nope")), "postgresql")
    assert list(out["schemas"]) == ["sales"] and any("access check unavailable" in w for w in out["warnings"])


def test_oracle_uses_non_maintained_accounts_and_falls_back_to_the_builtin_list(monkeypatch):
    data = {"HR": {"EMP": _t("ID")}, "SYS": {"OBJ": _t("ID")}, "SH": {"S": _t("ID")}}
    monkeypatch.setattr(reflection, "inspect", lambda conn: FakeInspector(data, "HR"))
    assert sorted(reflect_database(_Conn([("HR",)]), "oracle")["schemas"]) == ["HR"]  # ORACLE_MAINTAINED = 'N'
    assert sorted(reflect_database(_Conn(RuntimeError("pre-12c")), "oracle")["schemas"]) == ["HR", "SH"]  # static list drops SYS


# =================================================================== real PostgreSQL reflection (recorded)

ALL = FIX["all_schemas"]


def _pg_catalog(reflection_data=ALL, **kw):
    return catalog_from_reflection("northwind", "postgresql", reflection_data, **kw)


def test_recorded_reflection_never_listed_what_the_account_cannot_read():
    assert "locked" not in ALL["schemas"] and "empty_one" not in ALL["schemas"]
    assert "hidden_t" not in ALL["schemas"]["sales"]["tables"]
    assert ALL["skipped_schemas"] == {"information_schema": "system schema", "empty_one": "no readable tables or views", "locked": "no readable tables or views"}


def test_catalog_covers_every_readable_schema_with_qualified_names():
    cat = _pg_catalog()
    assert sorted(cat.tables) == [
        "Mixed Case.Order Lines", "hr.employee", "public.p1", "sales.customer", "sales.employee",
        "sales.mv", "sales.orders", "sales.v_orders",
    ]
    assert cat.tables["sales.mv"].kind == "materialized_view" and cat.tables["sales.v_orders"].kind == "view"
    assert cat.schemas["sales"] == {"tables": 3, "views": 2} and cat.default_schema == "public"
    assert cat.name_collisions == {"employee": ["hr", "sales"]}  # the same bare name in two schemas


def test_cross_schema_foreign_keys_know_both_schemas():
    cat = _pg_catalog()
    assert {"from_schema": "sales", "from_table": "customer", "from_column": "hr_owner", "to_schema": "hr", "to_table": "employee", "to_column": "id"} in cat.foreign_keys
    assert {"from_schema": "sales", "from_table": "orders", "from_column": "customer_id", "to_schema": "sales", "to_table": "customer", "to_column": "id"} in cat.foreign_keys


def test_a_foreign_key_with_no_stated_schema_points_at_the_default_schema():
    reflection_data = {
        "default_schema": "public", "warnings": [],
        "schemas": {"sales": {"tables": {"customer": {"kind": "table", "columns": [], "foreign_keys": [
            {"name": "f", "columns": ["country_id"], "referred_schema": None, "referred_table": "country", "referred_columns": ["id"]}]}}}},
    }
    fk = _pg_catalog(reflection_data).foreign_keys[0]
    assert (fk["from_schema"], fk["to_schema"]) == ("sales", "public")  # verified against a real PostgreSQL


def test_documents_for_a_multi_schema_database():
    cat = _pg_catalog(answering_schema="public")
    sd, inv = schema_document(cat), inventory_document(cat)
    assert sd["multi_schema"] is True and sd["source"] == "sqlalchemy" and [s["name"] for s in sd["schemas"]] == ["Mixed Case", "hr", "public", "sales"]
    by_q = {t["qualified_name"]: t for t in sd["tables"]}
    cust = by_q["sales.customer"]
    assert cust["schema"] == "sales" and cust["name"] == "customer" and cust["primary_key"] == ["id"] and cust["unique_keys"] == [["name"]]
    cols = {c["name"]: c for c in cust["columns"]}
    assert cols["mood"]["enum_values"] == ["ok", "bad"] and cols["mood"]["type"] == "sales.mood"
    assert cols["id"]["nullable"] is False and cols["id"]["is_primary_key"] is True and cols["hr_owner"]["is_foreign_key"] is True
    emp = by_q["hr.employee"]
    assert emp["comment"] == "Staff" and emp["check_constraints"][0]["sqltext"].startswith("dept = ANY")
    assert {c["name"]: c["comment"] for c in emp["columns"]}["name"] == "Full name"
    assert by_q["sales.orders"]["indexes"] == [{"name": "ix_orders_cust", "columns": ["customer_id"], "unique": False}]
    assert inv["counts"] == {"tables": 6, "views": 2, "total": 8} and inv["name_collisions"] == {"employee": ["hr", "sales"]}
    assert inv["answering_scope"] == {"schema": "public", "tables_outside_scope": 7}  # says plainly what answering covers
    assert check_consistency(cat, sd, inv, None)["ok"] is True


def test_the_sqlalchemy_schema_json_still_has_every_key_the_curated_file_has():
    erp = json.loads((ROOT / "databases" / "erp_main" / "schema" / "schema.json").read_text(encoding="utf-8"))
    ours = schema_document(_pg_catalog())
    assert set(erp) <= set(ours) and set(erp["tables"][0]) <= set(ours["tables"][0])
    assert set(erp["tables"][0]["columns"][0]) <= set(ours["tables"][0]["columns"][0])


def test_the_gate_catches_a_table_missing_from_one_of_the_files():
    cat = _pg_catalog()
    sd, inv = schema_document(cat), inventory_document(cat)
    inv["tables"] = [t for t in inv["tables"] if t["qualified_name"] != "hr.employee"]
    report = check_consistency(cat, sd, inv, None)
    assert report["ok"] is False and "not in inventory: ['hr.employee']" in " ".join(report["problems"])


def test_pinning_one_schema_changes_the_answering_scope_but_keeps_cross_schema_links():
    cat = _pg_catalog(FIX["pinned_sales"], answering_schema="sales")
    inv = inventory_document(cat)
    assert sorted(cat.schemas) == ["sales"] and inv["answering_scope"] == {"schema": "sales", "tables_outside_scope": 0}
    assert any(f["to_schema"] == "hr" for f in cat.foreign_keys)


def test_row_estimates_are_matched_by_schema_and_table_ignoring_case():
    cat = _pg_catalog(estimates=[{"TABLE_SCHEMA": "SALES", "TABLE_NAME": "Customer", "ROW_ESTIMATE": 300}, {"table_schema": "hr", "table_name": "employee", "row_estimate": -1}])
    assert cat.tables["sales.customer"].row_estimate == 300 and cat.tables["hr.employee"].row_estimate is None


def test_sqlite_catalog_uses_bare_names_and_no_schema_keys():
    data = {"default_schema": "main", "warnings": [], "schemas": {"main": {"tables": {"t": {"kind": "table", "columns": [{"name": "id", "type": "INTEGER"}], "foreign_keys": [
        {"columns": ["pid"], "referred_schema": None, "referred_table": "p", "referred_columns": ["id"]}]}, "p": {"kind": "table", "columns": []}}}}}
    cat = catalog_from_reflection("lite", "sqlite", data)
    assert sorted(cat.tables) == ["p", "t"] and cat.tables["t"].schema is None and cat.foreign_keys == [{"from_table": "t", "from_column": "pid", "to_table": "p", "to_column": "id"}]
    assert "qualified_name" not in schema_document(cat)["tables"][0]


# =================================================================== row-estimate SQL shape


@pytest.mark.parametrize("engine,read", [("postgresql", "postgres"), ("mysql", "mysql"), ("mssql", "tsql"), ("oracle", "oracle")])
def test_row_estimate_queries_are_single_catalog_selects(engine, read):
    tree = sqlglot.parse_one(ROW_ESTIMATE_SQL[engine], read=read)
    assert [e.alias_or_name for e in tree.expressions] == ["table_schema", "table_name", "row_estimate"]
    assert not list(tree.find_all(sqlglot.exp.Count)), "must never count data rows"
    assert not list(tree.find_all((sqlglot.exp.Insert, sqlglot.exp.Update, sqlglot.exp.Delete, sqlglot.exp.Drop)))
    for table in tree.find_all(sqlglot.exp.Table):
        schema = (table.args["db"].name if table.args.get("db") else "").lower()
        assert schema in {"information_schema", "pg_catalog", "sys"} or table.name.lower().startswith("all_"), table.sql()
    assert "sqlite" not in ROW_ESTIMATE_SQL


def test_only_postgres_and_oracle_connections_can_pin_a_schema():
    assert requested_schemas({"schema_name": "erp_food"}, "postgresql") == ["erp_food"]
    assert requested_schemas({"schema_name": " HR "}, "oracle") == ["HR"]
    assert requested_schemas({"schema_name": "x"}, "mysql") is None and requested_schemas({}, "postgresql") is None


# =================================================================== real SQLite through SQLAlchemy


@pytest.fixture()
def sqlite_db(tmp_path: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customer(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT, age INTEGER CHECK (age >= 0));
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customer(id), total REAL);
        CREATE INDEX ix_orders_customer ON orders(customer_id);
        CREATE TABLE order_items(order_id INTEGER, line_no INTEGER, sku TEXT, PRIMARY KEY(order_id, line_no),
                                 FOREIGN KEY(order_id) REFERENCES orders(id));
        CREATE VIEW v_orders AS SELECT * FROM orders;
        INSERT INTO customer(email, name) VALUES ('a@x.com', 'A');
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr("src.sql.registry.get_connection", lambda d, *a, **k: {"engine": "sqlite", "path": str(path)})
    monkeypatch.setattr(settings, "db_engine", "mysql")
    return path


@pytest.mark.asyncio
async def test_real_sqlite_catalog_through_sqlalchemy(sqlite_db: Path, dbs: Path):
    before = sqlite_db.read_bytes()
    report = await run_catalog_phase_sa(db_id="shop_db", dialect=DIALECTS["sqlite"], databases_dir=dbs)

    assert report["engine"] == "sqlalchemy" and report["ok"] is True and report["problems"] == []
    assert (report["tables"], report["views"], report["foreign_keys"]) == (3, 1, 2)
    assert report["schemas"] == {"main": {"tables": 3, "views": 1}} and report["unavailable"] == ["row_estimates"]
    assert report["files"]["written"] == ["schema/schema.json", "schema/table_inventory.json"]
    assert sqlite_db.read_bytes() == before  # the database file was not touched

    sd = json.loads((dbs / "shop_db/schema/schema.json").read_text())
    by_name = {t["name"]: t for t in sd["tables"]}
    assert "qualified_name" not in by_name["customer"]  # SQLite: one schema, bare names
    cust = {c["name"]: c for c in by_name["customer"]["columns"]}
    assert cust["email"]["nullable"] is False and cust["name"]["nullable"] is True
    assert by_name["order_items"]["primary_key"] == ["order_id", "line_no"] and by_name["v_orders"]["is_view"] is True
    assert sd["relationships"] == [
        {"from_table": "order_items", "from_column": "order_id", "to_table": "orders", "to_column": "id", "type": "fk"},
        {"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id", "type": "fk"},
    ]


@pytest.mark.asyncio
async def test_real_sqlite_check_constraints_unique_keys_and_indexes_are_read(sqlite_db: Path, dbs: Path):
    await run_catalog_phase_sa(db_id="shop_db", dialect=DIALECTS["sqlite"], databases_dir=dbs)
    sd = json.loads((dbs / "shop_db/schema/schema.json").read_text())
    by_name = {t["name"]: t for t in sd["tables"]}
    assert by_name["customer"]["unique_keys"] == [["email"]]
    # SQLite reports these only through SQLAlchemy's reader, which is the point of using it
    inv = json.loads((dbs / "shop_db/schema/table_inventory.json").read_text())
    assert inv["source"] == "sqlalchemy" and inv["counts"] == {"tables": 3, "views": 1, "total": 4}


# =================================================================== inside a schema sync


def _sync_args():
    emb = AsyncMock()
    emb.embed_chunks = AsyncMock(side_effect=lambda chunks: ([[0.1] * 8] * len(chunks), [None] * len(chunks)))
    return {"embedding_service": emb, "vector_store": AsyncMock()}


def _flags(monkeypatch, *on: str):
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in on)


@pytest.mark.asyncio
async def test_sync_uses_the_sqlalchemy_reader_when_its_flag_is_on(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    _flags(monkeypatch, "auto_catalog", "auto_sqlalchemy")
    result = await sync_live_schema(db_id="shop_db", **_sync_args())
    assert result["status"] == "ok" and result["catalog"]["engine"] == "sqlalchemy" and "sqlalchemy_fallback" not in result["catalog"]
    assert (dbs / "shop_db/schema/table_inventory.json").exists()


@pytest.mark.asyncio
async def test_sync_falls_back_to_the_native_extractor_if_sqlalchemy_fails(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    _flags(monkeypatch, "auto_catalog", "auto_sqlalchemy")
    monkeypatch.setattr("src.sql.sa.catalog_phase.create_sa_engine", MagicMock(side_effect=ImportError("aioodbc is not installed")))
    result = await sync_live_schema(db_id="shop_db", **_sync_args())
    cat = result["catalog"]
    assert result["status"] == "ok" and "engine" not in cat  # the native report
    assert "ImportError: aioodbc is not installed" in cat["sqlalchemy_fallback"] and cat["tables"] == 3
    assert (dbs / "shop_db/schema/table_inventory.json").exists()  # and the files still got written


@pytest.mark.asyncio
async def test_without_the_sqlalchemy_flag_the_native_extractor_runs_and_sqlalchemy_is_not_touched(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    _flags(monkeypatch, "auto_catalog")
    monkeypatch.setattr("src.sql.sa.catalog_phase.run_catalog_phase_sa", AsyncMock(side_effect=AssertionError("must not run")))
    result = await sync_live_schema(db_id="shop_db", **_sync_args())
    assert result["status"] == "ok" and "engine" not in result["catalog"]


@pytest.mark.asyncio
async def test_with_no_flags_the_sync_result_is_unchanged(sqlite_db: Path, dbs: Path, monkeypatch):
    from src.sql.schema_retrieval import sync_live_schema

    _flags(monkeypatch)
    result = await sync_live_schema(db_id="shop_db", **_sync_args())
    assert set(result) == {"status", "db_id", "document_id", "tables_synced", "table_names"}


def test_the_flag_exists_and_the_default_database_is_pinned_off():
    from src.utils.feature_flags import DB_FEATURE_FLAGS, is_db_feature_enabled

    assert "auto_sqlalchemy" in DB_FEATURE_FLAGS and is_db_feature_enabled("auto_sqlalchemy", "erp_main") is False
