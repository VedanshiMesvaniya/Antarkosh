"""Soft-delete filtering for every database and every SQL dialect: classification, conditions, enforcement, prompt and the switch.

Syntax is checked in all five dialects by parsing what is written back; meaning is checked by running the enforced SQL on SQLite.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlglot

import src.sql.prompt_builder as pb
from src.sql import registry
from src.sql import soft_delete as sd
from src.sql import soft_delete_spec as ss
from src.sql.soft_delete_spec import SoftDeleteSpec, classify_column, discover_from_schema

DIALECTS = ["mysql", "postgres", "tsql", "oracle", "sqlite"]
DB = "sd_db"


def _norm(sql: str, dialect: str) -> str:
    return ss.tidy(sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect))


# ----------------------------------------------------------------------------------------------- classification

# (column, type, nullable, expected kind) as each engine's catalog reports them
CASES = [
    # MySQL
    ("deleted_at", "datetime", True, "timestamp"), ("is_deleted", "tinyint", True, "int_flag"), ("deleted", "tinyint", False, "int_flag"),
    # PostgreSQL
    ("deleted_at", "timestamp without time zone", True, "timestamp"), ("is_deleted", "boolean", False, "boolean"), ("deleted", "boolean", True, "boolean"),
    ("is_deleted", "character", True, "char_flag"),
    # SQL Server
    ("IsDeleted", "bit", False, "int_flag"), ("DeletedOn", "datetime2", True, "timestamp"), ("Deleted", "bit", True, "int_flag"),
    ("DeletedDate", "smalldatetime", True, "timestamp"), ("DEL_FLAG", "nchar", True, "char_flag"),
    # Oracle
    ("IS_DELETED", "NUMBER", True, "int_flag"), ("DELETED_FLAG", "CHAR", True, "char_flag"), ("DELETED_AT", "TIMESTAMP(6)", True, "timestamp"),
    # SQLite
    ("is_deleted", "INTEGER", True, "int_flag"), ("deleted", "BOOLEAN", True, "boolean"), ("deleted_at", "DATETIME", True, "timestamp"),
    # names are compared without case, underscores or spaces
    ("Is Deleted", "bit", True, "int_flag"), ("deletedAt", "timestamp", True, "timestamp"),
    # must NOT be taken for a soft-delete marker
    ("DeletedBy", "int", True, None), ("deleted_by", "varchar", True, None), ("deleted", "int", True, None), ("DELETED", "NUMBER", True, None),
    ("deleted_at", "int", True, None), ("deleted_at", "TEXT", True, None), ("deleted_at", "datetime", False, None),  # NOT NULL cannot mean "NULL while live"
    ("is_deleted", "varchar", True, None), ("is_deleted", "text", True, None), ("status", "char", True, None), ("archived_at", "datetime", True, None),
    ("deleted_count", "int", True, None), ("is_deleted", "uuid", True, None),
]


@pytest.mark.parametrize(("name", "type_text", "nullable", "expected"), CASES)
def test_classification(name: str, type_text: str, nullable: bool, expected: str | None) -> None:
    assert classify_column(name, type_text, nullable=nullable) == expected


def test_a_flag_in_a_wide_text_column_is_recognised_only_with_evidence() -> None:
    assert classify_column("is_deleted", "enum", nullable=True, enum_values=["Y", "N"]) == "char_flag"
    assert classify_column("is_deleted", "enum", nullable=True, enum_values=["active", "gone"]) is None
    assert classify_column("is_deleted", "varchar", nullable=True, default="('N')") == "char_flag"  # defaults to 'N'
    assert classify_column("is_deleted", "varchar", nullable=True, default="'maybe'") is None
    assert classify_column("is_deleted", "varchar(1)", nullable=True) == "char_flag"


def _schema(*tables: dict[str, Any]) -> dict[str, Any]:
    return {"generated_by": "antarkosh-auto-catalog", "tables": list(tables)}


def _t(name: str, *cols: tuple, pk: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"name": name, "primary_key": pk or [], "columns": [{"name": c[0], "type": c[1], "nullable": c[2] if len(c) > 2 else True, **(c[3] if len(c) > 3 else {})} for c in cols], **extra}


def test_discovery_picks_one_column_per_table_by_priority_and_says_what_it_skipped() -> None:
    doc = _schema(
        _t("orders", ("id", "int", False), ("is_deleted", "bit"), ("deleted_at", "datetime"), pk=["id"]),
        _t("plain", ("id", "int", False), ("name", "varchar")),
        _t("flags", ("id", "int", False), ("deleted", "char"), pk=["id"]),
    )
    specs, notes = discover_from_schema(doc)
    assert specs["orders"] == SoftDeleteSpec("orders", "deleted_at", "timestamp", "discovered")
    assert specs["flags"].kind == "char_flag" and "plain" not in specs
    assert notes == ["orders: also has is_deleted; using deleted_at"]


def test_a_key_column_is_never_taken_for_the_marker() -> None:
    doc = _schema(_t("odd", ("deleted", "bit", False), ("x", "int"), pk=["deleted"]), _t("fk", ("is_deleted", "bit", True, {"is_foreign_key": True})))
    assert discover_from_schema(doc)[0] == {}


def test_malformed_documents_give_nothing() -> None:
    for doc in (None, [], {}, {"tables": "x"}, {"tables": [None, {"name": "t", "columns": "no"}, {"columns": []}]}):
        assert discover_from_schema(doc)[0] == {}


# ----------------------------------------------------------------------------------------------- where the knowledge comes from


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "databases"
    (base / DB / "schema").mkdir(parents=True)
    (base / DB / "semantics").mkdir()
    monkeypatch.setattr(registry, "DATABASES_DIR", base)
    ss.clear_spec_cache()
    yield base / DB
    ss.clear_spec_cache()


def _write(root: Path, rel: str, doc: Any) -> None:
    (root / rel).write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


def test_specs_come_from_the_catalog(root: Path) -> None:
    _write(root, "schema/schema.json", _schema(_t("orders", ("is_deleted", "bit")), _t("items", ("deleted_on", "datetime2"))))
    assert {t: s.kind for t, s in ss.load_specs(DB).items()} == {"orders": "int_flag", "items": "timestamp"}


def test_the_atlas_beats_the_declared_column_which_beats_discovery(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(root, "schema/schema.json", _schema(
        _t("a", ("deleted_at", "datetime"), ("is_deleted", "bit")), _t("b", ("removed_flag", "char"), ("is_deleted", "bit")), _t("c", ("is_deleted", "bit"))))
    _write(root, "semantics/behavioral_atlas.json", {"tables": {"a": {"columns": {"deleted_at": {}}}}})
    monkeypatch.setattr(registry, "get_database", lambda db_id: {"soft_delete": {"column": "removed_flag"}})
    specs = ss.load_specs(DB)
    assert (specs["a"].column, specs["a"].source) == ("deleted_at", "atlas")
    assert (specs["b"].column, specs["b"].source, specs["b"].kind) == ("removed_flag", "declared", "char_flag")
    assert (specs["c"].column, specs["c"].source) == ("is_deleted", "discovered")


def test_the_curated_file_forces_a_column_and_can_ignore_a_table(root: Path) -> None:
    _write(root, "schema/schema.json", _schema(_t("orders", ("is_deleted", "bit")), _t("audit_log", ("is_deleted", "bit")), _t("misc", ("x", "int"))))
    _write(root, ss.CURATED_REL, {"tables": {"misc": {"column": "gone", "kind": "char_flag"}, "bad": {"column": "x", "kind": "nonsense"}}, "ignore": ["Audit_Log"]})
    specs = ss.load_specs(DB)
    assert set(specs) == {"orders", "misc"} and specs["misc"] == SoftDeleteSpec("misc", "gone", "char_flag", "curated")


def test_missing_broken_or_odd_files_give_no_specs_and_never_raise(root: Path) -> None:
    assert ss.load_specs(DB) == {}
    for text in ("{broken", "[]", '{"tables": 5}'):
        _write(root, "schema/schema.json", text)
        ss.clear_spec_cache()
        assert ss.load_specs(DB) == {}
    assert ss.load_specs("Bad Id!") == {}


def test_the_specs_follow_the_files_when_they_change(root: Path) -> None:
    _write(root, "schema/schema.json", _schema(_t("orders", ("is_deleted", "bit"))))
    assert set(ss.load_specs(DB)) == {"orders"}
    _write(root, "schema/schema.json", _schema(_t("orders", ("is_deleted", "bit")), _t("items", ("deleted_at", "datetime"))))
    assert set(ss.load_specs(DB)) == {"orders", "items"}


# ----------------------------------------------------------------------------------------------- the conditions, in every dialect

SPECS = {
    "orders": SoftDeleteSpec("orders", "deleted_at", "timestamp", "t"),
    "items": SoftDeleteSpec("items", "IsDeleted", "int_flag", "t"),
    "cust": SoftDeleteSpec("cust", "del_flag", "char_flag", "t"),
    "prods": SoftDeleteSpec("prods", "removed", "boolean", "t"),
}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("spec", list(SPECS.values()), ids=list(SPECS))
def test_every_condition_is_valid_sql_in_every_dialect(spec: SoftDeleteSpec, dialect: str) -> None:
    for make in (ss.active_condition, ss.deleted_condition):
        text = ss.render(make(spec, "x"), dialect)
        sqlglot.parse_one(f"SELECT 1 FROM t AS x WHERE {text}", read=dialect, error_level=sqlglot.ErrorLevel.RAISE)


EXPECTED_ACTIVE = {
    ("timestamp", "tsql"): "x.c IS NULL", ("int_flag", "tsql"): "(x.c = 0 OR x.c IS NULL)", ("char_flag", "oracle"): "(x.c IN ('N', 'n') OR x.c IS NULL)",
    ("boolean", "postgres"): "(x.c = FALSE OR x.c IS NULL)", ("int_flag", "mysql"): "(x.c = 0 OR x.c IS NULL)", ("timestamp", "sqlite"): "x.c IS NULL",
}


@pytest.mark.parametrize(("kind", "dialect"), list(EXPECTED_ACTIVE))
def test_the_not_deleted_condition_reads_as_expected(kind: str, dialect: str) -> None:
    assert ss.render(ss.active_condition(SoftDeleteSpec("t", "c", kind, "x"), "x"), dialect) == EXPECTED_ACTIVE[(kind, dialect)]


@pytest.mark.parametrize(("dialect", "quote"), [("mysql", "`"), ("postgres", '"'), ("oracle", '"'), ("sqlite", '"'), ("tsql", "[")])
def test_an_unusual_column_name_is_quoted_the_way_the_dialect_quotes(dialect: str, quote: str) -> None:
    text = ss.render(ss.active_condition(SoftDeleteSpec("t", "Is Deleted", "int_flag", "x"), "x"), dialect)
    assert f"{quote}Is Deleted" in text
    sqlglot.parse_one(f"SELECT 1 FROM t AS x WHERE {text}", read=dialect, error_level=sqlglot.ErrorLevel.RAISE)


def test_a_boolean_is_written_the_way_each_engine_writes_one() -> None:
    spec = SoftDeleteSpec("t", "c", "boolean", "x")
    assert "FALSE" in ss.render(ss.active_condition(spec, "x"), "postgres") and "= 0" in ss.render(ss.active_condition(spec, "x"), "tsql")


# ----------------------------------------------------------------------------------------------- enforcing it

CASES_SQL = [
    # (what the model wrote, ACTIVE_ONLY result in the shape every dialect shares)
    ("SELECT o.id FROM orders o", "SELECT o.id FROM orders AS o WHERE o.deleted_at IS NULL"),
    ("SELECT id FROM orders", "SELECT id FROM orders WHERE deleted_at IS NULL"),
    ("SELECT id FROM items WHERE price > 5", "SELECT id FROM items WHERE price > 5 AND (IsDeleted = 0 OR IsDeleted IS NULL)"),
    ("SELECT id FROM cust", "SELECT id FROM cust WHERE (del_flag IN ('N', 'n') OR del_flag IS NULL)"),
    ("SELECT n FROM other", "SELECT n FROM other"),
    ("SELECT o.id, i.id FROM orders o JOIN items i ON i.oid = o.id", "SELECT o.id, i.id FROM orders AS o JOIN items AS i ON i.oid = o.id WHERE o.deleted_at IS NULL AND (i.IsDeleted = 0 OR i.IsDeleted IS NULL)"),
    # the other side of a LEFT JOIN: the condition goes in ON, so a deleted match becomes "no match" instead of dropping the row
    ("SELECT c.id, o.id FROM cust c LEFT JOIN orders o ON o.cid = c.id", "SELECT c.id, o.id FROM cust AS c LEFT JOIN orders AS o ON o.cid = c.id AND o.deleted_at IS NULL WHERE (c.del_flag IN ('N', 'n') OR c.del_flag IS NULL)"),
    # an OR already in the filter must not swallow the condition
    ("SELECT id FROM orders WHERE a = 1 OR b = 2", "SELECT id FROM orders WHERE (a = 1 OR b = 2) AND deleted_at IS NULL"),
    ("SELECT id FROM orders WHERE x IN (SELECT oid FROM items)", "SELECT id FROM orders WHERE x IN (SELECT oid FROM items WHERE (IsDeleted = 0 OR IsDeleted IS NULL)) AND deleted_at IS NULL"),
    ("WITH t AS (SELECT id FROM orders) SELECT id FROM t", "WITH t AS (SELECT id FROM orders WHERE deleted_at IS NULL) SELECT id FROM t"),
    ("SELECT id FROM orders UNION ALL SELECT id FROM items", "SELECT id FROM orders WHERE deleted_at IS NULL UNION ALL SELECT id FROM items WHERE (IsDeleted = 0 OR IsDeleted IS NULL)"),
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(("written", "expected"), CASES_SQL)
def test_active_only_filtering_in_every_dialect(written: str, expected: str, dialect: str) -> None:
    out, _ = ss.enforce(written, "ACTIVE_ONLY", dialect, SPECS)
    assert out == _norm(expected, dialect)


ALREADY_THERE = [
    "SELECT id FROM orders WHERE deleted_at IS NULL",
    "SELECT id FROM items WHERE IsDeleted = 0",
    "SELECT id FROM items WHERE 0 = IsDeleted",
    "SELECT id FROM items WHERE IsDeleted <> 1",
    "SELECT id FROM items WHERE IsDeleted = 0 OR IsDeleted IS NULL",
    "SELECT id FROM cust WHERE del_flag = 'N'",
    "SELECT id FROM cust WHERE del_flag IN ('N')",
    "SELECT id FROM cust WHERE del_flag <> 'Y'",
    "SELECT id FROM prods WHERE removed = FALSE",
    "SELECT id FROM prods WHERE NOT removed",
    "SELECT o.id FROM orders o WHERE o.deleted_at IS NULL AND o.x = 1",
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("written", ALREADY_THERE)
def test_a_condition_the_model_already_wrote_is_not_repeated(written: str, dialect: str) -> None:
    out, added = ss.enforce(written, "ACTIVE_ONLY", dialect, SPECS)
    assert added == [] and out == _norm(written, dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_deleted_only_adds_the_deleted_condition_and_swaps_a_contradicting_one(dialect: str) -> None:
    cases = {
        "SELECT id FROM orders": "SELECT id FROM orders WHERE deleted_at IS NOT NULL",
        "SELECT id FROM items": "SELECT id FROM items WHERE IsDeleted <> 0",
        "SELECT id FROM cust": "SELECT id FROM cust WHERE del_flag IN ('Y', 'y')",
        "SELECT id FROM orders WHERE deleted_at IS NULL": "SELECT id FROM orders WHERE deleted_at IS NOT NULL",
        "SELECT id FROM items WHERE IsDeleted = 0": "SELECT id FROM items WHERE IsDeleted <> 0",
        "SELECT id FROM items WHERE IsDeleted = 1": "SELECT id FROM items WHERE IsDeleted = 1",
    }
    for written, expected in cases.items():
        assert ss.enforce(written, "DELETED_ONLY", dialect, SPECS)[0] == _norm(expected, dialect), written


def test_include_archived_and_unknown_tables_change_nothing_but_formatting() -> None:
    sql = "SELECT id FROM orders o JOIN other x ON x.id = o.id"
    assert ss.enforce(sql, "INCLUDE_ARCHIVED", "tsql", SPECS) == (_norm(sql, "tsql"), [])
    assert ss.enforce("SELECT 1 FROM other", "ACTIVE_ONLY", "tsql", {}) == ("SELECT 1 FROM other", [])


def test_a_statement_that_cannot_be_parsed_raises_so_the_caller_keeps_it() -> None:
    with pytest.raises(sqlglot.errors.ParseError):
        ss.enforce("SELEC FROM WHERE (", "ACTIVE_ONLY", "tsql", SPECS)


def test_the_added_conditions_are_reported() -> None:
    _, added = ss.enforce("SELECT o.id FROM orders o JOIN items i ON i.oid = o.id", "ACTIVE_ONLY", "tsql", SPECS)
    assert added == ["orders: o.deleted_at IS NULL", "items: (i.IsDeleted = 0 OR i.IsDeleted IS NULL)"]


# ----------------------------------------------------------------------------------------------- what the filtering MEANS (SQLite)


@pytest.fixture()
def lite() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE orders (id INTEGER PRIMARY KEY, cid INT, deleted_at DATETIME);
        CREATE TABLE items  (id INTEGER PRIMARY KEY, oid INT, IsDeleted INTEGER);
        CREATE TABLE cust   (id INTEGER PRIMARY KEY, name TEXT, del_flag CHAR(1));
        CREATE TABLE prods  (id INTEGER PRIMARY KEY, removed BOOLEAN);
        INSERT INTO cust VALUES (1,'live','N'), (2,'gone','Y'), (3,'null',NULL), (4,'lower','n');
        INSERT INTO orders VALUES (10,1,NULL), (11,1,'2024-01-01'), (12,3,NULL);
        INSERT INTO items VALUES (100,10,0), (101,10,1), (102,12,NULL);
        INSERT INTO prods VALUES (1,0), (2,1), (3,NULL);
        """
    )
    return con


