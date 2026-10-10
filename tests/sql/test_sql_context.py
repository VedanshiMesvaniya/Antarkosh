"""Tests for DatabaseContext, get_context, and clear_context_cache."""

import dataclasses
import shutil

import pytest

from src.core.config import DATABASES_DIR
from src.sql.context import (
    DEFAULT_DB_ID,
    DatabaseContext,
    UnknownDatabaseError,
    clear_context_cache,
    get_context,
)
from src.sql.engine import Engine


@pytest.fixture(autouse=True)
def _cleanup_context_cache():
    """Ensure context cache is clean before and after each test."""
    clear_context_cache()
    yield
    clear_context_cache()


def test_erp_main_context_loads():
    """Verify get_context for erp_main loads valid DatabaseContext with expected fields."""
    ctx = get_context("erp_main")

    assert isinstance(ctx, DatabaseContext)
    assert ctx.db_id == "erp_main"
    assert ctx.db_id == DEFAULT_DB_ID
    assert ctx.engine == Engine.MYSQL
    assert ctx.display_name == "ERP (Main)"
    assert "Manufacturing ERP" in ctx.description
    assert ctx.soft_delete_column == "deleted_at"

    # Knowledge paths exist
    assert ctx.schema_path.exists()
    assert ctx.relationships_path.exists()
    assert ctx.glossary_path.exists()
    assert ctx.column_glossary_path.exists()
    assert ctx.atlas_path.exists()
    assert ctx.behavioral_atlas_path == ctx.atlas_path

    # Paths dict property
    assert "schema" in ctx.paths
    assert ctx.paths["schema"] == ctx.schema_path
    assert ctx.paths["relationships"] == ctx.relationships_path
    assert ctx.paths["glossary"] == ctx.glossary_path
    assert ctx.paths["column_glossary"] == ctx.column_glossary_path
    assert ctx.paths["atlas"] == ctx.atlas_path

    # Default call without arguments returns the same instance from cache
    ctx_default = get_context()
    assert ctx_default is ctx


def test_unknown_db_id_raises():
    """Verify get_context raises UnknownDatabaseError/ValueError for unknown or invalid IDs."""
    with pytest.raises(UnknownDatabaseError) as exc_info:
        get_context("nonexistent_db_abc")
    assert "nonexistent_db_abc" in str(exc_info.value)
    # UnknownDatabaseError must be catchable by FileNotFoundError, KeyError, or ValueError
    assert isinstance(exc_info.value, (FileNotFoundError, KeyError, ValueError))

    # Invalid db_id format fails at validate_db_id
    with pytest.raises(ValueError):
        get_context("invalid/path/traversal")
    with pytest.raises(ValueError):
        get_context("123starts_with_digit")
    with pytest.raises(ValueError):
        get_context("")


def test_two_contexts_are_independent_objects():
    """Verify contexts for two distinct databases are separate, independent objects."""
    custom_db_id = "test_custom_db"
    custom_db_dir = DATABASES_DIR / custom_db_id

    try:
        # Create minimal database pack for test_custom_db
        (custom_db_dir / "schema").mkdir(parents=True, exist_ok=True)
        (custom_db_dir / "semantics").mkdir(parents=True, exist_ok=True)

        db_yaml_content = (
            "id: test_custom_db\n"
            "display_name: Custom Secondary DB\n"
            "description: Secondary mock database for isolation test\n"
            "engine: sqlite\n"
            "connection: custom_test\n"
            "enabled: true\n"
            "soft_delete:\n"
            "  column: is_deleted\n"
        )
        (custom_db_dir / "db.yaml").write_text(db_yaml_content, encoding="utf-8")
        (custom_db_dir / "schema" / "schema.json").write_text("{}", encoding="utf-8")
        (custom_db_dir / "schema" / "relationships.json").write_text("{}", encoding="utf-8")
        (custom_db_dir / "semantics" / "glossary.json").write_text("{}", encoding="utf-8")
        (custom_db_dir / "semantics" / "column_glossary.json").write_text("{}", encoding="utf-8")
        (custom_db_dir / "semantics" / "behavioral_atlas.json").write_text("{}", encoding="utf-8")

        ctx_erp = get_context("erp_main")
        ctx_custom = get_context(custom_db_id)

        # Independence checks
        assert ctx_erp is not ctx_custom
        assert ctx_erp != ctx_custom
        assert ctx_erp.db_id == "erp_main"
        assert ctx_custom.db_id == custom_db_id
        assert ctx_erp.engine == Engine.MYSQL
        assert ctx_custom.engine == Engine.SQLITE
        assert ctx_erp.display_name != ctx_custom.display_name
        assert ctx_erp.soft_delete_column == "deleted_at"
        assert ctx_custom.soft_delete_column == "is_deleted"
        assert ctx_erp.schema_path != ctx_custom.schema_path

        # Clearing custom DB cache leaves erp_main cached
        clear_context_cache(custom_db_id)
        assert get_context("erp_main") is ctx_erp
        assert get_context(custom_db_id) is not ctx_custom
    finally:
        if custom_db_dir.exists():
            shutil.rmtree(custom_db_dir, ignore_errors=True)


def test_clear_context_cache_behavior():
    """Verify clear_context_cache with None clears all, while db_id clears selectively."""
    ctx1 = get_context("erp_main")
    assert get_context("erp_main") is ctx1

    clear_context_cache("erp_main")
    ctx2 = get_context("erp_main")
    assert ctx2 is not ctx1
    assert ctx2 == ctx1

    clear_context_cache()
    ctx3 = get_context("erp_main")
    assert ctx3 is not ctx2
    assert ctx3 == ctx1


def test_database_context_is_frozen():
    """Verify DatabaseContext is immutable."""
    ctx = get_context("erp_main")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.db_id = "new_id"  # type: ignore
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.display_name = "New Display Name"  # type: ignore
