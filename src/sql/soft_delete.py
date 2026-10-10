"""Soft-delete intent detection, table resolution, and SQL filter enforcement."""

from __future__ import annotations

import logging
import re

import sqlglot
import sqlglot.expressions as exp

from src.sql.knowledge.loaders import DEFAULT_DB_ID

logger = logging.getLogger(__name__)

# Fallback known tables with deleted_at column for erp_main when behavioral atlas is empty
FALLBACK_SOFT_DELETE_TABLES: set[str] = {
    "actual_production", "category", "color", "delivery_challan",
    "delivery_challan_products", "delivery_dispatch_attachment",
    "denomination_production", "financial_year", "lead", "lead_attachment",
    "lead_history", "lead_interested", "lead_product_sample_detail",
    "machine", "packaging", "packaging_products", "packagings", "party",
    "party_followup_history", "party_opening_balance", "product",
    "product_color", "product_opening_stock", "product_packaging_detail",
    "product_type", "production", "proforma", "proforma_products", "purchase",
    "purchase_attachment", "purchase_products", "quotation",
    "quotation_products", "sales_order", "sales_order_products", "stock",
    "stock_adjustment", "stock_temp", "unit", "warehouse",
}


def detect_soft_delete_intent(query: str) -> str:
    """Detect record status / archival intent from user query:
    - 'DELETED_ONLY': Explicitly requests deleted / removed / dropped / gone items.
    - 'INCLUDE_ARCHIVED': Explicitly requests archived / history / audit / past items (both active and deleted).
    - 'ACTIVE_ONLY': Standard operational query (default) or explicitly asks for active / current / present / live / existing.
    """
    q = query.lower()
    has_deleted = bool(re.search(r"\b(deleted|removed|dropped|gone)\b", q))

    # Exclude relative time windows like "past 30 days", "past 6 months", "past year"
    q_no_time_window = re.sub(
        r"\bpast\s+(?:few\s+)?(?:\d+\s+)?(?:days?|weeks?|months?|quarters?|years?)\b",
        "",
        q,
    )
    has_archival = bool(re.search(r"\b(archived|history|audit|historical|past)\b", q_no_time_window))
    has_active = bool(re.search(r"\b(active|current|present|live|existing)\b", q))

    if has_deleted and has_active:
        return "INCLUDE_ARCHIVED"
    if has_deleted:
        return "DELETED_ONLY"
    if has_archival:
        return "INCLUDE_ARCHIVED"
    return "ACTIVE_ONLY"


_SOFT_DELETE_TABLES_CACHE: dict[str, set[str]] = {}


def _get_tables_with_soft_delete(db_id: str = DEFAULT_DB_ID) -> set[str]:
    """Return the set of lowercase table names that possess a deleted_at column.

    Sourced from behavioral schema atlas via knowledge loader with fallback to known
    soft-delete schema tables.
    """
    norm_id = (db_id or DEFAULT_DB_ID).strip()
    if norm_id in _SOFT_DELETE_TABLES_CACHE:
        return _SOFT_DELETE_TABLES_CACHE[norm_id]

    try:
        from src.stages.s12b_sql_retrieval import _get_raw_behavioral_atlas

        atlas_data = _get_raw_behavioral_atlas(norm_id)
    except (ImportError, AttributeError):
        from src.sql.knowledge.loaders import load_knowledge_json

        atlas_data = load_knowledge_json("behavioral_atlas", db_id=norm_id) or {}

    tables = atlas_data.get("tables", {})
    tables_with_col = {
        t_name.lower()
        for t_name, t_meta in tables.items()
        if "deleted_at" in t_meta.get("columns", {})
    }

    if not tables_with_col and norm_id == DEFAULT_DB_ID:
        tables_with_col = set(FALLBACK_SOFT_DELETE_TABLES)

    _SOFT_DELETE_TABLES_CACHE[norm_id] = tables_with_col
    return _SOFT_DELETE_TABLES_CACHE[norm_id]


def _clear_soft_delete_tables_cache(db_id: str | None = None) -> None:
    if db_id is None:
        _SOFT_DELETE_TABLES_CACHE.clear()
    else:
        _SOFT_DELETE_TABLES_CACHE.pop(db_id, None)


_get_tables_with_soft_delete.cache_clear = _clear_soft_delete_tables_cache  # type: ignore[attr-defined]