def _run(con: sqlite3.Connection, sql: str, intent: str = "ACTIVE_ONLY") -> list[tuple]:
    return sorted(con.execute(ss.enforce(sql, intent, "sqlite", SPECS)[0]).fetchall())


def test_only_live_rows_are_returned_for_every_kind_of_marker(lite: sqlite3.Connection) -> None:
    assert _run(lite, "SELECT id FROM orders") == [(10,), (12,)]  # NULL marker = live
    assert _run(lite, "SELECT id FROM items") == [(100,), (102,)]  # 0 and NULL = live
    assert _run(lite, "SELECT id FROM cust") == [(1,), (3,), (4,)]  # 'N', 'n' and NULL = live
    assert _run(lite, "SELECT id FROM prods") == [(1,), (3,)]


def test_only_deleted_rows_are_returned_when_asked_for(lite: sqlite3.Connection) -> None:
    assert _run(lite, "SELECT id FROM orders", "DELETED_ONLY") == [(11,)]
    assert _run(lite, "SELECT id FROM items", "DELETED_ONLY") == [(101,)]
    assert _run(lite, "SELECT id FROM cust", "DELETED_ONLY") == [(2,)]
    assert _run(lite, "SELECT id FROM prods", "DELETED_ONLY") == [(2,)]


def test_a_deleted_row_on_the_far_side_of_a_left_join_becomes_no_match_not_a_missing_row(lite: sqlite3.Connection) -> None:
    rows = _run(lite, "SELECT c.id, o.id FROM cust c LEFT JOIN orders o ON o.cid = c.id")
    assert rows == [(1, 10), (3, 12), (4, None)]  # customer 1's deleted order 11 is gone, customer 4 stays without one; deleted customer 2 is out
    # the anti-join "customers without a live order" still works
    assert _run(lite, "SELECT c.id FROM cust c LEFT JOIN orders o ON o.cid = c.id WHERE o.id IS NULL") == [(4,)]


