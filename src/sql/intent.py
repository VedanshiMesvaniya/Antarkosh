"""Business analytical intent detection for text-to-SQL generation."""

from __future__ import annotations

import re
from typing import Any

from src.sql.soft_delete import detect_soft_delete_intent


def extract_analytical_intent(query: str) -> dict[str, Any]:
    """Extract business analytical intent (metrics, dimensions, filters, time_period, aggregation, limit, sorting)
    from a user's question without writing SQL, serving as a semantic-layer preprocessor.
    """
    q = query.lower()
    intent: dict[str, Any] = {
        "metrics": [],
        "dimensions": [],
        "filters": [],
        "time_period": None,
        "entities": [],
        "aggregation": None,
        "limit": None,
        "sorting": None,
        "ambiguous_terms": [],
    }

    # Relative Time Periods
    if "last month" in q or "previous month" in q:
        intent["time_period"] = "last month"
    elif "this year" in q or "current year" in q or "this financial year" in q or "this fiscal year" in q:
        intent["time_period"] = "this financial year"
    elif "last year" in q or "previous year" in q:
        intent["time_period"] = "last financial year"
    elif "last quarter" in q:
        intent["time_period"] = "last quarter"
    elif "last 6 months" in q or "past 6 months" in q:
        intent["time_period"] = "last 6 months"
    elif "last 30 days" in q or "past 30 days" in q:
        intent["time_period"] = "last 30 days"

    # Aggregations
    if re.search(r"\b(how many|count|number of)\b", q):
        intent["aggregation"] = "COUNT"
    elif re.search(r"\b(how much|total|sum|overall)\b", q):
        intent["aggregation"] = "SUM"
    elif re.search(r"\b(average|mean|avg)\b", q):
        intent["aggregation"] = "AVG"

    # Limits & Rankings
    top_match = re.search(r"\btop\s+(\d+)\b", q)
    if top_match:
        intent["limit"] = int(top_match.group(1))
        intent["sorting"] = "DESC"
    elif re.search(r"\b(best|highest|top|most|maximum|greatest)\b", q):
        intent["limit"] = 1
        intent["sorting"] = "DESC"
    elif re.search(r"\b(lowest|worst|least|minimum|cheapest)\b", q):
        intent["limit"] = 1
        intent["sorting"] = "ASC"

    # Metrics
    if any(k in q for k in ["bought from us", "sales", "revenue", "turnover", "sold", "spent with us", "order value", "orders"]):
        intent["metrics"].append("sales value / revenue")
        intent["dimensions"].append("customer / party")
    elif any(k in q for k in ["we bought", "we spend", "we spent", "purchase", "bought from vendor", "bought from supplier", "procurement", "inward", "suppliers paid"]):
        intent["metrics"].append("purchase expenditure")
        intent["dimensions"].append("supplier")
    elif any(k in q for k in ["bought", "buying", "spent", "spending"]):
        # Default "who bought the most / who spent the most" -> customer sales
        intent["metrics"].append("sales value / revenue")
        intent["dimensions"].append("customer / party")

    if any(k in q for k in ["qty", "quantity", "volume", "units"]):
        intent["metrics"].append("quantity / units")
    if any(k in q for k in ["stock", "inventory", "on hand"]):
        intent["metrics"].append("stock on hand")
    if any(k in q for k in ["production", "produced", "manufactured", "output"]):
        intent["metrics"].append("production quantity")

    # Dimensions & Entities
    if any(k in q for k in ["customer", "client", "buyer", "party", "parties"]) and "customer / party" not in intent["dimensions"]:
        intent["dimensions"].append("customer / party")
    if any(k in q for k in ["supplier", "vendor"]) and "supplier" not in intent["dimensions"]:
        intent["dimensions"].append("supplier")
    if any(k in q for k in ["product", "item", "goods", "sku"]):
        intent["dimensions"].append("product")
    if any(k in q for k in ["category"]):
        intent["dimensions"].append("category")
    if any(k in q for k in ["lead", "leads", "inquiry", "inquiries", "prospect"]):
        intent["dimensions"].append("sales lead")
    if any(k in q for k in ["challan", "dispatch", "delivery", "shipping"]):
        intent["dimensions"].append("delivery challan")
    if any(k in q for k in ["month", "monthly"]):
        intent["dimensions"].append("month")

    # Filters & Statuses
    if any(k in q for k in ["open", "pending", "active", "in progress", "in-progress"]):
        intent["filters"].append("open / pending / in-progress")
    if any(k in q for k in ["verified", "unverified", "pending verification"]):
        intent["filters"].append("carton verification status")
    if any(k in q for k in ["shortfall", "fell short", "short"]):
        intent["filters"].append("actual output < planned target")
    if any(k in q for k in ["inactive", "haven't ordered", "no orders"]):
        intent["filters"].append("inactive (no recent orders)")

    # Temporal Scope & Intent Detection (Distinguish Current vs All-Time / Cumulative)
    has_current_marker = bool(re.search(
        r"\b(current|this year|this financial year|this fiscal year|active year|current financial|current fiscal|ongoing|present year)\b",
        q,
    ))
    has_all_time_marker = bool(re.search(
        r"\b(total|all|all-time|all time|history|historical|overall|cumulative|ever|across all years|lifetime|entire)\b",
        q,
    ))
    has_cumulative_metric = (
        intent.get("aggregation") in ("SUM", "COUNT", "AVG")
        or bool(re.search(r"\b(quantity adjusted|adjusted quantity|adjusted qty|total quantity|total qty|how many|how much|sum of)\b", q))
    )

    if has_current_marker:
        intent["temporal_scope"] = "CURRENT_YEAR"
    elif has_all_time_marker or has_cumulative_metric or intent["time_period"] is None:
        intent["temporal_scope"] = "ALL_TIME"
    else:
        intent["temporal_scope"] = "SPECIFIC_PERIOD"

    # Record Status & Dynamic Soft-Delete Intent
    intent["soft_delete_intent"] = detect_soft_delete_intent(query)

    return intent


__all__ = ["extract_analytical_intent"]
