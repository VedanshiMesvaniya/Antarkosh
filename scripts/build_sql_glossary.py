#!/usr/bin/env python3
"""Build a schema-aware column glossary for Text-to-SQL.

Reads the existing glossary.json (business terms) and
schema.json (schema), and produces a
column_glossary.json mapping business terms to exact table.column paths.
"""

import json
from pathlib import Path

try:
    from scripts._paths import REPO_ROOT
except ModuleNotFoundError:
    from _paths import REPO_ROOT

from src.sql.knowledge.loaders import get_knowledge_path

HERE = Path(__file__).parent.resolve()
REPO = REPO_ROOT
SCHEMA_FILE = get_knowledge_path("schema")
OLD_GLOSSARY_FILE = get_knowledge_path("glossary")
OUT_FILE = get_knowledge_path("column_glossary")

# Manual overrides for terms that need complex logic, CASTs, or business definitions.
OVERRIDES = {
    "revenue": {
        "maps_to": "SUM(product.rate * sales_order_products.qty)",
        "note": "Booked base sales value (excluding tax) from sales orders. If question specifically asks for invoiced/tax-inclusive revenue or billing, use proforma.grand_total.",
        "synonyms": ["total sales", "sales value", "turnover", "sales amount", "base sales", "money made"],
    },
    "invoiced_revenue": {
        "maps_to": "SUM(proforma.grand_total)",
        "note": "Invoiced total amount including GST and discounts from proforma invoices.",
        "synonyms": ["invoiced total", "billed amount", "invoice revenue", "grand total", "net billing", "billed sales"],
    },
    "tax_amount": {
        "maps_to": "SUM(proforma.gst_amount)",
        "note": "Total GST tax amount from proforma invoices.",
        "synonyms": ["gst amount", "tax collected", "total gst", "tax value", "gst total"],
    },
    "stock_quantity": {
        "maps_to": "CAST(stock.qty AS UNSIGNED)",
        "note": "stock.qty is VARCHAR — CAST before aggregating.",
        "synonyms": ["inventory", "on-hand", "stock level", "available quantity", "stock on hand"],
    },
    "customer": {
        "maps_to": "party.party_name",
        "note": "party table holds both customers (profile_type='Party') and suppliers (profile_type='Company').",
        "synonyms": ["client", "buyer", "account", "customer name", "purchaser"],
    },
    "dispatched_quantity": {
        "maps_to": "delivery_challan_products.qty",
        "note": "Quantity dispatched via delivery challans.",
        "synonyms": ["dispatched", "shipped", "sent out", "delivery quantity", "dispatch count"],
    },
    "packed_cartons": {
        "maps_to": "packagings.packing_qty_count",
        "note": "Verified packed carton count (carton_verify_status='V').",
        "synonyms": ["cartons", "packed boxes", "ready cartons", "boxed stock", "warehouse boxes"],
    },
    "quotation": {
        "maps_to": "SUM(quotation_products.final_amount)",
        "note": "Price estimates, proposals, and quotations sent to customers.",
        "synonyms": ["quote", "estimate", "proposal", "price estimate", "quotations", "estimates", "proposals", "price estimates"],
    },
    "vendor": {
        "maps_to": "party.party_name",
        "note": "Suppliers and raw material vendors (party.profile_type='Company').",
        "synonyms": ["supplier", "seller", "raw material vendor", "suppliers", "vendors", "sellers"],
    },
    "purchase_quantity": {
        "maps_to": "purchase_products.qty",
        "note": "Quantity of raw materials or stock purchased from suppliers.",
        "synonyms": ["supplies bought", "materials purchased", "procured items", "purchase qty", "bought quantity"],
    },
    "transporter": {
        "maps_to": "delivery_challan.transport_name",
        "note": "Logistics carrier or transport company used for dispatch.",
        "synonyms": ["logistics company", "carrier", "shipping company", "transport agency", "logistics partner"],
    },
    "lead_inquiry": {
        "maps_to": "lead.company_name",
        "note": "Sales inquiries and potential client leads.",
        "synonyms": ["sales lead", "inquiry", "prospect", "new inquiry", "business lead", "leads", "inquiries", "prospects"],
    },
    "opening_balance": {
        "maps_to": "party_opening_balance.opening_balance",
        "note": "Initial starting balance for customers or suppliers.",
        "synonyms": ["initial balance", "starting balance", "opening dues", "prior balance", "carried forward balance"],
    },
    "stock_adjustment": {
        "maps_to": "stock_adjustment.qty",
        "note": "Manual inventory corrections and adjustments.",
        "synonyms": ["inventory correction", "stock write-off", "manual adjustment", "stock correction", "reconciled quantity"],
    },
}


import argparse

def build_glossary(schema_path: Path | None = None, base_glossary_path: Path | None = None, out_path: Path | None = None) -> None:
    schema_file = schema_path or SCHEMA_FILE
    base_file = base_glossary_path or OLD_GLOSSARY_FILE
    dest_file = out_path or OUT_FILE

    print(f"Reading schema from {schema_file}")
    schema_data = json.loads(schema_file.read_text(encoding="utf-8"))

    print(f"Reading base glossary from {base_file}")
    base_glossary = json.loads(base_file.read_text(encoding="utf-8"))

    glossary = {}

    # 1. Apply overrides
    for term, data in OVERRIDES.items():
        glossary[term] = data

    # 2. Add exact matches from schema
    for table in schema_data["tables"]:
        table_name = table["name"]
        for col in table["columns"]:
            col_name = col["name"]
            if col_name in ["id", "created_at", "updated_at", "deleted_at"]:
                continue

            # Look for synonyms in base glossary
            syns = base_glossary.get(col_name, [])

            # Simple heuristic names
            term_name = f"{table_name}_{col_name}"
            if term_name not in glossary and col_name not in OVERRIDES:
                glossary[term_name] = {
                    "maps_to": f"{table_name}.{col_name}",
                    "note": "",
                    "synonyms": syns,
                }

    dest_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"Writing column glossary with {len(glossary)} terms to {dest_file}")
    dest_file.write_text(json.dumps(glossary, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build column glossary from schema and base glossary")
    ap.add_argument("--db", type=str, default="erp_main", help="Target database id (default: erp_main)")
    ap.add_argument("--schema", type=Path, default=None, help="Path to schema.json")
    ap.add_argument("--base-glossary", type=Path, default=None, help="Path to base glossary.json")
    ap.add_argument("--out", type=Path, default=None, help="Output path for column_glossary.json")
    args = ap.parse_args()

    schema = args.schema
    if schema is None:
        schema = get_knowledge_path("schema", db_id=args.db)

    base = args.base_glossary
    if base is None:
        base = get_knowledge_path("glossary", db_id=args.db)

    out = args.out
    if out is None:
        out = get_knowledge_path("column_glossary", db_id=args.db)

    build_glossary(schema_path=schema, base_glossary_path=base, out_path=out)


if __name__ == "__main__":
    main()
