"""Feature flag infrastructure for SQL pipeline optimization."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import yaml

from src.core.config import CONFIG_DIR

logger = logging.getLogger(__name__)

DEFAULT_FLAGS: dict[str, bool] = {
    "delta_repair_enabled": False,
    "token_budget_enabled": False,
    "schema_compaction_enabled": False,
    "sql_safety_enabled": False,
    "zero_row_handling_enabled": False,
    "fast_path_enabled": False,
    "sql_micro_synthesis_enabled": False,
    "provider_routing_v2_enabled": False,
    # V1 Measurement & Safety Flags
    "enable_branching_traces": True,
    "guard_sql_safety_enforce": True,
    "guard_sql_soft_delete_enforce": True,
    "guard_sql_temporal_shadow": True,
    "guard_sql_temporal_enforce": False,
    "guard_schema_sufficiency_shadow": True,
    "guard_schema_sufficiency_enforce": False,
    "guard_rag_citation_shadow": True,
    "guard_rag_citation_enforce": True,
    "guard_decomposition_shadow": True,
    "enable_fallback_on_guard_failure": True,
}


_FLAGS_CACHE: dict[str, bool] | None = None
_CACHE_MTIME: float = 0.0


def _load_flags_from_yaml() -> dict[str, bool]:
    """Load flags from feature_flags.yaml or features.yaml if present, cached by mtime."""
    global _FLAGS_CACHE, _CACHE_MTIME
    target_file = None
    for filename in ("feature_flags.yaml", "feature_flags.example.yaml", "features.yaml"):
        file_path = CONFIG_DIR / filename
        if file_path.exists():
            target_file = file_path
            break

    if target_file is None:
        return {}

    try:
        mtime = target_file.stat().st_mtime
        if _FLAGS_CACHE is not None and mtime == _CACHE_MTIME:
            return _FLAGS_CACHE

        with open(target_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                raw_flags = data.get("features", data)
                if isinstance(raw_flags, dict):
                    _FLAGS_CACHE = {k: bool(v) for k, v in raw_flags.items()}
                    _CACHE_MTIME = mtime
                    return _FLAGS_CACHE
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to load %s: %s — falling back to defaults", target_file, e)

    return _FLAGS_CACHE or {}


def is_feature_enabled(flag_name: str) -> bool:
    """Safely check if a feature flag is enabled.

    Defaults to False if the flag does not exist or if configuration cannot be read.
    """
    file_flags = _load_flags_from_yaml()
    if flag_name in file_flags:
        return file_flags[flag_name]
    return DEFAULT_FLAGS.get(flag_name, False)


# ---------------------------------------------------------------------------
# Per-database flags (auto schema intelligence). Read from the top-level
# ``db_features:`` block of config/feature_flags.yaml, which the global loader
# above never touches. Everything defaults to OFF.
# ---------------------------------------------------------------------------

DB_FEATURE_FLAGS: tuple[str, ...] = (
    "auto_scaffold",
    "auto_catalog",
    "auto_sqlalchemy",
    "auto_procedures",
    "auto_sp_mining",
    "auto_relationships",
    "auto_relationships_apply",
    "auto_table_profiles",
    "auto_join_paths",
    "auto_column_meaning",
    "auto_prompt_shadow",
    "auto_prompt_inject",
    "auto_learning",
)

# Databases pinned to their curated files: these flags are always False here,
# whatever the YAML says. Kept local (no import) so this module stays light.
_PINNED_DB_IDS: frozenset[str] = frozenset({"erp_main"})

_DB_FLAGS_CACHE: dict[str, dict[str, bool]] | None = None
_DB_CACHE_MTIME: float = 0.0


def _load_db_flags_from_yaml() -> dict[str, dict[str, bool]]:
    """Return {db_id: {flag: bool}} from ``db_features:`` with ``db_defaults:`` template under '*'; {} on any problem."""
    global _DB_FLAGS_CACHE, _DB_CACHE_MTIME
    target_file = None
    for filename in ("feature_flags.yaml", "feature_flags.example.yaml", "features.yaml"):
        file_path = CONFIG_DIR / filename
        if file_path.exists():
            target_file = file_path
            break

    if target_file is None:
        return {}

    try:
        mtime = target_file.stat().st_mtime
        if _DB_FLAGS_CACHE is not None and mtime == _DB_CACHE_MTIME:
            return _DB_FLAGS_CACHE
        with open(target_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        parsed: dict[str, dict[str, bool]] = {}
        block = data.get("db_features") if isinstance(data, dict) else None
        if isinstance(block, dict):
            for db_id, flags in block.items():
                if isinstance(flags, dict):
                    # Only a literal YAML true counts; "false"/1/"yes" never enable.
                    parsed[str(db_id)] = {str(k): (v is True) for k, v in flags.items()}
        defaults = data.get("db_defaults") if isinstance(data, dict) else None
        if isinstance(defaults, dict):
            # "*" can never be a database id, so it safely holds the defaults for every other database.
            parsed["*"] = {str(k): (v is True) for k, v in defaults.items()}
        _DB_FLAGS_CACHE = parsed
        _DB_CACHE_MTIME = mtime
        return parsed
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to load db_features from %s: %s — all per-DB flags off", target_file, e)
        return {}


def _resolve_db_candidates(db_id: str) -> list[str]:
    """Generate candidate keys in preference order for db_features lookup."""
    candidates = [db_id]

    # 1. Strip numeric duplicate suffix (e.g. qts_ais_2 -> qts_ais)
    m = re.match(r"^(.+)_\d+$", db_id)
    if m:
        candidates.append(m.group(1))

    # 2. Connection metadata lookup (lazy import to keep module lightweight)
    try:
        from src.sql import registry

        cfg = registry.get_connection(db_id)
        raw_db = cfg.get("database") or cfg.get("service_name")
        if raw_db:
            slug = re.sub(r"[^a-z0-9]+", "_", str(raw_db).lower()).strip("_")
            if slug and slug not in candidates:
                candidates.append(slug)
                m2 = re.match(r"^(.+)_\d+$", slug)
                if m2 and m2.group(1) not in candidates:
                    candidates.append(m2.group(1))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not resolve connection metadata for %s: %s", db_id, exc)

    return candidates


def _find_db_flags(all_flags: dict[str, dict[str, bool]], db_id: str) -> dict[str, bool]:
    # 1. Check direct candidates (exact db_id, un-suffixed db_id, connection db name)
    for cand in _resolve_db_candidates(db_id):
        if cand in all_flags and not cand.startswith(("_", "*")):
            return all_flags[cand]

    # 2. Prefix matching against configured keys in db_features (longest prefix wins)
    # E.g. db_id "northwind_orchard_erp" matches YAML key "northwind"
    # E.g. db_id "sakila_db" matches YAML key "sakila"
    prefix_matches = []
    for k, v in all_flags.items():
        if k.startswith(("_", "*")):
            continue
        if db_id.startswith(f"{k}_") or k.startswith(f"{db_id}_"):
            prefix_matches.append((len(k), v))

    if prefix_matches:
        prefix_matches.sort(key=lambda item: item[0], reverse=True)
        return prefix_matches[0][1]

    return {}


def is_db_feature_enabled(flag_name: str, db_id: str | None) -> bool:
    """True only if ``flag_name`` is literally ``true`` for ``db_id`` in the YAML (its own entry under
    ``db_features`` matched by exact id, base id without numeric suffix, connection database name,
    prefix alias, or else the ``db_defaults`` block).

    Unknown flag, unknown/blank db_id, pinned db_id (erp_main), missing or
    unreadable file: all return False.
    """
    if flag_name not in DB_FEATURE_FLAGS:
        return False
    if not isinstance(db_id, str) or not db_id.strip():
        return False
    db_id = db_id.strip()
    if db_id in _PINNED_DB_IDS:
        return False
    all_flags = _load_db_flags_from_yaml()
    own = _find_db_flags(all_flags, db_id)
    if flag_name in own:
        return own[flag_name]
    return all_flags.get("*", {}).get(flag_name, False)  # db_defaults, else off


DEFAULT_NEW_DB_FLAGS: dict[str, bool] = {
    "auto_scaffold": True,
    "auto_catalog": True,
    "auto_sqlalchemy": True,
    "auto_procedures": True,
    "auto_sp_mining": True,
    "auto_relationships": True,
    "auto_relationships_apply": True,
    "auto_table_profiles": True,
    "auto_join_paths": True,
    "auto_column_meaning": True,
    "auto_prompt_shadow": True,
    "auto_prompt_inject": True,
    "auto_learning": True,
}


def register_db_feature_flags(
    db_id: str | None,
    flags: dict[str, bool] | None = None,
    config_dir: Path | None = None,
) -> bool:
    """Automatically ensure a newly connected database has its feature flags configured in feature_flags.yaml.

    All flags default to True for newly connected databases. If db_id is already configured in
    db_features, this function leaves it untouched (never overwrites existing customizations).
    If db_id is pinned (erp_main) or empty, it returns False.
    Returns True if new flags were written to the YAML file.
    """
    global _DB_FLAGS_CACHE, _DB_CACHE_MTIME
    if not isinstance(db_id, str) or not db_id.strip():
        return False
    db_id = db_id.strip()
    if db_id in _PINNED_DB_IDS or db_id.startswith(("_", "*")):
        return False

    base_dir = config_dir or CONFIG_DIR
    target_file = base_dir / "feature_flags.yaml"
    example_file = base_dir / "feature_flags.example.yaml"

    try:
        content = ""
        if target_file.exists():
            content = target_file.read_text(encoding="utf-8")
        elif example_file.exists():
            content = example_file.read_text(encoding="utf-8")

        existing_data = yaml.safe_load(content) if content.strip() else {}
        if not isinstance(existing_data, dict):
            existing_data = {}

        db_features = existing_data.get("db_features")
        if isinstance(db_features, dict) and db_id in db_features:
            return False  # Already registered, do not overwrite

        flags_to_apply = flags or DEFAULT_NEW_DB_FLAGS

        # Format YAML block for db_id
        entry_lines = [f"  {db_id}:"]
        for flag in DB_FEATURE_FLAGS:
            val = "true" if flags_to_apply.get(flag, True) else "false"
            entry_lines.append(f"    {flag}: {val}")
        entry_text = "\n".join(entry_lines) + "\n"

        if "db_features:" in content:
            idx = content.find("db_features:")
            eol = content.find("\n", idx)
            if eol != -1:
                new_content = content[:eol + 1] + entry_text + content[eol + 1:]
            else:
                new_content = content + "\n" + entry_text
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            new_content = content + "\ndb_features:\n" + entry_text

        # Validate that the resulting YAML parses correctly before writing
        verify_parsed = yaml.safe_load(new_content)
        if not isinstance(verify_parsed, dict) or "db_features" not in verify_parsed:
            logger.warning("Generated YAML invalid for %s; aborting write", db_id)
            return False

        # Atomic write
        target_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target_file.parent, prefix="." + target_file.stem + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            try:
                os.chmod(tmp, 0o644)
            except OSError:
                pass
            os.replace(tmp, target_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        _DB_FLAGS_CACHE = None
        _DB_CACHE_MTIME = 0.0
        logger.info("Automatically registered feature flags for new database %s", db_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register feature flags for %s: %s", db_id, exc)
        return False