def test_an_or_in_the_models_filter_cannot_let_deleted_rows_through(lite: sqlite3.Connection) -> None:
    assert _run(lite, "SELECT id FROM orders WHERE id = 11 OR id = 10") == [(10,)]


def test_subqueries_and_unions_are_filtered_too(lite: sqlite3.Connection) -> None:
    assert _run(lite, "SELECT id FROM orders WHERE id IN (SELECT oid FROM items)") == [(10,), (12,)]
    assert _run(lite, "SELECT id FROM cust UNION ALL SELECT id FROM prods") == [(1,), (1,), (3,), (3,), (4,)]


# ----------------------------------------------------------------------------------------------- mandatory mode and the public function


def test_soft_delete_mode_is_always_apply() -> None:
    assert ss.soft_delete_mode(DB) == "apply"
    assert ss.soft_delete_mode("erp_main") == "apply"
    assert ss.soft_delete_mode("any_db") == "apply"


def test_soft_delete_is_mandatory_not_a_toggleable_feature_flag() -> None:
    from src.utils.feature_flags import DB_FEATURE_FLAGS, OFF_BY_DEFAULT_FLAGS

    assert "auto_soft_delete_apply" not in DB_FEATURE_FLAGS
    assert "auto_soft_delete_apply" not in OFF_BY_DEFAULT_FLAGS


