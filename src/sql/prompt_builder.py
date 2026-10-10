"""SQL generation prompt builder, domain knowledge loaders, and readability rules.

Constructs comprehensive, token-bounded reasoning prompts for text-to-SQL translation,
including analytical intent, scoped readability protocols, behavioral schema atlas,
table relationships, column glossaries, and dialect date/time rules.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any

from src.sql.intent import extract_analytical_intent
from src.sql.knowledge.loaders import DEFAULT_DB_ID, get_knowledge_path
from src.sql.schema_retrieval import extract_schema_table_names, format_scoped_relationships
from src.sql.soft_delete import detect_soft_delete_intent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-database knowledge caches
# ---------------------------------------------------------------------------

_RAW_RELATIONSHIPS_CACHE: dict[str, list[dict[str, Any]]] = {}
_LOAD_RELATIONSHIPS_CACHE: dict[str, str] = {}
_LOAD_GLOSSARY_CACHE: dict[str, str] = {}
_RAW_COLUMN_GLOSSARY_CACHE: dict[str, dict] = {}
_BEHAVIORAL_ATLAS_CACHE: dict[str, dict[str, Any]] = {}


def get_raw_relationships(db_id: str = DEFAULT_DB_ID) -> list[dict[str, Any]]:
    """Load raw relationship list via knowledge loader for db_id."""
    normalized_id = (db_id or DEFAULT_DB_ID).strip()
    if normalized_id not in _RAW_RELATIONSHIPS_CACHE:
        try:
            path = get_knowledge_path("relationships", db_id=normalized_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            rels = data.get("relationships") if isinstance(data, dict) else data
            _RAW_RELATIONSHIPS_CACHE[normalized_id] = rels if isinstance(rels, list) else []
        except Exception:  # noqa: BLE001
            _RAW_RELATIONSHIPS_CACHE[normalized_id] = []
    return _RAW_RELATIONSHIPS_CACHE[normalized_id]


def clear_raw_relationships_cache(db_id: str | None = None) -> None:
    if db_id is None:
        _RAW_RELATIONSHIPS_CACHE.clear()
    else:
        _RAW_RELATIONSHIPS_CACHE.pop((db_id or DEFAULT_DB_ID).strip(), None)


get_raw_relationships.cache_clear = clear_raw_relationships_cache  # type: ignore[attr-defined]


def load_relationships(db_id: str = DEFAULT_DB_ID) -> str:
    """Load the inferred join map from disk, cached per database."""
    normalized_id = (db_id or DEFAULT_DB_ID).strip()
    if normalized_id not in _LOAD_RELATIONSHIPS_CACHE:
        rels = get_raw_relationships(normalized_id)
        if not rels:
            _LOAD_RELATIONSHIPS_CACHE[normalized_id] = ""
        else:
            grouped: dict[str, list[str]] = {}
            for r in rels:
                frm, fcol = r.get("from_table"), r.get("from_column")
                to, tcol = r.get("to_table"), r.get("to_column")
                if not all((frm, fcol, to, tcol)):
                    continue
                grouped.setdefault(frm, []).append(f"{fcol}->{to}.{tcol}")
            _LOAD_RELATIONSHIPS_CACHE[normalized_id] = "\n".join(
                f"- {table}: {', '.join(edges)}" for table, edges in sorted(grouped.items())
            )
    return _LOAD_RELATIONSHIPS_CACHE[normalized_id]


def clear_relationships_cache(db_id: str | None = None) -> None:
    if db_id is None:
        _LOAD_RELATIONSHIPS_CACHE.clear()
    else:
        _LOAD_RELATIONSHIPS_CACHE.pop((db_id or DEFAULT_DB_ID).strip(), None)


load_relationships.cache_clear = clear_relationships_cache  # type: ignore[attr-defined]


def load_glossary(db_id: str = DEFAULT_DB_ID) -> str:
    """Load SQL glossary from disk, cached per database."""
    normalized_id = (db_id or DEFAULT_DB_ID).strip()
    if normalized_id not in _LOAD_GLOSSARY_CACHE:
        try:
            path = get_knowledge_path("glossary", db_id=normalized_id)
            groups = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(groups, dict) or not groups:
                _LOAD_GLOSSARY_CACHE[normalized_id] = ""
            else:
                lines: list[str] = []
                for concept, syns in groups.items():
                    if isinstance(syns, str):
                        synonym_text = syns
                    elif isinstance(syns, list):
                        synonym_text = ", ".join(str(item) for item in syns if str(item).strip())
                    else:
                        synonym_text = str(syns)

                    synonym_text = synonym_text.strip()
                    if synonym_text:
                        lines.append(f"- {concept}: {synonym_text}")
                _LOAD_GLOSSARY_CACHE[normalized_id] = "\n".join(lines)
        except Exception:  # noqa: BLE001
            _LOAD_GLOSSARY_CACHE[normalized_id] = ""
    return _LOAD_GLOSSARY_CACHE[normalized_id]


def clear_glossary_cache(db_id: str | None = None) -> None:
    if db_id is None:
        _LOAD_GLOSSARY_CACHE.clear()
    else:
        _LOAD_GLOSSARY_CACHE.pop((db_id or DEFAULT_DB_ID).strip(), None)


load_glossary.cache_clear = clear_glossary_cache  # type: ignore[attr-defined]


def get_raw_column_glossary(db_id: str = DEFAULT_DB_ID) -> dict:
    """Load column-mapped glossary dict from disk, cached per database."""
    normalized_id = (db_id or DEFAULT_DB_ID).strip()
    if normalized_id not in _RAW_COLUMN_GLOSSARY_CACHE:
        try:
            path = get_knowledge_path("column_glossary", db_id=normalized_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                _RAW_COLUMN_GLOSSARY_CACHE[normalized_id] = {}
            else:
                _RAW_COLUMN_GLOSSARY_CACHE[normalized_id] = data
        except Exception:  # noqa: BLE001
            _RAW_COLUMN_GLOSSARY_CACHE[normalized_id] = {}
    return _RAW_COLUMN_GLOSSARY_CACHE[normalized_id]


def clear_raw_column_glossary_cache(db_id: str | None = None) -> None:
    if db_id is None:
        _RAW_COLUMN_GLOSSARY_CACHE.clear()
    else:
        _RAW_COLUMN_GLOSSARY_CACHE.pop((db_id or DEFAULT_DB_ID).strip(), None)


get_raw_column_glossary.cache_clear = clear_raw_column_glossary_cache  # type: ignore[attr-defined]


def get_raw_behavioral_atlas(db_id: str = DEFAULT_DB_ID) -> dict[str, Any]:
    """Load behavioral schema atlas dict from disk, cached per database."""
    norm_id = (db_id or DEFAULT_DB_ID).strip()
    if norm_id in _BEHAVIORAL_ATLAS_CACHE:
        return _BEHAVIORAL_ATLAS_CACHE[norm_id]
    try:
        atlas_path = get_knowledge_path("behavioral_atlas", db_id=norm_id)
        if atlas_path.exists():
            with open(atlas_path, "r", encoding="utf-8") as f:
                _BEHAVIORAL_ATLAS_CACHE[norm_id] = json.load(f)
        else:
            _BEHAVIORAL_ATLAS_CACHE[norm_id] = {}
    except Exception:  # noqa: BLE001
        _BEHAVIORAL_ATLAS_CACHE[norm_id] = {}
    return _BEHAVIORAL_ATLAS_CACHE[norm_id]


def clear_behavioral_atlas_cache(db_id: str | None = None) -> None:
    if db_id is None:
        _BEHAVIORAL_ATLAS_CACHE.clear()
    else:
        _BEHAVIORAL_ATLAS_CACHE.pop(db_id, None)


get_raw_behavioral_atlas.cache_clear = clear_behavioral_atlas_cache  # type: ignore[attr-defined]


def clear_prompt_caches(db_id: str | None = None) -> None:
    """Clear all prompt-related knowledge caches for db_id or globally."""
    clear_raw_relationships_cache(db_id)
    clear_relationships_cache(db_id)
    clear_glossary_cache(db_id)
    clear_raw_column_glossary_cache(db_id)
    clear_behavioral_atlas_cache(db_id)


# ---------------------------------------------------------------------------
# Glossary Stemming & Matching
# ---------------------------------------------------------------------------

GLOSSARY_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "of", "for", "in", "on", "at",
    "to", "by", "with", "and", "or", "our", "my", "your", "who", "what", "which",
    "show", "get", "list", "how", "much", "many", "this", "that", "from", "tell",
})


def stem_word(w: str) -> str:
    w = w.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("es") and len(w) > 3 and not w.endswith("tes"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def matches_glossary_candidate(candidate: str, query_lower: str, query_words: set[str], query_stems: set[str]) -> bool:
    cand_lower = candidate.lower().replace("_", " ").strip()
    if not cand_lower:
        return False
    # 1. Exact phrase with word boundaries & optional plural (e.g. "buyer/buyers", "estimate/estimates")
    if re.search(r"\b" + re.escape(cand_lower) + r"(?:s|es|ies)?\b", query_lower):
        return True
    # 2. Check stemmed candidate against query stems
    cand_words = [w for w in re.findall(r"\w+", cand_lower) if w not in GLOSSARY_STOP_WORDS]
    if not cand_words:
        return False
    if len(cand_words) == 1:
        w = cand_words[0]
        if w in query_words or stem_word(w) in query_stems:
            return True
    else:
        # Multi-word candidate: all significant stems must match
        cand_stems = {stem_word(w) for w in cand_words}
        if cand_stems.issubset(query_stems):
            return True
    return False


def build_column_glossary_for_query(query: str, db_id: str = DEFAULT_DB_ID) -> str:
    """Filter the glossary to only include terms/synonyms relevant to the query to save tokens."""
    data = get_raw_column_glossary(db_id)
    if not data:
        return ""

    query_lower = query.lower()
    raw_words = set(re.findall(r"\w+", query_lower)) - GLOSSARY_STOP_WORDS
    query_stems = {stem_word(w) for w in raw_words}

    lines: list[str] = []
    for term, details in data.items():
        if not isinstance(details, dict):
            continue
        candidates = [term] + details.get("synonyms", [])
        if any(matches_glossary_candidate(c, query_lower, raw_words, query_stems) for c in candidates if isinstance(c, str)):
            maps_to = details.get("maps_to")
            if maps_to:
                note = details.get("note", "")
                note_str = f" ({note})" if note else ""
                lines.append(f'- "{term}" → {maps_to}{note_str}')
                if len(lines) >= 15:
                    break

    return "\n".join(lines)


def build_behavioral_atlas_for_query(schema_tables: set[str], query: str, db_id: str = DEFAULT_DB_ID) -> str:
    """Extract dynamically filtered behavioral rules and formulas for active tables."""
    atlas_data = get_raw_behavioral_atlas(db_id)
    if not atlas_data or "tables" not in atlas_data:
        return ""

    tables = atlas_data["tables"]
    lines: list[str] = []

    soft_intent = detect_soft_delete_intent(query)

    # Cap to at most 4 active tables to strictly avoid LLM payload/TPM limits (under 8000 TPM)
    active_tables = sorted(schema_tables)[:4]

    for t_name in active_tables:
        if t_name not in tables:
            continue
        t_data = tables[t_name]
        lines.append(f"### Table `{t_name}`: {t_data.get('table_meaning', '')}")
        for r in t_data.get("table_behavioral_rules", [])[:2]:
            if "deleted_at" in r.lower():
                if soft_intent == "DELETED_ONLY":
                    r = re.sub(r"alias\.deleted_at\s+IS\s+NULL", f"{t_name}.deleted_at IS NOT NULL", r, flags=re.IGNORECASE)
                    r = re.sub(r"\bdeleted_at\s+IS\s+NULL\b", "deleted_at IS NOT NULL", r, flags=re.IGNORECASE)
                    r = re.sub(r"exclude soft-deleted", "include only soft-deleted", r, flags=re.IGNORECASE)
                    r += " (OVERRIDE: User explicitly requested DELETED records; use IS NOT NULL)"
                elif soft_intent == "INCLUDE_ARCHIVED":
                    r = f"Omit `{t_name}.deleted_at` filter to return all records (both active and deleted/historical)."
            lines.append(f"  - Rule: {r}")
        for w in t_data.get("join_warnings", [])[:1]:
            lines.append(f"  - ⚠️ Warning: {w}")

        # List columns with rules or formulas (max 4 per table)
        col_count = 0
        for c_name, c_data in t_data.get("columns", {}).items():
            c_rules = list(c_data.get("behavioral_rules", []))
            formula = c_data.get("aggregation_formula")
            c_warns = c_data.get("join_warnings", [])
            if c_name == "deleted_at":
                if soft_intent == "DELETED_ONLY":
                    c_rules = ["Filter WHERE deleted_at IS NOT NULL when targeting deleted records."]
                elif soft_intent == "INCLUDE_ARCHIVED":
                    c_rules = ["Omit deleted_at filter to return both active and historical/deleted records."]
            if c_rules or formula or c_warns:
                parts = []
                if c_rules:
                    parts.append(" | ".join(c_rules[:2]))
                if c_warns:
                    parts.append("⚠️ " + " | ".join(c_warns[:1]))
                if formula:
                    parts.append(f"Formula: `{formula}`")
                lines.append(f"  - `{t_name}.{c_name}` ({c_data.get('type', 'VARCHAR')}): {'; '.join(parts)}")
                col_count += 1
                if col_count >= 4:
                    break
        lines.append("")

    return "\n".join(lines)


def get_scoped_readability_rules(query: str, schema_tables: list[str]) -> str:
    """Dynamically scope readability rules to only those relevant to the query & schema tables,
    reducing SQL prompt tokens by 60% while preserving all critical schema nuances.
    """
    q = query.lower()
    tables = {t.lower() for t in schema_tables}

    soft_intent = detect_soft_delete_intent(query)
    if soft_intent == "DELETED_ONLY":
        soft_delete_rule = (
            "- Dynamic Soft-Delete Rule (EXPLICIT DELETED QUERY): The user is explicitly requesting DELETED or REMOVED records! "
            "DO NOT add `alias.deleted_at IS NULL`. Instead, add `WHERE alias.deleted_at IS NOT NULL` on the target table(s) with deleted_at. "
            "This strictly overrides any default `deleted_at IS NULL` rules or example templates below."
        )
    elif soft_intent == "INCLUDE_ARCHIVED":
        soft_delete_rule = (
            "- Dynamic Soft-Delete Rule (AUDIT / HISTORY QUERY): The user is querying audit history, past records, or archived data! "
            "DO NOT add `alias.deleted_at IS NULL`. Return ALL records (both active and deleted) without soft-delete restriction. "
            "This strictly overrides any default `deleted_at IS NULL` rules or example templates below."
        )
    else:
        soft_delete_rule = (
            "- Dynamic Soft-Delete Rule: For standard operational queries (active/current data), ALWAYS filter soft-deleted records: "
            "WHERE alias.deleted_at IS NULL on all tables with deleted_at."
        )

    rules: list[str] = [
        "Core SQL Generation & Schema Mapping Protocol:",
        "- Primary Key & Column Projection: For non-aggregate record queries, ALWAYS include the primary key column (e.g. table.id AS id) as the first selected column to serve as an anchor reference point, unless explicitly excluded by the user. Do not alias it confusingly (use alias.id AS id, not alias.id AS something_id unless requested). Follow the ID with only the specific columns or metrics asked by the user. Never bloat results with unsolicited columns (e.g. status, created_at, deleted_at).",
        "- Deduplication (DISTINCT): When looking up entity names, machine names, warehouses, or customer/vendor names from transactional or production tables (e.g. 'which machines produce product X', 'machines for product Y'), ALWAYS use SELECT DISTINCT (e.g. SELECT DISTINCT m.machine_name) or GROUP BY so that all unique entities appear within the row limit instead of repeating the same entity multiple times.",
        "- SELECT read-only queries only. Never return raw ID columns without their human-readable name (use AS descriptive_alias).",
        soft_delete_rule,
        "- Status flags: party.status, product.status, category.status use 'Y'/'N'. Stock booked='B', dispatched='D'.",
        "- Fuzzy LIKE Filtering: Always filter descriptive text columns (categories, products, colors, names) using `LIKE '%<term>%'` rather than strict `=`. For categories with spelling variations like 'CHANGABLE PACK', match `c.category_name LIKE '%CHANG%PACK%'` (the database category is 'CHANGEABLE PACK').",
        "- Temporal Scope & Financial Year Filtering: ONLY add `financial_year.current_year = 'Y'` if the user query contains explicit temporal markers such as 'current', 'this year', 'latest', 'active', or 'ongoing'. If the query asks for 'total', 'all', 'history', or implies a cumulative sum without a time qualifier (e.g. 'quantity adjusted', 'total sales', 'overall quantity'), DO NOT filter by current_year. Sum across all available years. When the user DOES explicitly request the current financial year, NEVER filter using `YEAR(date) = YEAR(CURDATE())`; ALWAYS join `financial_year fy ON t.financial_id = fy.id` (or `WHERE t.financial_id = (SELECT id FROM financial_year WHERE current_year = 'Y')`) with `fy.current_year = 'Y'`.",
    ]

    # Machine & Product Production
    if (
        "machine" in tables
        or "production" in tables
        or "actual_production" in tables
        or any(k in q for k in ["machine", "produce", "production", "batch", "apq", "ppq"])
    ):
        rules.append(
            "- Production Quantity & Batches: In the `production` table, the primary production/batch quantity is stored in `production.qty`. "
            "When asked for the production quantity, batch quantity, or quantity produced for a batch or product, ALWAYS select `production.qty AS production_quantity` (or `SUM(prd.qty)`). "
            "NEVER select `actual_production.apq` as the default production quantity, because `actual_production.apq` is unpopulated or 0 for many batches (which causes queries to return 0), while `production.qty` contains the true quantity. "
            "Only query `actual_production.apq` if the user explicitly asks for 'actual production quantity' or 'APQ' compared to planned targets."
        )
        rules.append(
            "- Machine & Product Production: Product names/codes (e.g. 'CAP03', 'CHP06070110-INNER') are stored in `product.product_name` (NEVER in `production.batch_no` or `stock.batch_no`). "
            "To find which machine was used to create or produce a product, ALWAYS join: `production prd JOIN product p ON prd.product_id = p.id JOIN machine m ON prd.machine_id = m.id WHERE p.product_name LIKE '%<product_name>%' AND prd.deleted_at IS NULL AND m.deleted_at IS NULL`. Return `SELECT DISTINCT m.machine_name`."
        )
    if any(k in q for k in ["invoice", "gt/", "pi_no", "pi number", "purchase invoice"]):
        if soft_intent == "DELETED_ONLY":
            rules.append(
                "- Invoices & Purchase Invoices (PI) [DELETED RECORDS]: Purchase invoice numbers are stored in `purchase.pi_no` (and invoices in `stock` where `stock_type = 'PI'`). "
                "For DELETED invoices, query `purchase pur JOIN party p ON pur.party_id = p.id WHERE pur.deleted_at IS NOT NULL AND p.deleted_at IS NULL` (or `stock s JOIN party p ON s.party_id = p.id WHERE s.stock_type = 'PI' AND s.deleted_at IS NOT NULL AND p.deleted_at IS NULL`). "
                "DO NOT filter `pur.deleted_at IS NULL` or `s.deleted_at IS NULL`!"
            )
        elif soft_intent == "INCLUDE_ARCHIVED":
            rules.append(
                "- Invoices & Purchase Invoices (PI) [HISTORY/AUDIT]: Query `purchase pur JOIN party p ON pur.party_id = p.id` (or `stock s JOIN party p ON s.party_id = p.id WHERE s.stock_type = 'PI'`) without `deleted_at IS NULL` on invoice tables to include all historical records."
            )
        else:
            rules.append(
                "- Invoices & Purchase Invoices (PI): Purchase invoice numbers (e.g. 'GT/0091', 'PI-...') are stored in the `purchase` table with column `purchase.pi_no`. "
                "To find the party or details for an invoice like 'GT/0091', ALWAYS query: `purchase pur JOIN party p ON pur.party_id = p.id WHERE pur.pi_no LIKE '%<invoice_no>%' AND pur.deleted_at IS NULL AND p.deleted_at IS NULL`. Return `p.party_name AS customer_or_supplier_name`."
            )

    # Product Units of Measure
    if "unit" in tables or any(k in q for k in ["unit", "uom", "measurement"]):
        rules.append(
            "- Product Units of Measure: The unit table contains unit definitions ('Pcs', 'Kg', 'Nos', etc.) and NEVER contains product names. "
            "To find the unit for a product (e.g. 'CAP03'), ALWAYS query: `product p JOIN unit u ON p.unit_id = u.id WHERE p.product_name LIKE '%<product>%' AND p.deleted_at IS NULL AND u.deleted_at IS NULL`. "
            "Return `p.product_name` and `u.unit_name AS unit_of_measure`. Never search `unit.unit_name` for product names."
        )

    # Product Color Linkage
    if "product_color" in tables or any(k in q for k in ["color", "colour"]):
        rules.append(
            "- Product Color Linkage: In `production`, `actual_production`, `stock`, and `sales_order_products`, the `product_color_id` column links directly to `product_color.id` (table `product_color`, column `product_color.color`) — NOT to the `color` table! "
            "ALWAYS join `product_color pc ON prd.product_color_id = pc.id` and select `pc.color AS color`. NEVER join with the `color` table; `color` is a separate master list whose IDs do not match `product_color_id`, which causes completely wrong colors to be returned."
        )

    # Product Type vs Category
    if ("product_type" in tables or "category" in tables) and any(k in q for k in ["type", "category", "raw material", "finished good", "carton"]):
        rules.append(
            "- Product Type vs Category: There are two places with product type: (1) `category.product_type` stores enum `'RM'` (Raw Material). "
            "(2) `product_type.product_type` stores text `'Raw Material'` (id=1) and `'Finished Goods'` (id=2). When querying products by category (e.g. 'Carton') and product type ('Raw Material'), ALWAYS include BOTH filters: `product p JOIN category c ON p.category_id = c.id WHERE c.category_name LIKE '%Carton%' AND (c.product_type = 'RM' OR p.product_type_id = 1)`. Never omit the category filter, and never compare `category.product_type = 'Raw Material'` directly (use `'RM'`)."
        )

    # Warehouse & Packaging
    if "packagings" in tables or "warehouse" in tables or any(k in q for k in ["warehouse", "carton", "bin", "rack", "stored", "storage", "location"]):
        rules.append(
            "- Warehouse & Packaging: Warehouse Identification, Carton Count vs Carton Quantity:\n"
            "  (1) Warehouse Identification: Warehouse codes (e.g. 'pm2bzd19') and warehouse names/numbers (e.g. '110') are stored in the `warehouse` table: `warehouse.warehouse_code` and `warehouse.location_name`. When matching a warehouse, ALWAYS join `warehouse w ON pk.warehouse_id = w.id` and filter `(w.warehouse_code = '<code_or_name>' OR w.location_name = '<code_or_name>')`. NEVER search `packagings.location_code` for warehouse codes (`packagings.location_code` only stores internal bin/rack shelf codes).\n"
            "  (2) Carton Count vs Carton Quantity: In the `packagings` table, each row represents ONE physical carton box. `COUNT(pk.id)` or `COUNT(*)` returns the number of carton boxes (e.g. 412 cartons). `pk.qty` stores the number of product units inside each carton. Therefore, when asked for 'carton quantity', 'total carton quantity', 'quantity in cartons', or 'stock quantity in warehouse', ALWAYS use `SUM(pk.qty) AS total_carton_quantity` (e.g. 152,354 units), NEVER `COUNT(*)`! You may return both: `COUNT(pk.id) AS total_cartons, SUM(pk.qty) AS total_carton_quantity`.\n"
            "  (3) Product Storage Locations: To find where a product is stored or what warehouse/bin it is in, join `packagings pk JOIN warehouse w ON pk.warehouse_id = w.id JOIN product p ON pk.product_id = p.id WHERE p.product_name LIKE '%<term>%'`. Return `p.product_name`, `w.warehouse_code`, `w.location_name AS warehouse_location`, `pk.location_code AS bin_location`, `pk.carton_no`, `pk.qty AS carton_qty`."
        )

    # Stock Adjustments
    if "stock_adjustment" in tables or any(k in q for k in ["adjustment", "adjust", "stock-out", "stock out", "stockout", "stock-in", "stock in", "stockin"]):
        rules.append(
            "- Stock Adjustments (StockOut vs StockIn): All inventory stock adjustments (stock-in additions, stock-out write-offs, physical count adjustments) are stored in the dedicated `stock_adjustment` table:\n"
            "  (1) Table Selection: ALWAYS use `stock_adjustment` when asked about stock adjustments, stock-out, stock-in, or adjusted quantity. NEVER use `stock` (which is for purchase inward and sales dispatches) and NEVER use `product_packaging_detail` (which is a packaging BOM master table).\n"
            "  (2) Transaction Type Enum: `stock_adjustment.transaction_type` has ONLY TWO exact enum values: `'StockOut'` (stock reduction / outward adjustment) and `'StockIn'` (stock addition / inward adjustment). NEVER use `'OUT'`, `'IN'`, `'Stock-Out'`, `'STOCK_OUT'`, or lowercase strings. For stock-out queries, filter `sa.transaction_type = 'StockOut'`. For stock-in queries, filter `sa.transaction_type = 'StockIn'`.\n"
            "  (3) Columns & Direct Foreign Keys: Adjustment Date `sa.stock_adjustment_date`, Adjusted Quantity `sa.qty` (or `SUM(sa.qty) AS total_adjusted_quantity`), Category Link `JOIN category c ON sa.category_id = c.id`, Product Link `JOIN product p ON sa.product_id = p.id`, Color Link `JOIN product_color pc ON sa.product_color_id = pc.id`.\n"
            "  (4) All-Time vs Current Year: For 'quantity adjusted' or 'total adjusted quantity' without an explicit year qualifier, DO NOT join financial_year and DO NOT filter current_year = 'Y'! Sum across all records: `SELECT SUM(sa.qty) AS total_adjusted_quantity FROM stock_adjustment sa JOIN product p ON sa.product_id = p.id WHERE sa.deleted_at IS NULL AND p.deleted_at IS NULL AND p.product_name LIKE '%<product>%'`."
        )

    # Delivery Challan & Pending Sales Orders
    if "delivery_challan" in tables or "delivery_challan_products" in tables or any(k in q for k in ["challan", "delivery", "dc", "dc_no", "shipment", "transporter", "lr"]):
        rules.append(
            "- Delivery Challan (DC) vs Invoice & Due Date: A Delivery Challan (DC) and an Invoice are completely separate documents! Actual DC numbers and dates are stored in the `delivery_challan` table: `dc.dc_no` (DC number) and `dc.dc_date` (DC date). Logistics columns: `dc.transport_name` (carrier name) and `dc.lr_number` (Lorry Receipt / LR number — NOT `lr_no`). The customer/party is linked directly via `delivery_challan.party_id = party.id`. NEVER search for DC numbers in `stock.invoice_no` or `stock`! IMPORTANT: `delivery_challan` has NO due date column; the order due date is stored in `sales_order.so_due_date`. When a query asks for the due date of a DC, you MUST join `sales_order`: `LEFT JOIN sales_order so ON dc.sales_order_id = so.id` and select `so.so_due_date AS due_date`."
        )
        rules.append(
            "- Delivery Challan & Pending Sales Orders: To find Sales Orders with pending/undelivered quantity for delivery challan creation, query: "
            "SELECT so.sales_order_no AS sales_order_number, so.sales_order_date AS order_date, p.party_name AS customer_name, pr.product_name AS product_name, sop.qty AS ordered_quantity, COALESCE(SUM(dcp.qty), 0) AS delivered_quantity, (sop.qty - COALESCE(SUM(dcp.qty), 0)) AS pending_quantity FROM sales_order so JOIN sales_order_products sop ON so.id = sop.sales_order_id JOIN party p ON so.party_id = p.id JOIN product pr ON sop.product_id = pr.id LEFT JOIN delivery_challan dc ON so.id = dc.sales_order_id AND dc.deleted_at IS NULL LEFT JOIN delivery_challan_products dcp ON dc.id = dcp.dc_id AND dcp.product_id = sop.product_id AND dcp.deleted_at IS NULL WHERE so.deleted_at IS NULL AND sop.deleted_at IS NULL AND p.deleted_at IS NULL AND pr.deleted_at IS NULL AND p.status = 'Y' GROUP BY so.sales_order_no, so.sales_order_date, p.party_name, pr.product_name, sop.qty HAVING pending_quantity > 0 ORDER BY so.sales_order_no, pr.product_name;"
        )

    # Sales Order & PO
    if "sales_order" in tables or "purchase" in tables or any(k in q for k in ["sales_order", "order", "party_po_no", "po", "due date"]):
        rules.append(
            "- Customer PO vs Supplier PO vs Proforma PO: PO numbers exist in 3 distinct places: (1) Customer/Party PO: `sales_order.party_po_no` (and `sales_order.party_po_date`). For questions asking for 'party's PO number', 'customer PO', or 'PO number for sales order/party', ALWAYS query `sales_order so JOIN party p ON so.party_id = p.id`. (2) Proforma PO: `proforma.po_no` (only for proforma invoice questions). (3) Supplier/Vendor PO: `purchase.ref_po_no` (only for supplier inward purchase orders). NEVER use `purchase.ref_po_no` for customer/party PO requests."
        )
        rules.append(
            "- Blocked Cartons: In `stock`, `party_id` is NULL. Join party through `sales_order`: `stock s JOIN sales_order so ON s.so_id = so.id JOIN party p ON so.party_id = p.id WHERE s.status = 'B'`."
        )

    # Invoices vs Proforma
    if "stock" in tables or "proforma" in tables or any(k in q for k in ["invoice", "invoices", "invoice_no", "proforma", "pi"]):
        if soft_intent == "DELETED_ONLY":
            rules.append(
                "- Invoices vs Proforma [DELETED RECORDS]: Actual invoice details are stored in the `stock` table where `stock.stock_type = 'PI'`, or in `purchase` for purchase invoices. "
                "For questions asking about DELETED invoices: (1) Query `stock s JOIN party p ON s.party_id = p.id WHERE s.stock_type = 'PI' AND s.deleted_at IS NOT NULL AND p.deleted_at IS NULL` (or from `purchase pur JOIN party p ON pur.party_id = p.id WHERE pur.deleted_at IS NOT NULL AND p.deleted_at IS NULL`). "
                "(2) DO NOT add `deleted_at IS NULL` on the invoice table; use `deleted_at IS NOT NULL`!"
            )
        elif soft_intent == "INCLUDE_ARCHIVED":
            rules.append(
                "- Invoices vs Proforma [HISTORY/AUDIT]: Actual invoice details are in `stock` (where `stock_type = 'PI'`) or `purchase`. "
                "For history/audit queries, omit `deleted_at IS NULL` on invoice tables to include all historical records."
            )
        else:
            rules.append(
                "- Invoices vs Proforma: Actual invoice details (numbers, dates, parties) are stored in the `stock` table where `stock.stock_type = 'PI'`, NOT in the `proforma` table! For questions asking about invoices, invoice lists, or invoice counts: (1) Query `stock s JOIN party p ON s.party_id = p.id WHERE s.stock_type = 'PI' AND s.deleted_at IS NULL AND p.deleted_at IS NULL`. (2) When `stock_type = 'PI'`, `s.party_id` connects DIRECTLY to `party.id` (do NOT route through sales_order). (3) Always filter `s.stock_type = 'PI'`. (4) Calculate invoice count as `COUNT(DISTINCT s.invoice_no)`. Only query `proforma` table if user explicitly specifies 'proforma'."
            )

    # Party & Leads
    if "party" in tables or "lead" in tables or any(k in q for k in ["party", "customer", "supplier", "vendor", "contact", "lead", "inquiry"]):
        rules.append(
            "- In party table, customer/supplier name is `party.party_name` (NEVER party.name). Contact persons are `party.contact_person1`.\n"
            "- In lead table, search `(lead.contact_name LIKE '%<name>%' OR lead.company_name LIKE '%<name>%')`.\n"
            "- Unified Contact Search: For generic contact info without 'lead'/'customer', UNION ALL across party and lead."
        )

    # Document Number Uniqueness Across Financial Years
    if any(t in tables for t in ["delivery_challan", "sales_order", "purchase", "proforma", "production"]) or any(k in q for k in ["dc_no", "so_no", "order_no", "number", "latest"]):
        rules.append(
            "- Document Number Uniqueness Across Financial Years (DC, Sales Order, PO, etc.): Document numbers (`dc_no`, `sales_order_no`, `purchase_no`, `proforma_no`, `production_no`) are NOT globally unique; they repeat across different financial years! "
            "If a financial year is specified, join `financial_year fy ON t.financial_id = fy.id`. If NO financial year is specified for a SINGLE DOCUMENT LOOKUP: the user intends the LATEST record! Sort by date DESC with `LIMIT 1` and include `fy.fyear AS financial_year` in SELECT. Do NOT apply LIMIT 1 or current_year filtering to cumulative/aggregate queries."
        )

    # Multi-domain Report
    if any(k in q for k in ["report", "summary", "combined", "ppq", "apq"]):
        rules.append(
            "- Combined Production, Stock & Sales Order Report: When queried for a multi-domain report (PPQ, APQ, Stock, Pending SOs) grouped by Category, Product, Color, use CTE subqueries aggregated per `(product_id, product_color_id)` before joining to `product p`."
        )

    if soft_intent == "DELETED_ONLY":
        rules.append(
            "- ⚠️ CRITICAL INTENT OVERRIDE: The user explicitly requested DELETED records. "
            "In your generated query, DO NOT use `deleted_at IS NULL` on the requested entity. Use `WHERE <alias>.deleted_at IS NOT NULL`."
        )
    elif soft_intent == "INCLUDE_ARCHIVED":
        rules.append(
            "- ⚠️ CRITICAL INTENT OVERRIDE: The user explicitly requested AUDIT / HISTORY / ARCHIVED records. "
            "In your generated query, DO NOT use `deleted_at IS NULL`. Allow all records (both active and deleted) to be returned."
        )

    return "\n".join(rules)


OUTPUT_READABILITY_RULES = """
Core SQL Generation & Schema Mapping Protocol:
- 4-Step Schema Resolution: Before writing any SQL, strictly resolve:
  1. Business Intent: What is the user asking for? (Identify exact business entities and metrics).
  2. Table Selection: Which table physically stores that data? (e.g. product definitions in `product`, machine definitions in `machine`, batch production quantities in `production`, actual invoices in `stock` where `stock_type = 'PI'`, delivery challans in `delivery_challan`, stock adjustments in `stock_adjustment` (NOT `stock` or `product_packaging_detail`), order due dates in `sales_order`, product colors in `product_color`, bin/carton storage locations in `packagings`, measurement units in `unit`, customer orders in `sales_order`).
  3. Column Resolution: Which specific column stores the value? (e.g. stock adjustment transaction type in `stock_adjustment.transaction_type` with exact values `'StockOut'` and `'StockIn'` (NEVER `'OUT'` or `'IN'`); adjusted quantity in `SUM(stock_adjustment.qty)`; warehouse code in `warehouse.warehouse_code`, NOT in `packagings.location_code`; total carton quantity in `SUM(packagings.qty)`, NOT `COUNT(*)`; order/delivery due date in `sales_order.so_due_date`; DC numbers in `delivery_challan.dc_no`, NOT in `stock.invoice_no`; product color in `product_color.color`, NOT in table `color`; production quantity in `production.qty`, NOT in `actual_production.apq`; product names/codes in `product.product_name`, not in `batch_no` or `unit_name`; storage codes in `packagings.location_code`, not `warehouse`; customer PO in `sales_order.party_po_no`, not `purchase.ref_po_no`).
  4. Relationship & Join Graph: How should the tables be joined? (Follow verified foreign keys directly: `stock_adjustment.category_id = category.id`; `stock_adjustment.product_id = product.id`; `stock_adjustment.product_color_id = product_color.id`; `packagings.warehouse_id = warehouse.id`; `delivery_challan.sales_order_id = sales_order.id` for due dates; `delivery_challan.party_id = party.id`; `production.product_color_id = product_color.id`; `production.product_id = product.id`; `production.machine_id = machine.id`; `stock.party_id = party.id` for invoices).
