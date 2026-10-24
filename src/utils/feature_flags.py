"""Feature flag infrastructure for SQL pipeline optimization."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import tempfile
import threading
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


# Every flag in DB_FEATURE_FLAGS is on for a newly connected database. Derived from DB_FEATURE_FLAGS, so a flag added there
# is automatically part of the new-database template and of the backfill below: no second list to forget to update.
DEFAULT_NEW_DB_FLAGS: dict[str, bool] = dict.fromkeys(DB_FEATURE_FLAGS, True)

_REGISTER_LOCK = threading.Lock()
_DB_FEATURES_HEADER = re.compile(r"^db_features[ \t]*:[ \t]*(?:#.*)?$")


def _yaml_key(db_id: str) -> str:
    """``db_id`` as a YAML mapping key that reads back as the same string (``yes``, ``null`` or ``2024`` need quotes)."""
    try:
        if yaml.safe_load(f"{db_id}: 1") == {db_id: 1}:
            return db_id
    except yaml.YAMLError:
        pass
    return json.dumps(db_id)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _edit_text(content: str, db_id: str, add: dict[str, bool], has_features_key: bool) -> str | None:
    """Insert ``add`` (flag -> value) for ``db_id`` into the block-style ``db_features:`` section of ``content``, keeping every
    comment and every other line as it was. Returns None when the file's layout is not plain block style (the caller then falls
    back to a structured rewrite)."""
    lines = content.split("\n")
    header = next((i for i, line in enumerate(lines) if _DB_FEATURES_HEADER.match(line)), None)
    key = _yaml_key(db_id)
    if header is None:
        if has_features_key:  # db_features exists but not as a plain block (flow style, anchor, ...)
            return None
        body = [f"  {key}:", *[f"    {flag}: {'true' if value else 'false'}" for flag, value in add.items()]]
        head = content if content.endswith("\n") or not content else content + "\n"
        return head + ("\n" if head else "") + "\n".join(["db_features:", *body]) + "\n"
    end = next((i for i in range(header + 1, len(lines)) if _is_content(lines[i]) and _indent_of(lines[i]) == 0), len(lines))
    block = [i for i in range(header + 1, end) if _is_content(lines[i])]
    entry_indent = _indent_of(lines[block[0]]) if block else 2
    plain = re.compile(rf"^ {{{entry_indent}}}(?:{re.escape(key)}|\"{re.escape(db_id)}\"|'{re.escape(db_id)}')[ \t]*:[ \t]*(?:#.*)?$")
    at = next((i for i in block if plain.match(lines[i])), None)
    if at is None:
        if any(re.match(rf"^ {{{entry_indent}}}(?:{re.escape(db_id)}|\"{re.escape(db_id)}\"|'{re.escape(db_id)}')[ \t]*:", lines[i]) for i in block):
            return None  # the entry exists but in flow style: {a: true, ...}
        header_line = header  # a new entry goes first in the section
        child = entry_indent + 2
        new = [f"{' ' * entry_indent}{key}:", *[f"{' ' * child}{flag}: {'true' if value else 'false'}" for flag, value in add.items()]]
        lines[header_line + 1 : header_line + 1] = new
        return "\n".join(lines)
    nxt = next((i for i in range(at + 1, end) if _is_content(lines[i])), None)
    child = _indent_of(lines[nxt]) if nxt is not None and _indent_of(lines[nxt]) > entry_indent else entry_indent + 2
    lines[at + 1 : at + 1] = [f"{' ' * child}{flag}: {'true' if value else 'false'}" for flag, value in add.items()]
    return "\n".join(lines)


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix="." + target.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register_db_feature_flags(
    db_id: str | None,
    flags: dict[str, bool] | None = None,
    config_dir: Path | None = None,
) -> bool:
    """Make sure ``db_id`` has every per-database flag in ``feature_flags.yaml`` (the local, git-ignored file).

    * A database with no entry gets a new one with every flag ``true`` (or the values in ``flags``).
    * A database that already has an entry keeps every value it has, however it is set; only flags it does not have yet
      (for example one added to the code later) are added, as ``true``. Nothing the owner set is ever changed.
    * Comments and layout are kept. The edit is checked before it is written: the file must parse to exactly what it
      parsed to before, plus the additions, otherwise nothing is written (a structured rewrite with a ``.bak`` copy is used
      only for layouts a text edit cannot handle, such as ``db_features: {}``).
    * Pinned databases (``erp_main``), ids starting with ``_`` or ``*``, and blank ids are never registered.

    Returns True if the file was written. Cheap when there is nothing to do: one cached lookup, no file read.
    """
    global _DB_FLAGS_CACHE, _DB_CACHE_MTIME, _FLAGS_CACHE, _CACHE_MTIME
    if not isinstance(db_id, str) or not db_id.strip():
        return False
    db_id = db_id.strip()
    if db_id in _PINNED_DB_IDS or db_id.startswith(("_", "*")):
        return False
    wanted = {**DEFAULT_NEW_DB_FLAGS, **(flags or {})}

    if config_dir is None:  # fast path: already complete in the cached file, nothing to read or write
        have = _load_db_flags_from_yaml().get(db_id)
        if have is not None and all(flag in have for flag in DB_FEATURE_FLAGS):
            return False

    base_dir = config_dir or CONFIG_DIR
    target_file = base_dir / "feature_flags.yaml"
    example_file = base_dir / "feature_flags.example.yaml"
    with _REGISTER_LOCK:
        try:
            source = target_file if target_file.exists() else example_file if example_file.exists() else None
            content = source.read_text(encoding="utf-8") if source else ""
            old = yaml.safe_load(content) if content.strip() else {}
            if not isinstance(old, dict):
                old = {}
            section = old.get("db_features")
            section = section if isinstance(section, dict) else {}
            existing_key = next((k for k in section if str(k) == db_id), None)
            entry = section.get(existing_key) if existing_key is not None else None
            if existing_key is not None and entry is not None and not isinstance(entry, dict):
                logger.warning("Feature flags for %s are not a mapping; left untouched", db_id)
                return False
            add = {flag: wanted.get(flag, True) for flag in DB_FEATURE_FLAGS if flag not in (entry or {})}
            if not add and existing_key is not None:
                return False  # complete already (also covers the first read of a local file)

            expected = copy.deepcopy(old)
            expected_section = expected.get("db_features")
            if not isinstance(expected_section, dict):
                expected_section = expected["db_features"] = {}
            expected_section.setdefault(existing_key if existing_key is not None else db_id, {})
            if expected_section[existing_key if existing_key is not None else db_id] is None:
                expected_section[existing_key if existing_key is not None else db_id] = {}
            expected_section[existing_key if existing_key is not None else db_id].update(add)

            new_text = _edit_text(content, db_id, add, "db_features" in old)
            structured = False
            if new_text is not None:
                try:
                    ok = yaml.safe_load(new_text) == expected
                except yaml.YAMLError:
                    ok = False
                if not ok:
                    new_text = None
            if new_text is None:  # a layout a text edit cannot handle: rewrite the structure, after saving a copy
                new_text = yaml.safe_dump(expected, sort_keys=False, allow_unicode=True, default_flow_style=False)
                structured = True
            if structured and target_file.exists():
                shutil.copy2(target_file, target_file.with_name(target_file.name + ".bak"))
            _atomic_write(target_file, new_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not register feature flags for %s: %s", db_id, exc)
            return False
        _DB_FLAGS_CACHE, _DB_CACHE_MTIME = None, 0.0
        _FLAGS_CACHE, _CACHE_MTIME = None, 0.0
    logger.info("Feature flags %s for database %s: %s", "backfilled" if existing_key is not None else "registered", db_id, ", ".join(add))
    return True