@pytest.fixture()
def known(root: Path) -> Path:
    _write(root, "schema/schema.json", _schema(_t("orders", ("id", "int", False), ("is_deleted", "bit"), pk=["id"]), _t("cust", ("id", "int", False), ("deleted_at", "datetime"), pk=["id"])))
    return root


LEGACY_SQL = [
    "SELECT id FROM orders", "SELECT id FROM orders o JOIN party p ON p.id = o.pid WHERE o.x = 1", "SELECT id FROM party WHERE deleted_at IS NULL",
    "SELECT id FROM party WHERE a = 1 OR b = 2", "not sql at all (",
]


def test_mandatory_enforcement_applies_real_marker_columns(known: Path) -> None:
    out = sd.enforce_soft_delete_filter("SELECT o.id FROM orders o JOIN cust c ON c.id = o.cid", "ACTIVE_ONLY", "tsql", DB)
    assert out == _norm("SELECT o.id FROM orders o JOIN cust c ON c.id = o.cid WHERE (o.is_deleted = 0 OR o.is_deleted IS NULL) AND c.deleted_at IS NULL", "tsql")


def test_database_without_knowledge_keeps_legacy_behaviour(root: Path) -> None:
    sql = "SELECT id FROM orders"
    assert sd.enforce_soft_delete_filter(sql, "ACTIVE_ONLY", "mysql", DB) == sd._enforce_legacy(sql, "ACTIVE_ONLY", "mysql", DB)


