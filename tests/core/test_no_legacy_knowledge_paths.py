"""Guard test ensuring no legacy knowledge file paths are referenced in src/

Excludes src/sql/knowledge/loaders.py which retains fallback references until Phase I.
"""

from __future__ import annotations

from src.core.config import PROJECT_ROOT

FORBIDDEN_STRINGS = [
    "sql_glossary.json",
    "sql_column_glossary.json",
    "sql_relationships.json",
    "behavioral_schema_atlas.json",
    "Antarkosh_schema.json",
]

ALLOWED_FILES = {
    (PROJECT_ROOT / "src" / "sql" / "knowledge" / "loaders.py").resolve(),
}


def test_no_legacy_knowledge_paths_in_src():
    src_dir = PROJECT_ROOT / "src"
    assert src_dir.is_dir(), f"src directory not found at {src_dir}"

    violations: list[str] = []

    for py_file in src_dir.rglob("*.py"):
        if py_file.resolve() in ALLOWED_FILES:
            continue

        try:
            content = py_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = py_file.read_text(encoding="latin-1")

        for s in FORBIDDEN_STRINGS:
            if s in content:
                violations.append(f"{py_file.relative_to(PROJECT_ROOT)} contains forbidden string {s!r}")

    assert not violations, (
        f"Found {len(violations)} legacy knowledge path reference(s) in src/:\n"
        + "\n".join(violations)
    )