def enforce_soft_delete_filter(
    sql: str,
    intent: str,
    dialect: str = "mysql",
    db_id: str = DEFAULT_DB_ID,
) -> str:
    """Post-generation SQL sanitizer enforcing deterministic soft-delete compliance.

    Defense-in-depth:
    - 'DELETED_ONLY': Guarantees `deleted_at IS NOT NULL` is present on target tables.
    - 'INCLUDE_ARCHIVED': Unchanged (allows both active and deleted records).
    - 'ACTIVE_ONLY' (Default): Guarantees `deleted_at IS NULL` on all referenced tables
      having a deleted_at column.
    """
    if not sql or not sql.strip():
        return sql
    if intent == "INCLUDE_ARCHIVED":
        return sql

    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception as e:  # noqa: BLE001
        logger.debug("Failed to parse SQL for soft-delete enforcement: %s", e)
        return sql

    soft_delete_tables = _get_tables_with_soft_delete(db_id)

    for sel in parsed.find_all(exp.Select):
        direct_tables: list[exp.Table] = []
        from_ = sel.args.get("from_")
        if from_ and isinstance(from_.this, exp.Table):
            direct_tables.append(from_.this)
        for join in sel.args.get("joins", []):
            if isinstance(join.this, exp.Table):
                direct_tables.append(join.this)

        target_tables = [t for t in direct_tables if t.name.lower() in soft_delete_tables]
        if not target_tables:
            continue

        where = sel.args.get("where")
        existing_filters: set[tuple[str, str]] = set()
        if where:
            for is_node in where.find_all(exp.Is):
                col = is_node.this
                if (
                    isinstance(col, exp.Column)
                    and col.name.lower() == "deleted_at"
                    and isinstance(is_node.expression, exp.Null)
                ):
                    is_not = isinstance(is_node.parent, exp.Not)
                    tbl_qual = col.table.lower() if col.table else ""
                    existing_filters.add((tbl_qual, "IS_NOT_NULL" if is_not else "IS_NULL"))
                    if intent == "DELETED_ONLY" and not is_not:
                        new_node = sqlglot.parse_one(f"{col.sql()} IS NOT NULL", read=dialect)
                        is_node.replace(new_node)
                        existing_filters.add((tbl_qual, "IS_NOT_NULL"))

        for tbl in target_tables:
            t_name = tbl.name.lower()
            t_alias = tbl.alias.lower() if tbl.alias else ""

            if intent == "DELETED_ONLY":
                has_filter = (
                    (t_alias, "IS_NOT_NULL") in existing_filters
                    or (t_name, "IS_NOT_NULL") in existing_filters
                    or (("", "IS_NOT_NULL") in existing_filters and len(target_tables) == 1)
                )
                if not has_filter:
                    qual = t_alias or (t_name if len(direct_tables) > 1 else "")
                    cond_str = f"{qual}.deleted_at IS NOT NULL" if qual else "deleted_at IS NOT NULL"
                    cond = sqlglot.parse_one(cond_str, read=dialect)
                    existing_where = sel.args.get("where")
                    if existing_where:
                        new_where = exp.Where(this=exp.And(this=existing_where.this, expression=cond))
                    else:
                        new_where = exp.Where(this=cond)
                    sel.set("where", new_where)
                    existing_filters.add((t_alias, "IS_NOT_NULL"))

            elif intent == "ACTIVE_ONLY":
                has_filter = (
                    (t_alias, "IS_NULL") in existing_filters
                    or (t_name, "IS_NULL") in existing_filters
                    or (("", "IS_NULL") in existing_filters and len(target_tables) == 1)
                )
                if not has_filter:
                    qual = t_alias or (t_name if len(direct_tables) > 1 else "")
                    cond_str = f"{qual}.deleted_at IS NULL" if qual else "deleted_at IS NULL"
                    cond = sqlglot.parse_one(cond_str, read=dialect)
                    existing_where = sel.args.get("where")
                    if existing_where:
                        new_where = exp.Where(this=exp.And(this=existing_where.this, expression=cond))
                    else:
                        new_where = exp.Where(this=cond)
                    sel.set("where", new_where)
                    existing_filters.add((t_alias, "IS_NULL"))

    out_sql = parsed.sql(dialect=dialect)
    out_sql = re.sub(r"\bNOT\s+([\w\.]+)\s+IS\s+NULL\b", r"\1 IS NOT NULL", out_sql, flags=re.IGNORECASE)
    return out_sql


__all__ = [
    "FALLBACK_SOFT_DELETE_TABLES",
    "_SOFT_DELETE_TABLES_CACHE",
    "_clear_soft_delete_tables_cache",
    "_get_tables_with_soft_delete",
    "detect_soft_delete_intent",
    "enforce_soft_delete_filter",
]
