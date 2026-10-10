"""Unit tests verifying Step G2 module migrations and shim re-exports."""

from __future__ import annotations

import pytest

import src.core.pipeline_metrics as shim_pm
import src.sql.learning.pipeline_metrics as new_pm

import src.core.sql_column_registry as shim_cr
import src.sql.safety.column_registry as new_cr

import src.utils.fast_path as shim_fp
import src.sql.fast_path as new_fp

import src.utils.semantic_cache as shim_sc
import src.sql.semantic_cache as new_sc

import src.utils.empty_result_classifier as shim_erc
import src.sql.safety.empty_result_classifier as new_erc

import src.stages.sql_repair as shim_sr
import src.sql.repair.sql_repair as new_sr

import src.prompts.delta_repair as shim_dr
import src.sql.repair.delta_repair as new_dr


def test_g2_shims_object_identity():
    # 1. pipeline_metrics
    assert shim_pm.log_event is new_pm.log_event
    assert shim_pm.CURRENT_DB_ID is new_pm.CURRENT_DB_ID
    assert shim_pm.PipelineEvent is new_pm.PipelineEvent

    # 2. column_registry
    assert shim_cr.ColumnRegistry is new_cr.ColumnRegistry

    # 3. fast_path
    assert shim_fp.fast_path_format is new_fp.fast_path_format
    assert shim_fp.format_list_fast_path is new_fp.format_list_fast_path

    # 4. semantic_cache
    assert shim_sc.SemanticCache is new_sc.SemanticCache
    assert shim_sc.get_semantic_cache is new_sc.get_semantic_cache

    # 5. empty_result_classifier
    assert shim_erc.classify_empty_result is new_erc.classify_empty_result
    assert shim_erc.VALID_EMPTY == new_erc.VALID_EMPTY

    # 6. sql_repair
    assert shim_sr.attempt_delta_repair is new_sr.attempt_delta_repair
    assert shim_sr.MAX_DELTA_REPAIR_ATTEMPTS == new_sr.MAX_DELTA_REPAIR_ATTEMPTS

    # 7. delta_repair
    assert shim_dr.build_delta_repair_payload is new_dr.build_delta_repair_payload
    assert shim_dr.count_tokens is new_dr.count_tokens


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
