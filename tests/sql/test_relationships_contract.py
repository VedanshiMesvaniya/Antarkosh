"""Phase 6 / step 6a: pin the EXISTING behaviour that relationship inference relies on.

Nothing here tests new code. These tests document how ``relationships.json`` is consumed today, so that a
later change to that code (for example schema-aware answering, Phase 3d) fails loudly here and prompts a
review of Phase 6, instead of silently changing what an inferred relationship does in a prompt.

What Phase 6 relies on, and why it matters:
* the prompt pipeline sees ONE schema and BARE lower-case table names, so only edges between tables of that
  schema can ever be used (``extract_schema_table_names``, ``get_1hop_neighbors``, ``format_scoped_relationships``);
* ``relationships.json`` edges pull neighbouring tables into a prompt (1-hop expansion), so a wrong edge changes
  which tables the model sees;
* extra keys on an edge (confidence, evidence) are ignored by the readers;
* the readers cache per database and a sync does NOT clear that cache, so a writer must call ``clear_prompt_caches``;
* the template ships ``{"relationships": []}`` as a replaceable placeholder.
"""
# ruff: noqa: F811

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.sql import prompt_builder as pb
from src.sql.schema_retrieval import (
    extract_schema_table_names,
    format_scoped_relationships,
    get_1hop_neighbors,
)
from tests.sql.test_catalog_extraction import ROOT, dbs  # noqa: F401  (shared fixture)

RELS = [
    {"from_table": "orders", "from_column": "customer_id", "to_table": "customer", "to_column": "id",
     "confidence": 0.91, "evidence": [{"rule": "proc_join", "weight": 0.9}], "from_schema": "public", "to_schema": "public"},
    {"from_table": "order_items", "from_column": "order_id", "to_table": "orders", "to_column": "id"},
]


@pytest.fixture()
def rel_db(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs)
    pb.clear_prompt_caches()
    yield dbs
    pb.clear_prompt_caches()


def _write(rel_db: Path, rels) -> None:
    (rel_db / "pg_live/schema/relationships.json").write_text(json.dumps({"relationships": rels}), encoding="utf-8")


def test_the_template_ships_an_empty_replaceable_relationships_placeholder():
    assert json.loads((ROOT / "databases/_template/schema/relationships.json").read_text(encoding="utf-8")) == {"relationships": []}


def test_edges_with_extra_keys_are_read_and_the_extra_keys_are_ignored(rel_db: Path):
    _write(rel_db, RELS)
    raw = pb.get_raw_relationships("pg_live")
    assert len(raw) == 2 and raw[0]["confidence"] == 0.91
    assert pb.load_relationships("pg_live") == "- order_items: order_id->orders.id\n- orders: customer_id->customer.id"


def test_a_missing_or_broken_file_means_no_relationships_not_an_error(rel_db: Path):
    (rel_db / "pg_live/schema/relationships.json").unlink()
    assert pb.get_raw_relationships("pg_live") == []
    pb.clear_prompt_caches()
    (rel_db / "pg_live/schema/relationships.json").write_text("{not json", encoding="utf-8")
    assert pb.get_raw_relationships("pg_live") == []


def test_the_relationship_cache_is_per_database_and_only_clear_prompt_caches_refreshes_it(rel_db: Path):
    _write(rel_db, RELS[:1])
    assert len(pb.get_raw_relationships("pg_live")) == 1
    _write(rel_db, RELS)  # what a writer does
    assert len(pb.get_raw_relationships("pg_live")) == 1  # STALE: nothing cleared the cache
    pb.clear_prompt_caches("pg_live")
    assert len(pb.get_raw_relationships("pg_live")) == 2


def test_the_prompt_sees_bare_lower_case_table_names_only():
    assert extract_schema_table_names("CREATE TABLE Orders (id INT);\n\nCREATE TABLE order_items (x INT)") == {"orders", "order_items"}
    # Characterisation: a schema-qualified name yields only the schema part. If this ever changes (Phase 3d,
    # schema-aware answering), Phase 6 must be revisited: edges outside the answering schema become usable.
    assert extract_schema_table_names("CREATE TABLE erp_food.party (id INT)") == {"erp_food"}


def test_scoped_relationships_match_lower_case_bare_names_and_only_between_tables_in_the_prompt():
    text = format_scoped_relationships({"orders", "customer"}, rels=RELS)
    assert text == "- orders: customer_id->customer.id"  # order_items is not in the prompt, so its edge is dropped
    assert format_scoped_relationships({"orders"}, rels=RELS) == ""
    upper = [{"from_table": "Orders", "from_column": "customer_id", "to_table": "Customer", "to_column": "id"}]
    assert format_scoped_relationships({"orders", "customer"}, rels=upper) == "- orders: customer_id->customer.id"


def test_relationship_edges_pull_neighbouring_tables_into_the_prompt():
    assert get_1hop_neighbors({"orders"}, rels=RELS) == {"customer", "order_items"}
    assert get_1hop_neighbors({"customer"}, rels=RELS) == {"orders"}
    assert get_1hop_neighbors({"unrelated"}, rels=RELS) == set()
