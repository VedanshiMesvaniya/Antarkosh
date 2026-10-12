"""Unit tests verifying Step G2 canonical modules."""

from __future__ import annotations

import src.sql.fast_path as new_fp
import src.sql.learning.pipeline_metrics as new_pm
import src.sql.repair.delta_repair as new_dr
import src.sql.repair.sql_repair as new_sr
import src.sql.safety.column_registry as new_cr
import src.sql.safety.empty_result_classifier as new_erc
import src.sql.semantic_cache as new_sc


def test_g2_canonical_modules_smoke():
    assert callable(new_pm.log_event)
    assert new_pm.CURRENT_DB_ID is not None
    assert new_cr.ColumnRegistry is not None
    assert callable(new_sr.attempt_delta_repair)
    assert callable(new_dr.build_delta_repair_payload)


def test_g2_fast_path_smoke():
    table_md = "| count |\n|:---|\n| 42 |"
    res = new_fp.fast_path_format(new_fp.QueryType.COUNT, table_md, "How many orders are there?")
    assert res is not None
    assert "42" in res


def test_g2_empty_result_classifier_smoke():
    assert new_erc.classify_empty_result("") == new_erc.VALID_EMPTY
    sql_with_join_no_where = "SELECT a.id FROM table_a a JOIN table_b b ON a.id = b.a_id"
    assert new_erc.classify_empty_result(sql_with_join_no_where) == new_erc.SUSPICIOUS_EMPTY


def test_g2_semantic_cache_smoke():
    cache = new_sc.get_semantic_cache()
    assert cache is not None
    scope = new_sc.SemanticCache.build_scope_key("tenant1", db_id="default")
    assert "tenant1" in scope
