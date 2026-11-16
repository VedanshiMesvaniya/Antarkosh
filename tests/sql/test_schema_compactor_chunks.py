"""Schema chunks as the retriever really builds them (header comment, trailing FOREIGN KEY lines) must parse and compact."""

from __future__ import annotations

import logging

import pytest

from src.sql.schema_compactor import _prepare_ddl_for_parse, compact_ddl, extract_join_hints
from src.sql.schema_retrieval import _enrich_table_schema

BODY = "TABLE sqts_cost_recovery (\n  id int NOT NULL,\n  qn_number varchar(20),\n  price decimal(18,2),\n  PRIMARY KEY (id)\n)"
FKS = ["  FOREIGN KEY (qn_number) REFERENCES sqts_main_lear(qn_number)"]
META = {"domain": "quality", "primary_key": ["id"]}


@pytest.mark.parametrize("meta", [None, META])
@pytest.mark.parametrize("fks", [None, FKS])
def test_every_chunk_shape_compacts_without_falling_back_to_raw_ddl(caplog: pytest.LogCaptureFixture, meta, fks):
    chunk = _enrich_table_schema("sqts_cost_recovery", BODY, meta, fks)
    with caplog.at_level(logging.WARNING):
        out = compact_ddl(chunk, "tsql")
    assert "could not parse" not in caplog.text and "Falling back" not in caplog.text
    assert out.startswith("sqts_cost_recovery:") and "qn_number(varchar" in out and "CREATE" not in out
    assert ("FK->sqts_main_lear.qn_number" in out) == bool(fks)


@pytest.mark.parametrize("meta", [None, META])
def test_join_hints_are_read_from_the_foreign_key_lines_of_a_chunk(meta):
    chunk = _enrich_table_schema("sqts_cost_recovery", BODY, meta, FKS)
    assert "sqts_cost_recovery.qn_number -> sqts_main_lear.qn_number" in extract_join_hints([chunk], "tsql")
    assert extract_join_hints([_enrich_table_schema("sqts_cost_recovery", BODY, meta, None)], "tsql") == ""


def test_plain_create_table_is_unchanged_by_the_pre_clean():
    plain = "CREATE TABLE t (\n  id int NOT NULL,\n  name varchar(50),\n  PRIMARY KEY (id)\n)"
    assert _prepare_ddl_for_parse(plain) == plain
    assert compact_ddl(plain, "tsql") == "t: id(int), name(varchar)"


def test_whole_line_comments_are_dropped_and_a_comment_ending_in_a_comma_no_longer_adds_a_comma():
    ddl = "-- Table: t | Domain: x,\nTABLE t (\n  -- the key, always set,\n  id int,\n  name varchar(5)\n)"
    assert _prepare_ddl_for_parse(ddl).startswith("CREATE TABLE t (")
    assert ",\n  ,\n" not in _prepare_ddl_for_parse(ddl)
    assert compact_ddl(ddl, "tsql") == "t: id(int), name(varchar)"


def test_text_after_the_table_that_is_not_foreign_keys_is_left_exactly_as_it_was():
    odd = "CREATE TABLE t (id int)\nCOMMENT ON TABLE t IS 'x'"
    assert _prepare_ddl_for_parse(odd) == odd
    unbalanced = "TABLE t (id int, name varchar(5)"
    assert _prepare_ddl_for_parse(unbalanced).endswith("varchar(5)")


def test_several_foreign_key_lines_and_a_closing_paren_inside_a_string_are_handled():
    chunk = "TABLE a (\n  id int,\n  note varchar(5) DEFAULT ')',\n  x int,\n  y int\n)\n  FOREIGN KEY (x) REFERENCES b(id),\n  FOREIGN KEY (y) REFERENCES c(id)"
    out = compact_ddl(chunk, "tsql")
    assert "x(int, FK->b.id)" in out and "y(int, FK->c.id)" in out
