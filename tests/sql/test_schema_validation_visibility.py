"""The validator must see every table the prompt shows, in either form (compacted line or raw DDL block).

Reported failure: a query that joined a table shown as ``name: col(type), ...`` with a table whose DDL stayed raw was rejected as
"Table ... does not exist. Available tables: [<only the raw one>]", and the repair step, told that, guessed table names.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.provider_client import ProviderRouter
from src.sql import generation, pipeline
from src.sql.repair.sql_repair import (
    closest_tables,
    describe_missing_table,
    extract_schema_context_from_ddl,
    extract_visible_schema_context,
    missing_table_name,
    repair_context_for_missing_table,
)
from src.sql.schema_compactor import compact_ddl

RAW_TYPES = "TABLE gen_attachment_types (\nattachment_type_id tinyint,\nlabel varchar,\nactive bit\n)"
COMPACT_LEAR = "sqts_main_lear_attachments: lear_attachment_lid(int, PK), file_name(varchar), description(varchar), attachment_type_id(int, FK->gen_attachment_types.attachment_type_id)"
COMPACT_ALERT = "sqts_main_quality_alert: quality_alert_lid(int, PK), title(varchar)"
RAW_JUNCTION = "-- Table: sqts_main_quality_alert_attachments (domain: quality)\nTABLE sqts_main_quality_alert_attachments (\nquality_alert_attachment_lid int,\nsqts_main_quality_alert_lid int,\nsqts_main_lear_attachments_lid int\n)"
JOIN_HINTS = "Join Hints:\n- sqts_main_lear_attachments.attachment_type_id -> gen_attachment_types.attachment_type_id"
MIXED = f"{COMPACT_LEAR}\n\n{COMPACT_ALERT}\n\n{RAW_TYPES}\n\n{RAW_JUNCTION}\n\n{JOIN_HINTS}"


# ================================================================= the parser


def test_compacted_lines_are_read_with_their_columns_and_not_their_foreign_key_targets():
    ctx = extract_visible_schema_context(COMPACT_LEAR + "\n" + COMPACT_ALERT)
    assert ctx == {
        "sqts_main_lear_attachments": ["lear_attachment_lid", "file_name", "description", "attachment_type_id"],
        "sqts_main_quality_alert": ["quality_alert_lid", "title"],
    }
    assert "gen_attachment_types" not in ctx  # FK->gen_attachment_types.attachment_type_id is a reference, not a table of the schema


def test_a_mixed_schema_shows_every_table_in_both_forms_and_ignores_hints_and_comments():
    ctx = extract_visible_schema_context(MIXED)
    assert set(ctx) == {"sqts_main_lear_attachments", "sqts_main_quality_alert", "gen_attachment_types", "sqts_main_quality_alert_attachments"}
    assert ctx["gen_attachment_types"] == ["attachment_type_id", "label", "active"]
    assert ctx["sqts_main_quality_alert_attachments"] == ["quality_alert_attachment_lid", "sqts_main_quality_alert_lid", "sqts_main_lear_attachments_lid"]
    assert not {"join hints", "-", "table"} & set(ctx)


def test_the_old_parser_could_not_see_compacted_tables_and_still_behaves_exactly_as_before():
    assert set(extract_schema_context_from_ddl(MIXED)) == {"gen_attachment_types", "sqts_main_quality_alert_attachments"}


def test_for_a_schema_of_raw_blocks_only_the_new_parser_gives_the_same_answer_as_the_old_one():
    raw = "CREATE TABLE users (\n id INTEGER PRIMARY KEY,\n username VARCHAR(50)\n);\n\nCREATE TABLE orders (\n id INTEGER PRIMARY KEY,\n user_id INTEGER\n);"
    assert extract_visible_schema_context(raw) == extract_schema_context_from_ddl(raw)
    assert extract_visible_schema_context(raw, ["orders"]) == extract_schema_context_from_ddl(raw, ["orders"])


def test_the_relevant_tables_filter_applies_to_both_forms_and_a_raw_block_wins_over_a_line():
    assert set(extract_visible_schema_context(MIXED, ["sqts_main_lear_attachments", "gen_attachment_types"])) == {"sqts_main_lear_attachments", "gen_attachment_types"}
    both = "gen_attachment_types: other_col(int)\n\n" + RAW_TYPES
    assert extract_visible_schema_context(both)["gen_attachment_types"] == ["attachment_type_id", "label", "active"]


def test_odd_input_is_handled_quietly():
    assert extract_visible_schema_context("") == {} and extract_visible_schema_context("no tables here\n- a note: with a colon but no column list") == {}
    assert extract_visible_schema_context("t_empty: (no columns)") == {"t_empty": []}
    assert extract_visible_schema_context("Order_Lines: Line_No(int, PK), Qty(int)") == {"order_lines": ["line_no", "qty"]}


def test_every_table_the_compactor_emits_is_visible_whether_it_compacted_or_fell_back():
    ddls = [
        "TABLE a_plain (\nid int,\nname varchar\n)",
        "-- Table: b_with_header (domain: x)\nTABLE b_with_header (\nid int,\nlabel varchar\n)",  # falls back to raw
        "TABLE c_fk (\nid int,\na_id int,\nFOREIGN KEY (a_id) REFERENCES a_plain(id)\n)",
        "TABLE d_audit (\nid int,\ncreated_at datetime,\nvalue decimal\n)",
    ]
    schema = "\n\n".join(compact_ddl(d, dialect="tsql") for d in ddls)
    ctx = extract_visible_schema_context(schema)
    assert set(ctx) == {"a_plain", "b_with_header", "c_fk", "d_audit"}
    assert ctx["a_plain"] == ["id", "name"] and ctx["b_with_header"] == ["id", "label"] and "a_id" in ctx["c_fk"]
    assert "created_at" not in ctx["d_audit"]  # the compactor drops audit columns; the validator sees what the model sees


# ================================================================= the feedback for a table that really does not exist


AVAILABLE = {"sqts_main_quality_alert_attachments": ["quality_alert_attachment_lid"], "gen_attachment_types": ["attachment_type_id", "label"], "qc_attachments": ["qc_attachment_id", "file_name"], "pr_attachments": ["pr_attachment_id"], "sqts_main_quality_alert": ["quality_alert_lid"]}
ERROR = "Table 'sqts_main_lear_attachments' does not exist in schema. Available tables: ['gen_attachment_types']"


def test_the_message_names_the_closest_real_tables_instead_of_the_tables_the_sql_mentioned():
    msg = describe_missing_table(ERROR, AVAILABLE)
    assert msg.startswith("Table 'sqts_main_lear_attachments' does not exist in the provided schema (5 tables are provided).")
    assert "sqts_main_quality_alert_attachments" in msg and "Available tables: ['gen_attachment_types']" not in msg
    assert len(msg) < 300  # the repair prompt truncates error text at 400 characters
    assert closest_tables("sqts_main_lear_attachments", AVAILABLE)[0] == "sqts_main_quality_alert_attachments"
    assert missing_table_name("Table 'x_y' referenced in CTE 'c' does not exist in schema.") == "x_y" and missing_table_name("other error") is None
    assert describe_missing_table("some other error", AVAILABLE) == "some other error" and describe_missing_table(ERROR, {}) == ERROR


def test_the_repair_step_is_shown_the_real_tables_closest_ones_with_their_columns():
    ctx = repair_context_for_missing_table(ERROR, AVAILABLE, {"gen_attachment_types": ["attachment_type_id", "label"]})
    assert next(iter(ctx)) == "gen_attachment_types" and set(ctx) == set(AVAILABLE)
    assert ctx["sqts_main_quality_alert_attachments"] == ["quality_alert_attachment_lid"]  # the closest table keeps its columns
    big = {f"table_{i}": ["c"] for i in range(80)}
    capped = repair_context_for_missing_table("Table 'table_x' does not exist", big, {})
    assert len(capped) <= 3 + 10  # a large schema never floods the repair prompt


# ================================================================= through the real validate-and-repair loop


@pytest.fixture()
def retriever(monkeypatch):
    r = pipeline.SQLRetriever(router=MagicMock(spec=ProviderRouter), db_id="erp_main")
    monkeypatch.setattr(pipeline.SQLRetriever, "_column_registry", {})  # no full-catalog column check in these tests
    r._router.chat = AsyncMock(return_value="")
    executed: list[str] = []

    async def fake_run(sql, db_id=None):
        executed.append(sql)
        return [{"file_name": "a.pdf", "description": "drawing", "attachment_type": "PDF"}]

    monkeypatch.setattr(generation, "run_readonly_query", fake_run)
    r.executed = executed
    return r


QUESTION = "Show each quality notification attachment with its file name, description, and attachment type."
GOOD_SQL = (
    "SELECT la.file_name, la.description, gat.label AS attachment_type FROM sqts_main_lear_attachments AS la "
    "LEFT JOIN gen_attachment_types AS gat ON la.attachment_type_id = gat.attachment_type_id"
)


@pytest.mark.asyncio
async def test_the_reported_query_that_joins_a_compacted_table_with_a_raw_one_now_runs_without_any_repair(retriever):
    retriever._generate_sql = AsyncMock(return_value=GOOD_SQL)
    chunks = await generation.execute_with_delta_repair(retriever, QUESTION, MIXED)
    assert len(chunks) == 1 and retriever.executed == [GOOD_SQL]
    retriever._router.chat.assert_not_awaited()  # nothing to repair: the query was right


@pytest.mark.asyncio
async def test_a_query_over_raw_tables_only_is_validated_exactly_as_before(retriever):
    retriever._generate_sql = AsyncMock(return_value="SELECT t.label FROM gen_attachment_types AS t")
    assert len(await generation.execute_with_delta_repair(retriever, QUESTION, MIXED)) == 1
    retriever._generate_sql = AsyncMock(return_value="SELECT t.nope FROM gen_attachment_types AS t")
    retriever.executed.clear()
    await generation.execute_with_delta_repair(retriever, QUESTION, MIXED)
    assert "SELECT t.nope" not in " ".join(retriever.executed)  # an unknown column is still stopped


@pytest.mark.asyncio
async def test_when_every_referenced_table_is_compacted_the_unchanged_behaviour_is_kept(retriever):
    """Characterisation, not endorsement: with no raw TABLE block among the referenced tables, table validation has never
    run, and this fix keeps that. (The full-catalog column registry still checks columns in production.)"""
    compact_only = COMPACT_LEAR + "\n\n" + COMPACT_ALERT
    sql = "SELECT la.file_name FROM sqts_main_lear_attachments AS la JOIN ghost_table AS g ON g.id = la.lear_attachment_lid"
    retriever._generate_sql = AsyncMock(return_value=sql)
    await generation.execute_with_delta_repair(retriever, QUESTION, compact_only)
    assert retriever.executed == [sql]


@pytest.mark.asyncio
async def test_a_table_that_really_does_not_exist_is_repaired_with_the_real_table_list(retriever):
    bad = "SELECT a.file_name FROM sqts_main_attachments AS a JOIN gen_attachment_types AS g ON g.attachment_type_id = a.attachment_type_id"
    retriever._generate_sql = AsyncMock(return_value=bad)
    retriever._router.chat = AsyncMock(return_value=f"```sql\n{GOOD_SQL}\n```")
    chunks = await generation.execute_with_delta_repair(retriever, QUESTION, MIXED)
    assert len(chunks) == 1 and retriever.executed == [GOOD_SQL]
    prompt = retriever._router.chat.await_args.kwargs["messages"][1]["content"]
    assert "Table 'sqts_main_attachments' does not exist in the provided schema" in prompt
    assert "sqts_main_lear_attachments" in prompt and "sqts_main_quality_alert" in prompt  # the compacted tables are offered too
    assert "Available tables: ['gen_attachment_types']" not in prompt


@pytest.mark.asyncio
async def test_failures_other_than_a_missing_table_are_repaired_with_the_same_context_as_before(retriever, monkeypatch):
    seen = {}

    async def spy(**kw):
        seen.update(kw)

    monkeypatch.setattr(generation, "attempt_delta_repair", spy)
    retriever._generate_sql = AsyncMock(return_value="SELECT t.nope FROM gen_attachment_types AS t")
    await generation.execute_with_delta_repair(retriever, QUESTION, MIXED)
    assert seen["error_type"] == "column_not_found" and set(seen["schema_context"]) == {"gen_attachment_types"}  # the referenced table only, as before
