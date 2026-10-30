"""The SQL prompt must scope its relationship block to the tables it shows, in raw or compacted schema form."""

from types import SimpleNamespace

import pytest

import src.sql.prompt_builder as pb
from src.sql.prompt_builder import _RAW_RELATIONSHIPS_CACHE, build_sql_prompt

_DB = "test_scoping_db"
_DIALECT = SimpleNamespace(name="T-SQL", date_functions="")

# Compacted form: "table: col(type), ..." (no TABLE keyword), as produced when schema compaction is on.
_COMPACT_SCHEMA = (
    "sqts_orders: id(int, PK), customer_id(int, FK->sqts_customers.id), total(decimal)\n\n"
    "sqts_customers: id(int, PK), name(varchar)"
)


def _unrelated_edges(count: int) -> list[dict]:
    return [
        {"from_table": f"other_{i}", "from_column": "ref_id", "to_table": f"other_{i + 1}", "to_column": "id"}
        for i in range(count)
    ]


def _whole_map_text(rels: list[dict]) -> str:
    return "\n".join(f"- {r['from_table']}: {r['from_column']}->{r['to_table']}.{r['to_column']}" for r in rels)


@pytest.fixture(autouse=True)
def _clean_cache():
    yield
    _RAW_RELATIONSHIPS_CACHE.pop(_DB, None)


def test_prompt_table_names_sees_compacted_and_raw_tables():
    mixed = _COMPACT_SCHEMA + "\n\nTABLE sqts_raw (\n  id int\n)"
    assert pb._prompt_table_names(mixed) == {"sqts_orders", "sqts_customers", "sqts_raw"}


def test_compacted_schema_gets_scoped_relationships_not_the_whole_map():
    in_scope = [{"from_table": "sqts_orders", "from_column": "customer_id", "to_table": "sqts_customers", "to_column": "id"}]
    whole = in_scope + _unrelated_edges(400)
    _RAW_RELATIONSHIPS_CACHE[_DB] = whole

    prompt = build_sql_prompt("orders per customer", _COMPACT_SCHEMA, _DIALECT, db_id=_DB, relationships=_whole_map_text(whole))

    assert "customer_id->sqts_customers.id" in prompt
    assert "other_5" not in prompt  # the rest of the database's map is not in the prompt


def test_large_unscoped_map_is_left_out_when_nothing_can_be_scoped():
    whole = _unrelated_edges(400)
    _RAW_RELATIONSHIPS_CACHE[_DB] = whole

    prompt = build_sql_prompt("anything", "no recognisable tables here", _DIALECT, db_id=_DB, relationships=_whole_map_text(whole))

    assert "Table relationships:" not in prompt
    assert "other_5" not in prompt


def test_small_unscoped_map_is_still_used():
    small = "- production: product_id->product.id, machine_id->machine.id"
    prompt = build_sql_prompt("anything", "no recognisable tables here", _DIALECT, db_id=_DB, relationships=small)
    assert "Table relationships:\n" + small in prompt