- SELECT read-only queries only.
- Primary Key & Column Projection: For non-aggregate record queries, ALWAYS include the primary key column (e.g. table.id AS id) as the first selected column to serve as an anchor reference point, unless explicitly excluded by the user. Do not alias it confusingly (use alias.id AS id, not alias.id AS something_id unless requested). Follow the ID with only the specific columns or metrics asked by the user. Never bloat results with unsolicited columns (e.g. status, created_at, deleted_at). Never return raw foreign key ID columns without their human-readable name (use AS descriptive_alias).
- Dynamic Soft-Delete Filtering: For standard operational queries, always filter soft-deleted records (WHERE alias.deleted_at IS NULL). If the user query explicitly mentions 'deleted', 'removed', or 'dropped', filter WHERE alias.deleted_at IS NOT NULL; if asking for 'history', 'audit', or 'archived', omit the deleted_at filter to return all records.
- Status flags: party.status, product.status, category.status use 'Y'/'N'. Stock booked='B', dispatched='D'.
- In party table, customer/supplier name is `party.party_name` (NEVER party.name). Contact persons are `party.contact_person1`.
- In lead table, search `(lead.contact_name LIKE '%<name>%' OR lead.company_name LIKE '%<name>%')`.
- Unified Contact Search: For generic contact info without 'lead'/'customer', UNION ALL across party and lead.
- Product Color Linkage: In `production`, `actual_production`, `stock`, and `sales_order_products`, the `product_color_id` column links directly to `product_color.id` (table `product_color`, column `product_color.color`) — NOT to the `color` table! ALWAYS join `product_color pc ON prd.product_color_id = pc.id` and select `pc.color AS color`. NEVER join with the `color` table; `color` is a separate master list whose IDs do not match `product_color_id`, which causes completely wrong colors to be returned.
- Blocked Cartons: In `stock`, `party_id` is NULL. Join party through `sales_order`: `stock s JOIN sales_order so ON s.so_id = so.id JOIN party p ON so.party_id = p.id WHERE s.status = 'B'`.
- Delivery Challan & Pending Sales Orders: To find Sales Orders with pending/undelivered quantity for delivery challan creation, query:
SELECT so.sales_order_no AS sales_order_number, so.sales_order_date AS order_date, p.party_name AS customer_name, pr.product_name AS product_name, sop.qty AS ordered_quantity, COALESCE(SUM(dcp.qty), 0) AS delivered_quantity, (sop.qty - COALESCE(SUM(dcp.qty), 0)) AS pending_quantity FROM sales_order so JOIN sales_order_products sop ON so.id = sop.sales_order_id JOIN party p ON so.party_id = p.id JOIN product pr ON sop.product_id = pr.id LEFT JOIN delivery_challan dc ON so.id = dc.sales_order_id AND dc.deleted_at IS NULL LEFT JOIN delivery_challan_products dcp ON dc.id = dcp.dc_id AND dcp.product_id = sop.product_id AND dcp.deleted_at IS NULL WHERE so.deleted_at IS NULL AND sop.deleted_at IS NULL AND p.deleted_at IS NULL AND pr.deleted_at IS NULL AND p.status = 'Y' GROUP BY so.sales_order_no, so.sales_order_date, p.party_name, pr.product_name, sop.qty HAVING pending_quantity > 0 ORDER BY so.sales_order_no, pr.product_name;
- Fuzzy LIKE Filtering: Always filter descriptive text columns (categories, products, colors, names) using `LIKE '%<term>%'` rather than strict `=`. For categories with spelling variations like 'CHANGABLE PACK', match `c.category_name LIKE '%CHANG%PACK%'` (the database category is 'CHANGEABLE PACK').
- Production Quantity & Batches: In the `production` table, the primary production/batch quantity is stored in `production.qty`. When asked for the production quantity, batch quantity, or quantity produced for a batch or product, ALWAYS select `production.qty AS production_quantity` (or `SUM(prd.qty)`). NEVER select `actual_production.apq` as the default production quantity, because `actual_production.apq` is unpopulated or 0 for many batches (which causes queries to return 0), while `production.qty` contains the true quantity. Only query `actual_production.apq` if the user explicitly asks for 'actual production quantity' or 'APQ' compared to planned targets.
- Machine & Product Production: Product names/codes (e.g. 'CAP03', 'CHP06070110-INNER') are stored in `product.product_name` (NEVER in `production.batch_no` or `stock.batch_no`). To find which machine was used to create or produce a product, ALWAYS join: `production prd JOIN product p ON prd.product_id = p.id JOIN machine m ON prd.machine_id = m.id WHERE p.product_name LIKE '%<product_name>%' AND prd.deleted_at IS NULL AND m.deleted_at IS NULL`. Return `m.machine_name`.
- Warehouse & Packaging: Warehouse Identification, Carton Count vs Carton Quantity:
  (1) Warehouse Identification: Warehouse codes (e.g. 'pm2bzd19') and warehouse names/numbers (e.g. '110') are stored in the `warehouse` table: `warehouse.warehouse_code` and `warehouse.location_name`. When matching a warehouse, ALWAYS join `warehouse w ON pk.warehouse_id = w.id` and filter `(w.warehouse_code = '<code_or_name>' OR w.location_name = '<code_or_name>')`. NEVER search `packagings.location_code` for warehouse codes (`packagings.location_code` only stores internal bin/rack shelf codes).
  (2) Carton Count vs Carton Quantity: In the `packagings` table, each row represents ONE physical carton box. `COUNT(pk.id)` or `COUNT(*)` returns the number of carton boxes (e.g. 412 cartons). `pk.qty` stores the number of product units inside each carton. Therefore, when asked for 'carton quantity', 'total carton quantity', 'quantity in cartons', or 'stock quantity in warehouse', ALWAYS use `SUM(pk.qty) AS total_carton_quantity` (e.g. 152,354 units), NEVER `COUNT(*)`! You may return both: `COUNT(pk.id) AS total_cartons, SUM(pk.qty) AS total_carton_quantity`.
  (3) Product Storage Locations: To find where a product is stored or what warehouse/bin it is in, join `packagings pk JOIN warehouse w ON pk.warehouse_id = w.id JOIN product p ON pk.product_id = p.id WHERE p.product_name LIKE '%<term>%'`. Return `p.product_name`, `w.warehouse_code`, `w.location_name AS warehouse_location`, `pk.location_code AS bin_location`, `pk.carton_no`, `pk.qty AS carton_qty`.
