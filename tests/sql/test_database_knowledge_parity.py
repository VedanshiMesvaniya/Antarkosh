"""Test that knowledge files exist in databases/erp_main/ and load cleanly,
and verify knowledge loaders.
"""

from pathlib import Path

import pytest

from src.core.config import DATABASES_DIR
from src.sql.knowledge.loaders import (
    KnowledgeFileNotFound,
    get_database_knowledge_path,
    get_knowledge_path,
    load_knowledge_json,
    validate_db_id,
)

ERP_MAIN_FILES = [
    DATABASES_DIR / "erp_main" / "schema" / "schema.json",
    DATABASES_DIR / "erp_main" / "schema" / "relationships.json",
    DATABASES_DIR / "erp_main" / "semantics" / "glossary.json",
    DATABASES_DIR / "erp_main" / "semantics" / "column_glossary.json",
    DATABASES_DIR / "erp_main" / "semantics" / "behavioral_atlas.json",
    DATABASES_DIR / "erp_main" / "semantics" / "routing_hints.json",
]


@pytest.mark.parametrize("file_path", ERP_MAIN_FILES)
def test_erp_main_knowledge_files_exist_and_load(file_path: Path):
    """All required knowledge files for erp_main must exist, be non-empty, and contain valid JSON."""
    assert file_path.exists(), f"Required knowledge file missing: {file_path}"
    assert file_path.stat().st_size > 0, f"Knowledge file is empty: {file_path}"
    import json

    data = json.loads(file_path.read_text(encoding="utf-8"))
    assert isinstance(data, (dict, list)), f"Expected dict or list in {file_path}, got {type(data)}"


def test_get_database_knowledge_path_resolves_database_dir():
    """get_database_knowledge_path must return the databases/erp_main file when it exists."""
    p = get_database_knowledge_path("erp_main", "schema/schema.json")
    assert p == DATABASES_DIR / "erp_main" / "schema" / "schema.json"
    assert p.exists()


def test_get_database_knowledge_path_non_existent_db_raises():
    """Non-existent database without files must raise KnowledgeFileNotFound, never fall back."""
    with pytest.raises(KnowledgeFileNotFound, match="missing for database"):
        get_database_knowledge_path("non_existent_db", "schema/relationships.json")


def test_get_database_knowledge_path_missing_file_raises():
    """When an erp_main knowledge file is missing, KnowledgeFileNotFound is raised."""
    with pytest.raises(KnowledgeFileNotFound, match="missing for database"):
        get_database_knowledge_path("erp_main", "schema/non_existent.json")


def test_validate_db_id_validation():
    """Valid and invalid db_ids must be correctly accepted or rejected."""
    # Valid IDs
    validate_db_id("erp_main")
    validate_db_id("db2")
    validate_db_id("custom_analytics_db_01")

    # Invalid IDs
    for invalid in ["", "a", "1db", "ERP_MAIN", "db-2", "../x", "a/b", "a" * 41]:
        with pytest.raises(ValueError):
            validate_db_id(invalid)


def test_get_database_knowledge_path_path_traversal_rejected():
    """Relative paths with path traversal or absolute paths must raise ValueError."""
    for bad_path in ["../secret.json", "schema/../../passwords", "/etc/passwd"]:
        with pytest.raises(ValueError):
            get_database_knowledge_path("erp_main", bad_path)


def test_load_knowledge_json_propagates_not_found_for_other_db():
    """load_knowledge_json propagates KnowledgeFileNotFound for non-existent databases."""
    with pytest.raises(KnowledgeFileNotFound):
        load_knowledge_json("relationships", "other_db")


def test_load_knowledge_json():
    """load_knowledge_json loads expected data from new knowledge location."""
    rels = load_knowledge_json("relationships", "erp_main")
    assert isinstance(rels, dict)
    assert "relationships" in rels
    assert len(rels["relationships"]) > 0


def test_load_knowledge_json_all_kinds():
    """All supported knowledge kinds load successfully for erp_main."""
    for kind in ["schema", "relationships", "glossary", "column_glossary", "behavioral_atlas", "routing_hints"]:
        data = load_knowledge_json(kind, "erp_main")
        assert isinstance(data, (dict, list))
        assert len(data) > 0


def test_template_structure():
    """databases/_template must contain db.yaml and subdirectories."""
    template_dir = DATABASES_DIR / "_template"
    assert (template_dir / "db.yaml").exists()
    for sub in ["schema", "semantics", "learned", "evals"]:
        assert (template_dir / sub).is_dir()
        assert (template_dir / sub / ".gitkeep").exists()
