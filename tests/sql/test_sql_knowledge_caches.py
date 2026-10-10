"""Tests for per-db_id knowledge caches in s12b_sql_retrieval."""

import json
import shutil
from pathlib import Path

import pytest

from src.core.config import DATABASES_DIR
from src.stages.s12b_sql_retrieval import (
    _get_raw_behavioral_atlas,
    _get_raw_column_glossary,
    _get_raw_relationships,
    _get_tables_with_soft_delete,
    _load_glossary,
    _load_relationships,
)


@pytest.fixture(autouse=True)
def _clear_all_caches():
    """Clear all s12b knowledge caches before and after each test."""
    _get_raw_relationships.cache_clear()
    _load_relationships.cache_clear()
    _load_glossary.cache_clear()
    _get_raw_column_glossary.cache_clear()
    _get_raw_behavioral_atlas.cache_clear()
    _get_tables_with_soft_delete.cache_clear()
    yield
    _get_raw_relationships.cache_clear()
    _load_relationships.cache_clear()
    _load_glossary.cache_clear()
    _get_raw_column_glossary.cache_clear()
    _get_raw_behavioral_atlas.cache_clear()
    _get_tables_with_soft_delete.cache_clear()


def test_erp_main_baseline_golden_snapshot():
    """Verify erp_main returns exact golden metrics and zero-arg matches explicit db_id."""
    # Zero-argument calls
    rels = _get_raw_relationships()
    rels_text = _load_relationships()
    glossary = _load_glossary()
    col_glossary = _get_raw_column_glossary()
    atlas = _get_raw_behavioral_atlas()
    soft_delete = _get_tables_with_soft_delete()

    # Golden snapshot sizes captured prior to change
    assert len(rels) == 294
    assert len(rels_text) == 8303
    assert len(glossary) == 6095
    assert len(col_glossary) == 835
    assert len(atlas) == 2
    assert len(soft_delete) == 40

    # Explicit erp_main calls produce identical results and share cached objects
    assert _get_raw_relationships("erp_main") is rels
    assert _load_relationships("erp_main") == rels_text
    assert _load_glossary("erp_main") == glossary
    assert _get_raw_column_glossary("erp_main") is col_glossary
    assert _get_raw_behavioral_atlas("erp_main") is atlas
    assert _get_tables_with_soft_delete("erp_main") is soft_delete


def test_cache_clear_on_public_names():
    """Verify cache_clear() works as expected on all public names."""
    rels1 = _get_raw_relationships("erp_main")
    _get_raw_relationships.cache_clear()
    rels2 = _get_raw_relationships("erp_main")
    # New object after cache clear, but identical contents
    assert rels1 == rels2

    atlas1 = _get_raw_behavioral_atlas("erp_main")
    _get_raw_behavioral_atlas.cache_clear()
    atlas2 = _get_raw_behavioral_atlas("erp_main")
    assert atlas1 == atlas2

    sd1 = _get_tables_with_soft_delete("erp_main")
    _get_tables_with_soft_delete.cache_clear()
    sd2 = _get_tables_with_soft_delete("erp_main")
    assert sd1 == sd2


def test_two_db_folders_isolation_no_sharing(tmp_path: Path):
    """Verify two databases do not share any knowledge cache entries."""
    db_id_mock = "mock_db_cache_test"
    mock_db_dir = DATABASES_DIR / db_id_mock

    try:
        (mock_db_dir / "schema").mkdir(parents=True, exist_ok=True)
        (mock_db_dir / "semantics").mkdir(parents=True, exist_ok=True)

        # Mock relationships with custom edges
        custom_rels = {
            "relationships": [
                {
                    "from_table": "custom_orders",
                    "from_column": "customer_id",
                    "to_table": "custom_customers",
                    "to_column": "id",
                }
            ]
        }
        (mock_db_dir / "schema" / "relationships.json").write_text(
            json.dumps(custom_rels), encoding="utf-8"
        )

        # Mock glossary
        custom_glossary = {"Client": "custom_customers"}
        (mock_db_dir / "semantics" / "glossary.json").write_text(
            json.dumps(custom_glossary), encoding="utf-8"
        )

        # Mock column glossary
        custom_col_glossary = {"client_name": "custom_customers.name"}
        (mock_db_dir / "semantics" / "column_glossary.json").write_text(
            json.dumps(custom_col_glossary), encoding="utf-8"
        )

        # Mock behavioral atlas
        custom_atlas = {
            "version": 1,
            "tables": {
                "custom_orders": {"columns": {"deleted_at": "datetime"}},
                "custom_customers": {"columns": {"archived": "boolean"}},
            },
        }
        (mock_db_dir / "semantics" / "behavioral_atlas.json").write_text(
            json.dumps(custom_atlas), encoding="utf-8"
        )

        # Load erp_main and mock_db
        erp_rels = _get_raw_relationships("erp_main")
        mock_rels = _get_raw_relationships(db_id_mock)
        assert len(mock_rels) == 1
        assert len(erp_rels) == 294
        assert mock_rels != erp_rels

        erp_rels_txt = _load_relationships("erp_main")
        mock_rels_txt = _load_relationships(db_id_mock)
        assert "custom_orders: customer_id->custom_customers.id" in mock_rels_txt
        assert "sales_order_products" in erp_rels_txt
        assert "sales_order_products" not in mock_rels_txt

        erp_glossary = _load_glossary("erp_main")
        mock_glossary = _load_glossary(db_id_mock)
        assert "- Client: custom_customers" in mock_glossary
        assert "- Client: custom_customers" not in erp_glossary

        erp_col_glossary = _get_raw_column_glossary("erp_main")
        mock_col_glossary = _get_raw_column_glossary(db_id_mock)
        assert "client_name" in mock_col_glossary
        assert "client_name" not in erp_col_glossary

        erp_atlas = _get_raw_behavioral_atlas("erp_main")
        mock_atlas = _get_raw_behavioral_atlas(db_id_mock)
        assert "custom_orders" in mock_atlas.get("tables", {})
        assert "custom_orders" not in erp_atlas.get("tables", {})

        erp_sd = _get_tables_with_soft_delete("erp_main")
        mock_sd = _get_tables_with_soft_delete(db_id_mock)
        assert mock_sd == {"custom_orders"}
        assert len(erp_sd) == 40
        assert mock_sd != erp_sd
    finally:
        if mock_db_dir.exists():
            shutil.rmtree(mock_db_dir, ignore_errors=True)
