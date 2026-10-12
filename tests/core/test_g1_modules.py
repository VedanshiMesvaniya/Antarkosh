"""Tests for Phase G1 module moves and backward compatibility shims."""

from __future__ import annotations

import src.pipeline.context_gatekeeper as pipe_cg
import src.sql.safety.confidence_scorer as safe_cs
import src.sql.safety.drift_validator as safe_dv
import src.sql.safety.join_graph as safe_jg
import src.sql.safety.sql_privacy as safe_sp


def test_join_graph_builder_functionality():
    """Verify JoinGraphBuilder builds valid joins from schema and relationships."""
    atlas = {
        "tables": {
            "orders": {
                "foreign_keys": [
                    {"column": "customer_id", "references_table": "customers", "references_column": "id"}
                ]
            }
        }
    }
    builder = safe_jg.JoinGraphBuilder(schema_atlas=atlas, relationships=[])
    assert ("orders", "customers") in builder.valid_joins
    assert ("customers", "orders") in builder.valid_joins


def test_context_gatekeeper_functionality():
    """Verify ContextGatekeeper classifies follow-ups and new topics."""
    gatekeeper = pipe_cg.ContextGatekeeper()
    # Explicit reset marker
    action, _ = gatekeeper.classify_query("new question: show all sales")
    assert action == pipe_cg.ContextAction.RESET

    # Follow-up marker with history -> REFINE
    gatekeeper.conversation_history.append(
        pipe_cg.ConversationState(
            previous_query="show sales",
            previous_sql="SELECT * FROM sales",
            previous_filters={},
            previous_tables=["sales"],
            timestamp="2026-01-01",
        )
    )
    action2, _ = gatekeeper.classify_query("what about in North region?")
    assert action2 == pipe_cg.ContextAction.REFINE


def test_sql_privacy_functionality():
    """Verify sanitize_assistant_turn strips raw rows while preserving executed query."""
    text = (
        "SQL Query Executed: `SELECT id, name FROM users`\n\n"
        "| id | name |\n|---|---|\n| 1 | Alice |\n"
    )
    sanitized = safe_sp.sanitize_assistant_turn(text, model_used="sql/direct")
    assert "Alice" not in sanitized
    assert "SELECT id, name FROM users" in sanitized


def test_confidence_scorer_functionality():
    """Verify ConfidenceScorer produces valid weighted breakdown."""
    scorer = safe_cs.ConfidenceScorer()
    breakdown = scorer.calculate(
        pattern_matches=2,
        total_patterns=5,
        validation_results=[],
        reflexion_attempts=0,
    )
    assert 0.0 <= breakdown.final_score <= 1.0
    assert isinstance(breakdown.explanations, list)


def test_drift_validator_functionality():
    """Verify drift_validator runs cleanly against active erp_main schema."""
    errors = safe_dv.validate_glossary_and_relationships()
    assert errors == []
