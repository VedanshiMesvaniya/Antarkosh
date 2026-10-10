"""Local Telemetry Dashboard API endpoints — overview, failure distribution, guard telemetry, trace logs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()


@router.get("/ui/telemetry/overview")
@router.get("/telemetry/overview", include_in_schema=False)
async def get_telemetry_overview() -> dict[str, Any]:
    """Return high-level summary telemetry (p50/p95 latency, success rate, fallback rate)."""
    from src.utils.trace_writer import get_telemetry_aggregator

    return get_telemetry_aggregator().get_overview()


@router.get("/ui/telemetry/failures")
@router.get("/telemetry/failures", include_in_schema=False)
async def get_telemetry_failures() -> dict[str, Any]:
    """Return failure distribution by semantic category and stage."""
    from src.utils.trace_writer import get_telemetry_aggregator

    return get_telemetry_aggregator().get_failures()


@router.get("/ui/telemetry/guards")
@router.get("/telemetry/guards", include_in_schema=False)
async def get_telemetry_guards() -> dict[str, Any]:
    """Return guard evaluation metrics (checks, shadow blocks, enforced blocks, block rates)."""
    from src.utils.trace_writer import get_telemetry_aggregator

    return get_telemetry_aggregator().get_guards()


@router.get("/ui/telemetry/traces")
@router.get("/telemetry/traces", include_in_schema=False)
async def get_telemetry_traces(
    limit: int = 50,
    status: str | None = None,
) -> dict[str, Any]:
    """Return recent trace snapshots filtered by status."""
    from src.utils.trace_writer import get_telemetry_aggregator

    traces = get_telemetry_aggregator().get_traces(limit=limit, status=status)
    return {
        "traces": traces,
        "count": len(traces),
        "limit": limit,
        "status_filter": status,
    }