def test_crash_in_the_new_path_falls_back_to_legacy(known: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "enforce", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sql = "SELECT id FROM party"
    assert sd.enforce_soft_delete_filter(sql, "ACTIVE_ONLY", "mysql", DB) == sd._enforce_legacy(sql, "ACTIVE_ONLY", "mysql", DB)


def test_the_old_path_no_longer_lets_an_or_swallow_its_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy function used to build ``a OR b AND deleted_at IS NULL`` (deleted rows matching ``a`` got through)."""
    monkeypatch.setattr(sd, "_get_tables_with_soft_delete", lambda db_id=DB: {"party"})
    assert sd._enforce_legacy("SELECT id FROM party WHERE a = 1 OR b = 2", "ACTIVE_ONLY", "mysql", DB) == "SELECT id FROM party WHERE (a = 1 OR b = 2) AND deleted_at IS NULL"
    assert sd._enforce_legacy("SELECT id FROM party WHERE a = 1 AND b = 2", "ACTIVE_ONLY", "mysql", DB) == "SELECT id FROM party WHERE a = 1 AND b = 2 AND deleted_at IS NULL"


# ----------------------------------------------------------------------------------------------- the prompt

_DIALECT = SimpleNamespace(name="T-SQL", date_functions="", sqlglot_dialect="tsql")


@pytest.fixture()
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")


def _prompt(query: str = "list orders") -> str:
    return pb.build_sql_prompt(query, "orders: id(int, PK), is_deleted(bit)\n\ncust: id(int, PK), deleted_at(datetime)\n\nother: id(int, PK)", _DIALECT, db_id=DB)


def test_mandatory_prompt_tells_model_the_real_columns(known: Path, quiet: None) -> None:
    on = _prompt()
    assert "Soft-delete conditions" in on
    block = on[on.index("\n\nSoft-delete conditions"):]
    assert "- orders: (alias.is_deleted = 0 OR alias.is_deleted IS NULL)" in block
    assert "- cust: alias.deleted_at IS NULL" in block
    assert "other" not in block


def test_the_prompt_block_follows_the_intent_of_the_question(known: Path, quiet: None) -> None:
    assert "alias.is_deleted <> 0" in _prompt("show the deleted orders")
    assert "AUDIT / HISTORY" in _prompt("order history and audit trail")
    assert "- orders:" not in _prompt("order history and audit trail").split("AUDIT / HISTORY")[1]


def test_only_the_tables_in_the_prompt_are_named_and_the_list_is_capped(root: Path) -> None:
    specs = {f"t{i}": SoftDeleteSpec(f"t{i}", "is_deleted", "int_flag", "x") for i in range(40)}
    block = ss.prompt_block(specs, {"t1", "t2", "zzz"}, "ACTIVE_ONLY", "tsql")
    assert "t1:" in block and "t2:" in block and "zzz" not in block
    assert ss.prompt_block(specs, {"zzz"}, "ACTIVE_ONLY", "tsql") == ""
    assert ss.prompt_block(specs, {f"t{i}" for i in range(40)}, "ACTIVE_ONLY", "tsql").count("\n- ") == ss.MAX_PROMPT_TABLES


def test_a_prompt_problem_never_breaks_the_prompt(known: Path, monkeypatch: pytest.MonkeyPatch, quiet: None) -> None:
    prompt_before = _prompt()
    assert "Soft-delete conditions" in prompt_before
    monkeypatch.setattr(ss, "prompt_block", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    prompt_after = _prompt()
    assert "Soft-delete conditions" not in prompt_after
