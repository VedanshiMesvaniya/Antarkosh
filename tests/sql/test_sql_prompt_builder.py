"""Unit and parity tests for prompt builder extracted into src.sql.prompt_builder."""

from unittest.mock import MagicMock

import src.sql.prompt_builder as pb
from src.sql.knowledge.loaders import DEFAULT_DB_ID
from src.sql.prompt_builder import (
    _BEHAVIORAL_ATLAS_CACHE,
    _LOAD_GLOSSARY_CACHE,
    _RAW_RELATIONSHIPS_CACHE,
    build_sql_prompt,
    clear_prompt_caches,
    get_scoped_readability_rules,
    matches_glossary_candidate,
    stem_word,
)


def test_stem_word_rules():
    """Verify stemming logic for glossary candidate matching."""
    assert stem_word("categories") == "category"
    assert stem_word("parties") == "party"
    assert stem_word("boxes") == "box"
    assert stem_word("products") == "product"
    assert stem_word("machines") == "machin"
    assert stem_word("rates") == "rate"  # ends with 'tes', so -s stripped
    assert stem_word("gross") == "gross"  # ends with 'ss', not stripped


def test_matches_glossary_candidate():
    """Verify glossary term matching with query terms and stems."""
    query_lower = "show all pending sales orders"
    words = {"pending", "sales", "orders"}
    stems = {"pend", "sale", "order"}

    assert matches_glossary_candidate("sales order", query_lower, words, stems) is True
    assert matches_glossary_candidate("order", query_lower, words, stems) is True
    assert matches_glossary_candidate("machine", query_lower, words, stems) is False


def test_get_scoped_readability_rules_scopes():
    """Verify that scoped readability rules adapt to soft-delete and table triggers."""
    # Active operational query with DC and Sales Order
    active_rules = get_scoped_readability_rules(
        "What is the due date for delivery challan DC 527?",
        ["delivery_challan", "sales_order", "party"],
    )
    assert "WHERE alias.deleted_at IS NULL" in active_rules
    assert "Delivery Challan (DC) vs Invoice & Due Date" in active_rules
    assert "Customer PO vs Supplier PO" in active_rules

    # Explicit deleted query
    deleted_rules = get_scoped_readability_rules(
        "Show me deleted invoices for party X.",
        ["stock", "purchase", "party"],
    )
    assert "WHERE alias.deleted_at IS NOT NULL" in deleted_rules
    assert "CRITICAL INTENT OVERRIDE: The user explicitly requested DELETED records" in deleted_rules

    # Audit / History query
    history_rules = get_scoped_readability_rules(
        "Give me the audit history of product Y.",
        ["product"],
    )
    assert "DO NOT add `alias.deleted_at IS NULL`" in history_rules
    assert "CRITICAL INTENT OVERRIDE: The user explicitly requested AUDIT / HISTORY" in history_rules


def test_build_sql_prompt_assembly():
    """Verify full prompt construction includes expected sections."""
    dialect_mock = MagicMock()
    dialect_mock.name = "MySQL"
    dialect_mock.date_functions = "CURDATE(), DATE_SUB(CURDATE(), INTERVAL 1 MONTH)"

    schema = (
        "CREATE TABLE product (id INT, product_name VARCHAR, deleted_at TIMESTAMP);\n"
        "CREATE TABLE production (id INT, product_id INT, machine_id INT, qty INT, deleted_at TIMESTAMP);\n"
        "CREATE TABLE machine (id INT, machine_name VARCHAR, deleted_at TIMESTAMP);"
    )

    prompt = build_sql_prompt(
        query="Which machines produced product CAP03?",
        schema=schema,
        dialect=dialect_mock,
        db_id=DEFAULT_DB_ID,
        relationships="- production: product_id->product.id, machine_id->machine.id",
    )

    assert "You are Antarkosh, an expert Enterprise Business Intelligence Agent for MySQL." in prompt
    assert "Extracted Business Intent:" in prompt
    assert "Schema:\n" in prompt
    assert "Table relationships:\n" in prompt
    assert "Date/time syntax for MySQL:\n" in prompt
    assert "CURDATE()" in prompt


def test_cache_clearing_and_isolation():
    """Verify cache clear behaviors per-db and globally."""
    db1 = "test_db_1"
    db2 = "test_db_2"

    _RAW_RELATIONSHIPS_CACHE[db1] = [{"from_table": "t1"}]
    _RAW_RELATIONSHIPS_CACHE[db2] = [{"from_table": "t2"}]
    _LOAD_GLOSSARY_CACHE[db1] = "glossary_1"
    _LOAD_GLOSSARY_CACHE[db2] = "glossary_2"

    assert db1 in _RAW_RELATIONSHIPS_CACHE
    assert db2 in _RAW_RELATIONSHIPS_CACHE

    # Clear db1 only
    clear_prompt_caches(db1)
    assert db1 not in _RAW_RELATIONSHIPS_CACHE
    assert db1 not in _LOAD_GLOSSARY_CACHE
    assert db2 in _RAW_RELATIONSHIPS_CACHE
    assert db2 in _LOAD_GLOSSARY_CACHE

    # Clear globally
    clear_prompt_caches()
    assert len(_RAW_RELATIONSHIPS_CACHE) == 0
    assert len(_LOAD_GLOSSARY_CACHE) == 0


def test_pipeline_clear_knowledge_caches_delegation():
    """Verify that pipeline.clear_knowledge_caches clears prompt caches."""
    from src.sql.pipeline import clear_knowledge_caches

    db_target = "target_test_db"
    _RAW_RELATIONSHIPS_CACHE[db_target] = [{"from": "a"}]
    _LOAD_GLOSSARY_CACHE[db_target] = "glossary_text"
    _BEHAVIORAL_ATLAS_CACHE[db_target] = {"tables": {}}

    clear_knowledge_caches(db_target)

    assert db_target not in _RAW_RELATIONSHIPS_CACHE
    assert db_target not in _LOAD_GLOSSARY_CACHE
    assert db_target not in _BEHAVIORAL_ATLAS_CACHE
