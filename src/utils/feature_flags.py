"""Feature flag infrastructure for SQL pipeline optimization."""

from __future__ import annotations

import logging
import re

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
    for filename in ("feature_flags.yaml", "features.yaml"):
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
    for filename in ("feature_flags.yaml", "features.yaml"):
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
