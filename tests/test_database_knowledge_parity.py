"""Test byte-identity parity between legacy knowledge files and databases/erp_main/ files,
and verify fallback loaders.
"""

from pathlib import Path
import pytest

from src.core.config import CONFIG_DIR, DATABASES_DIR, PROJECT_ROOT
from src.sql.knowledge.loaders import (
    DEFAULT_DB_ID,
    get_database_knowledge_path,
    get_knowledge_path,
    load_knowledge_json,
)

PAIRS = [
    (
        PROJECT_ROOT / "evals" / "Antarkosh" / "Antarkosh_schema.json",
        DATABASES_DIR / "erp_main" / "schema" / "schema.json",
    ),
    (
        CONFIG_DIR / "sql_relationships.json",
        DATABASES_DIR / "erp_main" / "schema" / "relationships.json",
    ),
    (
        CONFIG_DIR / "sql_glossary.json",
        DATABASES_DIR / "erp_main" / "semantics" / "glossary.json",
    ),
    (
        CONFIG_DIR / "sql_column_glossary.json",
        DATABASES_DIR / "erp_main" / "semantics" / "column_glossary.json",
    ),
    (
        CONFIG_DIR / "behavioral_schema_atlas.json",
        DATABASES_DIR / "erp_main" / "semantics" / "behavioral_atlas.json",
    ),
]


@pytest.mark.parametrize("old_path,new_path", PAIRS)
def test_knowledge_file_byte_identity(old_path: Path, new_path: Path):
    """Old config/evals files and new databases/erp_main/ copies must be byte-identical."""
    assert old_path.exists(), f"Old file missing: {old_path}"
    assert new_path.exists(), f"New file missing: {new_path}"
    assert old_path.read_bytes() == new_path.read_bytes(), (
        f"Byte mismatch between {old_path} and {new_path}"
    )


def test_get_database_knowledge_path_prefers_database_dir():
    """get_database_knowledge_path must return the databases/erp_main file when it exists."""
    p = get_database_knowledge_path("erp_main", "schema/schema.json")
    assert p == DATABASES_DIR / "erp_main" / "schema" / "schema.json"
    assert p.exists()


def test_get_database_knowledge_path_fallback(caplog):
    """When a file in databases/ does not exist, it falls back to legacy path with a warning."""
    with caplog.at_level("WARNING"):
        p = get_database_knowledge_path("non_existent_db", "schema/relationships.json")
    assert p == CONFIG_DIR / "sql_relationships.json"
    assert "falling back to legacy location" in caplog.text


def test_load_knowledge_json():
    """load_knowledge_json loads expected data from new knowledge location."""
    rels = load_knowledge_json("relationships", "erp_main")
    assert isinstance(rels, dict)
    assert "relationships" in rels
    assert len(rels["relationships"]) > 0


def test_template_structure():
    """databases/_template must contain db.yaml and subdirectories."""
    template_dir = DATABASES_DIR / "_template"
    assert (template_dir / "db.yaml").exists()
    for sub in ["schema", "semantics", "learned", "evals"]:
        assert (template_dir / sub).is_dir()
        assert (template_dir / sub / ".gitkeep").exists()