- Stock Adjustments (StockOut vs StockIn): All inventory stock adjustments (stock-in additions, stock-out write-offs, physical count adjustments) are stored in the dedicated `stock_adjustment` table:
  (1) Table Selection: ALWAYS use `stock_adjustment` when asked about stock adjustments, stock-out, stock-in, or adjusted quantity. NEVER use `stock` (which is for purchase inward and sales dispatches) and NEVER use `product_packaging_detail` (which is a packaging BOM master table).
  (2) Transaction Type Enum: `stock_adjustment.transaction_type` has ONLY TWO exact enum values: `'StockOut'` (stock reduction / outward adjustment) and `'StockIn'` (stock addition / inward adjustment). NEVER use `'OUT'`, `'IN'`, `'Stock-Out'`, `'STOCK_OUT'`, or lowercase strings. For stock-out queries, filter `sa.transaction_type = 'StockOut'`. For stock-in queries, filter `sa.transaction_type = 'StockIn'`.
  (3) Columns & Direct Foreign Keys:
      - Adjustment Date: `sa.stock_adjustment_date` (e.g. `WHERE sa.stock_adjustment_date = '2025-06-03'`).
      - Adjusted Quantity: `sa.qty` (or `SUM(sa.qty) AS total_adjusted_quantity`).
      - Category Link: `JOIN category c ON sa.category_id = c.id WHERE c.category_name LIKE '%<name>%'`.
      - Product Link: `JOIN product p ON sa.product_id = p.id WHERE p.product_name LIKE '%<name>%'`.
      - Color Link: `JOIN product_color pc ON sa.product_color_id = pc.id`.
  (4) Canonical Query Templates:
      - Adjustment Count by Date: `SELECT COUNT(*) AS stock_out_adjustment_count FROM stock_adjustment sa WHERE sa.deleted_at IS NULL AND sa.stock_adjustment_date = '<date>' AND sa.transaction_type = 'StockOut';`
      - Category Stock-out Quantity: `SELECT c.category_name, SUM(sa.qty) AS total_stock_out_quantity FROM stock_adjustment sa JOIN category c ON sa.category_id = c.id WHERE sa.deleted_at IS NULL AND c.deleted_at IS NULL AND c.category_name LIKE '%<cat>%' AND sa.transaction_type = 'StockOut' AND sa.stock_adjustment_date = '<date>' GROUP BY c.category_name;`
      - Product Adjusted Quantity: `SELECT p.product_name, sa.transaction_type, SUM(sa.qty) AS total_qty_adjusted FROM stock_adjustment sa JOIN product p ON sa.product_id = p.id WHERE sa.deleted_at IS NULL AND p.deleted_at IS NULL AND p.product_name LIKE '%<product>%' GROUP BY p.product_name, sa.transaction_type;`
      - Product Total Adjusted Quantity (All-Time): `SELECT SUM(sa.qty) AS total_qty_adjusted FROM stock_adjustment sa JOIN product p ON sa.product_id = p.id WHERE sa.deleted_at IS NULL AND p.deleted_at IS NULL AND p.product_name LIKE '%<product>%';`
