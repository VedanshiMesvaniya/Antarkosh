"""Static local policy-procedure mappings: extraction, sync output, and prompt use."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import src.sql.prompt_builder as pb
from src.sql.mining.catalog_index import CatalogIndex
from src.sql.sa import mining_phase as mp
from src.sql.sa.routines import Routine
from src.sql.semantics.policy_mappings import POLICY_MAPPINGS_KEY, extract_policy_mappings

POLICY_SQL = """
CREATE PROCEDURE [dbo].[Policy_GetColumnValueMappings] AS
BEGIN
    SELECT m.table_name, m.column_name, m.stored_value, m.meaning, m.confidence, m.evidence, m.query_hint
    FROM (VALUES
        (N'gen_supplier_master', N'country', N'IND, CAN, DEU',
         N'Three-letter country codes: India = IND, Canada = CAN, Germany = DEU.', N'INFERRED',
         N'Static policy definition', N'Filter with country = ''IND'' for India.'),
        (N'gen_supplier_master', N'active', N'N',
         N'Legacy status value.', N'CONFIRMED', N'Procedure source', N'Use only for an explicit inactive-status request.'),
        (N'gen_supplier_master', N'country', N'(other)',
         N'No value list in the policy.', N'UNRESOLVED', N'none', N'Do not guess.'),
        (N'not_in_catalog', N'country', N'XX',
         N'Must not survive catalog validation.', N'CONFIRMED', N'none', N'Ignore.')
    ) AS m(table_name, column_name, stored_value, meaning, confidence, evidence, query_hint);
END
"""


def _catalog() -> CatalogIndex:
    catalog = CatalogIndex.from_document({
        "default_schema": "dbo",
        "tables": [{
            "name": "gen_supplier_master",
            "qualified_name": "dbo.gen_supplier_master",
            "columns": [{"name": "country"}, {"name": "active"}],
        }],
    })
    assert catalog is not None
    return catalog


def _policy_routine(*, source: str = "folder") -> Routine:
    return Routine(
        schema="",
        name="Policy_SP.sql",
        kind="SCRIPT",
        source=source,
        definition=POLICY_SQL,
        members=("dbo].[Policy_GetColumnValueMappings",),
    )


def test_only_literal_folder_policy_rows_are_parsed_and_catalog_validated():
    rows = extract_policy_mappings([_policy_routine(), _policy_routine(source="database")], _catalog())
    assert [(row["table"], row["column"], row["confidence"]) for row in rows] == [
        ("dbo.gen_supplier_master", "country", "INFERRED"),
        ("dbo.gen_supplier_master", "active", "CONFIRMED"),
        ("dbo.gen_supplier_master", "country", "UNRESOLVED"),
    ]
    assert rows[0]["stored_value"] == "IND, CAN, DEU"
    assert rows[0]["query_hint"] == "Filter with country = 'IND' for India."


@pytest.mark.asyncio
async def test_sync_writes_policy_mappings_to_existing_generated_hints_file(tmp_path: Path):
    db_id = "policy_db"
    schema_dir = tmp_path / db_id / "schema"
    schema_dir.mkdir(parents=True)
    schema_dir.joinpath("schema.json").write_text(json.dumps({
        "default_schema": "dbo",
        "tables": [{
            "name": "gen_supplier_master",
            "qualified_name": "dbo.gen_supplier_master",
            "columns": [{"name": "country"}, {"name": "active"}],
        }],
    }), encoding="utf-8")

    report = await mp.run_mining_phase(
        db_id=db_id,
        engine_key="mssql",
        routines=[_policy_routine()],
        databases_dir=tmp_path,
        collect_hints=False,
    )

    hints = json.loads((schema_dir / "column_hints.json").read_text(encoding="utf-8"))
    assert hints["generated_by"] == "auto_sp_mining"
    assert hints[POLICY_MAPPINGS_KEY][0]["stored_value"] == "IND, CAN, DEU"
    assert hints["stats"]["policy_mappings"] == 3
    assert report["column_hints"]["policy_mappings"] == 3


def test_prompt_uses_relevant_static_mapping_but_not_unresolved_or_unrequested_status(monkeypatch):
    monkeypatch.setattr(pb, "get_raw_column_hints", lambda db_id: {
        POLICY_MAPPINGS_KEY: [
            {
                "table": "dbo.gen_supplier_master", "column": "country", "stored_value": "IND, CAN, DEU",
                "meaning": "Three-letter country codes: India = IND, Canada = CAN, Germany = DEU.",
                "confidence": "INFERRED", "query_hint": "Filter with country = 'IND' for India.",
            },
            {
                "table": "dbo.gen_supplier_master", "column": "active", "stored_value": "N",
                "meaning": "Legacy inactive status.", "confidence": "CONFIRMED",
                "query_hint": "Use only for an explicit inactive-status request.",
            },
            {
                "table": "dbo.gen_supplier_master", "column": "country", "stored_value": "(other)",
                "meaning": "Unknown values.", "confidence": "UNRESOLVED", "query_hint": "Do not guess.",
            },
        ],
    })
    dialect = MagicMock(name="dialect")
    dialect.name = "SQL Server"
    dialect.date_functions = "GETDATE()"
    schema = "gen_supplier_master: supplier_master_id(int), country(varchar), active(char)"

    prompt = pb.build_sql_prompt("show suppliers from India", schema, dialect, db_id="policy_db")

    assert "Static policy mappings (local authored metadata, not live query results)" in prompt
    assert "India = IND" in prompt and "country = 'IND'" in prompt
    assert "UNRESOLVED policy" not in prompt
    assert "Legacy inactive status" not in prompt
    assert "never add an active/status filter unless the user explicitly asks" in prompt
