"""Compatibility shim for src.core.sql_drift_validator -> src.sql.safety.drift_validator."""

from __future__ import annotations

from src.sql.safety.drift_validator import (
    CONFIG_DIR,
    GLOSSARY_FILE,
    PROJECT_ROOT,
    RELATIONSHIPS_FILE,
    REPO_ROOT,
    SCHEMA_FILE,
    load_schema_tables_and_columns,
    validate_glossary_and_relationships,
    validate_glossary_drift,
    validate_relationships_drift,
)

__all__ = [
    "CONFIG_DIR",
    "GLOSSARY_FILE",
    "PROJECT_ROOT",
    "RELATIONSHIPS_FILE",
    "REPO_ROOT",
    "SCHEMA_FILE",
    "load_schema_tables_and_columns",
    "validate_glossary_and_relationships",
    "validate_glossary_drift",
    "validate_relationships_drift",
]

if __name__ == "__main__":
    errs = validate_glossary_and_relationships()
    if errs:
        print(f"FAILED: Found {len(errs)} drift errors:")
        for err in errs:
            print(f" - {err}")
        raise SystemExit(1)
    else:
        print("OK: Zero drift detected in glossary and relationships!")