- Product Units of Measure: The unit table contains unit definitions ('Pcs', 'Kg', 'Nos', etc.) and NEVER contains product names. To find the unit for a product (e.g. 'CAP03'), ALWAYS query: `product p JOIN unit u ON p.unit_id = u.id WHERE p.product_name LIKE '%<product>%' AND p.deleted_at IS NULL AND u.deleted_at IS NULL`. Return `p.product_name` and `u.unit_name AS unit_of_measure`. Never search `unit.unit_name` for product names.
- Product Type vs Category: There are two places with product type: (1) `category.product_type` stores enum `'RM'` (Raw Material). (2) `product_type.product_type` stores text `'Raw Material'` (id=1) and `'Finished Goods'` (id=2). When querying products by category (e.g. 'Carton') and product type ('Raw Material'), ALWAYS include BOTH filters: `product p JOIN category c ON p.category_id = c.id WHERE c.category_name LIKE '%Carton%' AND (c.product_type = 'RM' OR p.product_type_id = 1)`. Never omit the category filter, and never compare `category.product_type = 'Raw Material'` directly (use `'RM'`).
- Customer PO vs Supplier PO vs Proforma PO: PO numbers exist in 3 distinct places: (1) Customer/Party PO: `sales_order.party_po_no` (and `sales_order.party_po_date`). For questions asking for "party's PO number", "customer PO", or "PO number for sales order/party", ALWAYS query `sales_order so JOIN party p ON so.party_id = p.id`. (2) Proforma PO: `proforma.po_no` (only for proforma invoice questions). (3) Supplier/Vendor PO: `purchase.ref_po_no` (only for supplier inward purchase orders). NEVER use `purchase.ref_po_no` for customer/party PO requests.
- Invoices vs Proforma: Actual invoice details (numbers, dates, parties) are stored in the `stock` table where `stock.stock_type = 'PI'`, NOT in the `proforma` table! For questions asking about invoices, invoice lists, or invoice counts: (1) Query `stock s JOIN party p ON s.party_id = p.id WHERE s.stock_type = 'PI' AND s.deleted_at IS NULL AND p.deleted_at IS NULL`. (2) When `stock_type = 'PI'`, `s.party_id` connects DIRECTLY to `party.id` (do NOT route through sales_order). (3) Always filter `s.stock_type = 'PI'`. (4) Calculate invoice count as `COUNT(DISTINCT s.invoice_no)`. Only query `proforma` table if user explicitly specifies "proforma".
- Delivery Challan (DC) vs Invoice & Due Date: A Delivery Challan (DC) and an Invoice are completely separate documents! Actual DC numbers and dates are stored in the `delivery_challan` table: `dc.dc_no` (DC number) and `dc.dc_date` (DC date). Logistics columns: `dc.transport_name` (carrier name) and `dc.lr_number` (Lorry Receipt / LR number — NOT `lr_no`). The customer/party is linked directly via `delivery_challan.party_id = party.id`. NEVER search for DC numbers in `stock.invoice_no` or `stock`! IMPORTANT: `delivery_challan` has NO due date column; the order due date is stored in `sales_order.so_due_date`. When a query asks for the due date of a DC, you MUST join `sales_order`: `LEFT JOIN sales_order so ON dc.sales_order_id = so.id` and select `so.so_due_date AS due_date`.
- Document Number Uniqueness Across Financial Years (DC, Sales Order, PO, etc.): Document numbers (`dc_no`, `sales_order_no`, `purchase_no`, `proforma_no`, `production_no`) are NOT globally unique; they repeat across different financial years! For example, `dc_no = 527` and `sales_order_no = 405` exist in multiple financial years for completely different parties. (1) If a financial year is specified (e.g. 'in 2024-2025' or 'this year'), join `financial_year fy ON t.financial_id = fy.id` and filter `fy.fyear = '...'` or `fy.current_year = 'Y'`. (2) If NO financial year is specified for a SINGLE DOCUMENT LOOKUP: the user intends the LATEST / CURRENT record! ALWAYS sort by date DESC with `LIMIT 1` (e.g. `ORDER BY dc.dc_date DESC LIMIT 1` or `ORDER BY so.sales_order_date DESC LIMIT 1`), and include `fy.fyear AS financial_year` in the SELECT clause so the user knows which financial year the document belongs to. (3) For AGGREGATE or cumulative metric queries (e.g. sums, counts, totals), do NOT apply LIMIT 1 and do NOT filter by current_year unless explicitly asked.
- Temporal Scope & Financial Year Filtering: ONLY add `financial_year.current_year = 'Y'` if the user query contains explicit temporal markers such as 'current', 'this year', 'latest', 'active', or 'ongoing'. If the query asks for 'total', 'all', 'history', or implies a cumulative sum without a time qualifier (e.g. 'quantity adjusted', 'total sales', 'overall quantity'), DO NOT filter by current_year. Sum across all available years. When the user DOES explicitly request the current financial year, NEVER filter using `YEAR(date) = YEAR(CURDATE())`; ALWAYS join `financial_year fy ON t.financial_id = fy.id` (or `WHERE t.financial_id = (SELECT id FROM financial_year WHERE current_year = 'Y')`) with `fy.current_year = 'Y'`.
- Combined Production, Stock & Sales Order Report: When queried for a multi-domain report (PPQ, APQ, Stock, Pending SOs) grouped by Category, Product, Color, use CTE subqueries (WITH prod_m AS (...), stock_m AS (...), so_m AS (...)) aggregated per `(product_id, product_color_id)` before joining to `product p` to prevent Cartesian join multiplication.
"""


def build_sql_prompt(
    query: str,
    schema: str,
    dialect: Any,
    db_id: str = DEFAULT_DB_ID,
    last_error: str | None = None,
    relationships: str = "",
    pattern_learner: Any | None = None,
) -> str:
    current_date_str = datetime.date.today().isoformat()  # noqa: DTZ011
    intent = extract_analytical_intent(query)
    intent_summary_lines = []
    if intent.get("metrics"):
        intent_summary_lines.append(f"- Metrics: {', '.join(intent['metrics'])}")
    if intent.get("dimensions"):
        intent_summary_lines.append(f"- Dimensions: {', '.join(intent['dimensions'])}")
    if intent.get("filters"):
        intent_summary_lines.append(f"- Filters: {', '.join(intent['filters'])}")
    if intent.get("time_period"):
        intent_summary_lines.append(f"- Time Period: {intent['time_period']}")
    if intent.get("aggregation"):
        intent_summary_lines.append(f"- Aggregation: {intent['aggregation']}")
    if intent.get("limit"):
        intent_summary_lines.append(f"- Limit: {intent['limit']} (Sorting: {intent['sorting'] or 'DESC'})")
    if intent.get("temporal_scope") == "CURRENT_YEAR":
        intent_summary_lines.append(
            "- Temporal Scope: CURRENT FINANCIAL YEAR (Join financial_year and filter `financial_year.current_year = 'Y'`)"
        )
    elif intent.get("temporal_scope") == "ALL_TIME":
        intent_summary_lines.append(
            "- Temporal Scope: ALL-TIME / CUMULATIVE (DO NOT filter by `financial_year.current_year = 'Y'`. Sum/aggregate across ALL available years!)"
        )

    soft_intent = intent.get("soft_delete_intent", "ACTIVE_ONLY")
    if soft_intent == "DELETED_ONLY":
        intent_summary_lines.append(
            "- Record Status / Soft-Delete Scope: EXPLICIT DELETED RECORDS (User explicitly requested deleted/removed records. Filter specifically for deleted records using `WHERE alias.deleted_at IS NOT NULL` or omit `deleted_at IS NULL`. DO NOT exclude deleted records!)"
        )
    elif soft_intent == "INCLUDE_ARCHIVED":
        intent_summary_lines.append(
            "- Record Status / Soft-Delete Scope: AUDIT / HISTORY / ARCHIVED (User explicitly requested history, audit trail, or archived records. DO NOT add `deleted_at IS NULL` filter! Allow all records—both active and deleted—to be returned.)"
        )
    else:
        intent_summary_lines.append(
            "- Record Status / Soft-Delete Scope: ACTIVE OPERATIONAL (Default rule. Filter out soft-deleted records using `WHERE alias.deleted_at IS NULL` on all tables with a deleted_at column.)"
        )

    intent_section = (
        "\nExtracted Business Intent:\n" + "\n".join(intent_summary_lines) + "\n"
        if intent_summary_lines
        else ""
    )

    schema_tables = extract_schema_table_names(schema)
    behavioral_atlas_text = build_behavioral_atlas_for_query(schema_tables, query, db_id=db_id)
    column_glossary = build_column_glossary_for_query(query, db_id=db_id)

    dialect_name = getattr(dialect, "name", "SQL")

    system_prompt = f"""You are Antarkosh, an expert Enterprise Business Intelligence Agent for {dialect_name}.
