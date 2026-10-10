"""Tests for Phase G1 module moves and backward compatibility shims."""

from __future__ import annotations

import src.core.ab_test_engine as shim_ab
import src.core.confidence_scorer as shim_cs
import src.core.context_gatekeeper as shim_cg
import src.core.join_graph as shim_jg
import src.core.pattern_learner as shim_pl
import src.core.result_validator as shim_rv
import src.core.schema_monitor as shim_sm
import src.core.sql_drift_validator as shim_dv
import src.pipeline.context_gatekeeper as pipe_cg
import src.sql.learning.ab_test_engine as learn_ab
import src.sql.learning.pattern_learner as learn_pl
import src.sql.learning.schema_monitor as learn_sm
import src.sql.safety.confidence_scorer as safe_cs
import src.sql.safety.drift_validator as safe_dv
import src.sql.safety.join_graph as safe_jg
import src.sql.safety.result_validator as safe_rv
import src.sql.safety.sql_privacy as safe_sp
import src.utils.sql_privacy as shim_sp


def test_g1_shims_object_identity():
    """All 9 moved modules must preserve strict object identity with their compatibility shims."""
    # 1. join_graph
    assert shim_jg.JoinGraphBuilder is safe_jg.JoinGraphBuilder
    assert shim_jg.ForeignKey is safe_jg.ForeignKey
    assert shim_jg.JoinPath is safe_jg.JoinPath

    # 2. schema_monitor
    assert shim_sm.SchemaMonitor is learn_sm.SchemaMonitor
    assert shim_sm.SchemaDrift is learn_sm.SchemaDrift
    assert shim_sm.SCHEMA_ATLAS_PATH == learn_sm.SCHEMA_ATLAS_PATH
    assert shim_sm.DRIFT_LOG_PATH == learn_sm.DRIFT_LOG_PATH

    # 3. ab_test_engine
    assert shim_ab.ABTestEngine is learn_ab.ABTestEngine
    assert shim_ab.ExperimentEvent is learn_ab.ExperimentEvent

    # 4. context_gatekeeper
    assert shim_cg.ContextGatekeeper is pipe_cg.ContextGatekeeper
    assert shim_cg.ContextAction is pipe_cg.ContextAction
    assert shim_cg.ConversationState is pipe_cg.ConversationState

    # 5. sql_drift_validator
    assert shim_dv.validate_glossary_and_relationships is safe_dv.validate_glossary_and_relationships
    assert shim_dv.validate_glossary_drift is safe_dv.validate_glossary_drift
    assert shim_dv.validate_relationships_drift is safe_dv.validate_relationships_drift
    assert shim_dv.load_schema_tables_and_columns is safe_dv.load_schema_tables_and_columns

    # 6. result_validator
    assert shim_rv.ResultValidator is safe_rv.ResultValidator
    assert shim_rv.ValidationSeverity is safe_rv.ValidationSeverity
    assert shim_rv.ValidationResult is safe_rv.ValidationResult

    # 7. confidence_scorer
    assert shim_cs.ConfidenceScorer is safe_cs.ConfidenceScorer
    assert shim_cs.ConfidenceBreakdown is safe_cs.ConfidenceBreakdown

    # 8. pattern_learner
    assert shim_pl.PatternLearner is learn_pl.PatternLearner
    assert shim_pl.LearnedPattern is learn_pl.LearnedPattern

    # 9. sql_privacy
    assert shim_sp.sanitize_assistant_turn is safe_sp.sanitize_assistant_turn
    assert shim_sp._is_sql_generated is safe_sp._is_sql_generated
    assert shim_sp._SQL_MARKER == safe_sp._SQL_MARKER


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
