"""The same behaviour in every supported SQL dialect: what each engine profile provides, and that the dialect-neutral steps cope with
each engine's native type names, upper-case names and quoting. (Live-server behaviour is outside what a unit test can show.)"""

from __future__ import annotations

import pytest

import src.sql.prompt_builder as pb
from src.sql.dialects import get_dialect_profile
from src.sql.prompt_builder import _prompt_table_names
from src.sql.schema_compactor import _simplify_data_type
from src.sql.schema_retrieval import format_scoped_relationships

ENGINES = {"sqlite": "sqlite", "mysql": "mysql", "postgresql": "postgres", "mssql": "tsql", "oracle": "oracle"}


@pytest.mark.parametrize(("engine", "glot"), ENGINES.items())
def test_every_engine_profile_provides_what_the_phases_need(engine: str, glot: str) -> None:
    p = get_dialect_profile(engine)
    assert p.sqlglot_dialect == glot  # the name sqlglot knows (SQL Server is "tsql", not "mssql")
    assert p.schema_query and p.date_functions and p.table_query and p.key_query
    if engine != "sqlite":  # SQLite has no foreign-key catalog view and keeps no row statistics; the extractor says so in its report
        assert p.fk_query and p.row_estimate_query
    else:
        assert not p.row_estimate_query


# each engine's own spelling of the common types -> the family the compact schema shows
TYPES = {
    "mysql": {"int": "int", "bigint": "int", "tinyint": "int", "varchar": "varchar", "longtext": "varchar", "datetime": "datetime", "timestamp": "datetime", "decimal": "decimal", "enum": "enum", "json": "json"},
    "postgresql": {"integer": "int", "smallint": "int", "character varying": "varchar", "text": "varchar", "boolean": "bool", "timestamp without time zone": "datetime", "timestamp with time zone": "datetime", "numeric": "decimal", "uuid": "uuid", "jsonb": "json"},
    "mssql": {"int": "int", "bit": "bit", "nvarchar": "varchar", "nchar": "varchar", "datetime2": "datetime", "smalldatetime": "datetime", "datetimeoffset": "datetime", "decimal": "decimal", "money": "money", "uniqueidentifier": "uniqueidentifier"},
    "oracle": {"NUMBER": "number", "VARCHAR2": "varchar", "NVARCHAR2": "varchar", "CHAR": "varchar", "DATE": "datetime", "TIMESTAMP(6)": "datetime", "TIMESTAMP(6) WITH TIME ZONE": "datetime", "CLOB": "varchar", "BLOB": "blob"},
    "sqlite": {"INTEGER": "int", "TEXT": "varchar", "REAL": "decimal", "BOOLEAN": "bool", "DATETIME": "datetime", "VARCHAR(50)": "varchar", "DECIMAL(10,2)": "decimal"},
}


@pytest.mark.parametrize(("engine", "native", "family"), [(e, n, f) for e, m in TYPES.items() for n, f in m.items()])
def test_each_engines_native_types_compact_to_a_sensible_family(engine: str, native: str, family: str) -> None:
    assert _simplify_data_type(native) == family


def test_upper_case_names_as_oracle_reports_them_scope_the_relationships_like_lower_case_ones() -> None:
    schema = "ORDERS: ID(int, PK), CUSTOMER_ID(int, FK->CUSTOMERS.ID)\n\nCUSTOMERS: ID(int, PK)"
    names = _prompt_table_names(schema)
    assert names == {"orders", "customers"}
    rels = [{"from_table": "ORDERS", "from_column": "CUSTOMER_ID", "to_table": "CUSTOMERS", "to_column": "ID"}]
    assert format_scoped_relationships(names, "q", rels=rels) == "- orders: CUSTOMER_ID->customers.ID"


@pytest.mark.parametrize("engine", list(ENGINES))
def test_the_sql_prompt_builds_for_every_engine_and_carries_its_name_and_date_functions(engine: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id="x": [])
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id="x": "")
    profile = get_dialect_profile(engine)
    prompt = pb.build_sql_prompt("orders per customer", "orders: id(int, PK)\n\ncustomers: id(int, PK)", profile, db_id="parity_db")
    assert profile.name in prompt and profile.date_functions.strip().splitlines()[0].strip() in prompt