Your goal is to translate the business question into a valid, executable, read-only {dialect_name} SELECT query.

IMPORTANT: Output ONLY the final SQL query in a ```sql ... ``` code block. Strictly NO introductory explanations, NO conversational prose, NO step-by-step bullet points.

Rules:
- Read-Only: SELECT statements only.
- Mixed / Multi-part queries: If the user question contains both document/system questions (e.g. OCR, RAG architecture, policies, tax rates, general docs) and database questions (e.g. products, machines, orders, stock, production), IGNORE the document/system questions and generate SQL ONLY for the database portion! Only respond with NO_SQL if NO part of the question relates to the database schema.
- Dynamic Soft-Delete & Record Status:
  * Default Rule (Active Operational Data): For standard operational queries, or when asking for 'active', 'current', 'present', 'live', or 'existing' data, ALWAYS filter out soft-deleted records by adding `alias.deleted_at IS NULL` on all tables with a `deleted_at` column.
  * Exception Rule (Explicit Deleted Records): If the user query explicitly mentions 'deleted', 'removed', 'dropped', or 'gone', DO NOT add the `deleted_at IS NULL` filter. Instead, filter specifically for deleted records using `WHERE alias.deleted_at IS NOT NULL` (or omit `deleted_at IS NULL` for joined lookup entities).
  * Exception Rule (Audit / History / Archived Data): If the user query explicitly mentions 'archived', 'history', 'audit', or 'past records', DO NOT add the `deleted_at IS NULL` filter. Allow all records (both active and deleted) to be returned.
- Casting: Use CAST(col AS DECIMAL(10,2)) for numeric operations on VARCHAR columns (e.g. stock.qty).
- Aliases: Use descriptive aliases (e.g. AS customer_name, AS total_revenue). Never return raw IDs without names.
- Status Flags: Active='Y', Inactive='N'. Stock booked='B', dispatched='D'.
- Customer vs Supplier: In party table, join to sales_order for Customers, or purchase for Suppliers.
- Current Date: {current_date_str} (Use for relative date calculations like 'this year', 'last month').
- Temporal Scope & Financial Year Filtering:
  * ONLY add `financial_year.current_year = 'Y'` if the user query contains explicit temporal markers such as 'current', 'this year', 'latest', 'active', or 'ongoing'.
  * Negative Constraint: If the query asks for 'total', 'all', 'history', or implies a cumulative sum without a time qualifier (e.g. 'quantity adjusted', 'total sales', 'overall quantity'), DO NOT filter by current_year. Sum across all available years.
  * Ambiguity Handling: If the temporal intent is ambiguous between current vs all-time, PREFER the broader scope (all-time). NEVER silently narrow cumulative or aggregate queries to the current financial year.

{intent_section}
Schema:
{schema}
"""
    system_prompt += "\n\n" + get_scoped_readability_rules(query, sorted(schema_tables))

    if behavioral_atlas_text:
        system_prompt += (
            "\n\n=== BEHAVIORAL SCHEMA ATLAS & COGNITIVE ONTOLOGY ===\n"
            f"{behavioral_atlas_text}"
        )

    scoped_rels = format_scoped_relationships(schema_tables, query, db_id=db_id) if schema_tables else ""
    rels_to_inject = scoped_rels or relationships
    if rels_to_inject:
        system_prompt += (
            f"\n\nTable relationships:\n{rels_to_inject}"
        )

    if column_glossary:
        system_prompt += (
            "\n\nColumn mapping (use these exact paths — do NOT invent columns):\n"
            f"{column_glossary}"
        )
    if getattr(dialect, "date_functions", None):
        system_prompt += (
            f"\n\nDate/time syntax for {dialect_name}:\n"
            f"{dialect.date_functions}"
        )
    if pattern_learner:
        learned_patterns = pattern_learner.get_patterns_for_query(query)
        if learned_patterns:
            pattern = learned_patterns[0]
            pattern_text = f"- Scenario: {pattern.business_scenario}\n  Reasoning: {pattern.cot_reasoning_snippet}\n  Template: `{pattern.sql_structure_template}`"
            system_prompt += f"\n\n=== RELEVANT LEARNED SQL PATTERN ===\n{pattern_text}"

    if last_error:
        system_prompt += f"\n\nWARNING: Your previous attempt failed with this error: {last_error}\nPlease fix the SQL query and try again."

    return system_prompt


# ---------------------------------------------------------------------------
# Re-export aliases for compatibility
# ---------------------------------------------------------------------------
_get_raw_relationships = get_raw_relationships
_clear_raw_relationships_cache = clear_raw_relationships_cache
_load_relationships = load_relationships
_clear_relationships_cache = clear_relationships_cache
_load_glossary = load_glossary
_clear_glossary_cache = clear_glossary_cache
_get_raw_column_glossary = get_raw_column_glossary
_clear_raw_column_glossary_cache = clear_raw_column_glossary_cache
_GLOSSARY_STOP_WORDS = GLOSSARY_STOP_WORDS
_stem_word = stem_word
_matches_glossary_candidate = matches_glossary_candidate
_build_column_glossary_for_query = build_column_glossary_for_query
_get_raw_behavioral_atlas = get_raw_behavioral_atlas
_clear_behavioral_atlas_cache = clear_behavioral_atlas_cache
_build_behavioral_atlas_for_query = build_behavioral_atlas_for_query
_get_scoped_readability_rules = get_scoped_readability_rules
_OUTPUT_READABILITY_RULES = OUTPUT_READABILITY_RULES
_build_sql_prompt = build_sql_prompt
_clear_prompt_caches = clear_prompt_caches
