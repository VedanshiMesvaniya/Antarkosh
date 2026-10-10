"""System settings and provider management endpoints for UI API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import require_admin
from src.api.ui.databases import DBConnectionPayload
from src.core import db_settings
from src.core.config import settings
from src.core.provider_client import ProviderRouter
from src.core.state import state_manager
from src.sql.learning.pipeline_metrics import get_score_summary

logger = logging.getLogger(__name__)
router = APIRouter()

# Human-readable labels + display order for the provider picker. OpenRouter
# leads because it's the default soft pin.
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "gemini": "Gemini",
    "groq": "Groq",
    "nvidia_nim": "NVIDIA NIM",
}
_PROVIDER_ORDER = ["openrouter", "gemini", "groq", "nvidia_nim"]

_PROVIDER_METADATA = {
    "gemini": {
        "model": "gemini-3.5-flash",
        "role": "General RAG & Vision",
    },
    "groq": {
        "model": "qwen3.8-27b",
        "role": "Fast Reasoning & Repair",
    },
    "nvidia_nim": {
        "model": "nemotron-3.5",
        "role": "Heavy Reasoning",
    },
    "openrouter": {
        "model": "multi-model",
        "role": "Fallback Chain",
    },
}


@router.get("/overview")
async def get_overview() -> dict[str, Any]:
    """Overview stats for the UI dashboard."""
    provider_router = ProviderRouter()
    has_llm = any(p.is_available for p in provider_router._providers.values())
    
    return {
        "backendStatus": "online",
        "ollamaStatus": "inactive",
        "vectorStatus": "ready",
        "modelLabel": "Auto-routed via ProviderRouter" if has_llm else "No Providers Configured",
        "contextTokens": 8192,
        "privacyLabel": "Zero-Cost Free Tier API",
    }


@router.get("/providers")
async def get_providers() -> dict[str, Any]:
    """List selectable model providers for the settings picker."""
    provider_router = ProviderRouter()
    available = {
        name for name, provider in provider_router._providers.items() if provider.is_available
    }
    ordered = [n for n in _PROVIDER_ORDER if n in available] + [
        n for n in sorted(available) if n not in _PROVIDER_ORDER
    ]

    options = [{"id": "auto", "label": "Auto (recommended)"}]
    options += [{"id": n, "label": _PROVIDER_LABELS.get(n, n)} for n in ordered]

    default = (settings.default_provider or "auto").strip().lower()
    if default != "auto" and default not in available:
        default = "auto"

    return {"providers": options, "default": default}


@router.get("/providers/usage")
async def get_provider_usage() -> dict[str, Any]:
    """Live per-provider quota usage for the settings usage meter."""
    from src.core.rate_limiter import get_shared_rate_limiter

    provider_router = ProviderRouter()
    available = [
        name for name, provider in provider_router._providers.items() if provider.is_available
    ]
    snapshot = get_shared_rate_limiter().usage_snapshot(available)

    ordered = [n for n in _PROVIDER_ORDER if n in available] + [
        n for n in sorted(available) if n not in _PROVIDER_ORDER
    ]
    providers = []
    for name in ordered:
        s = snapshot.get(name, {})
        meta = _PROVIDER_METADATA.get(name, {})
        providers.append(
            {
                "id": name,
                "label": _PROVIDER_LABELS.get(name, name),
                "model": meta.get("model", "auto"),
                "role": meta.get("role", "LLM Worker"),
                "rpmUsed": s.get("rpm_used", 0),
                "rpmLimit": s.get("rpm_limit", 0),
                "rpdUsed": s.get("rpd_used", 0),
                "rpdLimit": s.get("rpd_limit", 0),
                "tpmUsed": s.get("tpm_used", 0),
                "tpmLimit": s.get("tpm_limit", 0),
                "tpdUsed": s.get("tpd_used", 0),
                "tpdLimit": s.get("tpd_limit", 0),
                "backoffSeconds": s.get("backoff_seconds", 0),
            }
        )
    return {"providers": providers}


@router.get("/pipeline/metrics")
async def get_pipeline_metrics() -> dict[str, Any]:
    """Aggregated pipeline performance: total score, catches, blunders, breakdown."""
    return get_score_summary()


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """Get UI settings."""
    return state_manager.get_settings()


@router.post("/settings")
async def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Save UI settings."""
    return state_manager.save_settings(settings)


@router.post("/settings/sync-schema")
async def sync_schema(
    db_id: str = "erp_main",
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Sync the live database schema into the vector store for Schema RAG."""
    from src.sql.knowledge.loaders import validate_db_id
    from src.sql.schema_retrieval import sync_live_schema

    try:
        validate_db_id(db_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        result = await sync_live_schema(db_id=db_id)
        return result
    except Exception as e:  # noqa: BLE001
        logger.error("Schema sync failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Schema sync failed: {e}")


@router.get("/settings/database")
async def get_db_connection(_admin: str = Depends(require_admin)) -> dict[str, Any]:
    """Current live-DB connection (password never returned) + the engine/field catalogue."""
    return db_settings.current_config()


@router.post("/settings/database/test")
async def test_db_connection(
    body: DBConnectionPayload, _admin: str = Depends(require_admin)
) -> dict[str, Any]:
    """Try the credentials without saving anything."""
    try:
        await db_settings.test_only(body.model_dump())
    except db_settings.DBSettingsError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}


@router.post("/settings/database")
async def save_db_connection(
    body: DBConnectionPayload, _admin: str = Depends(require_admin)
) -> dict[str, Any]:
    """Test, then persist to .env and apply live. Nothing is written if the test fails."""
    try:
        return await db_settings.save(body.model_dump())
    except db_settings.DBSettingsError as e:
        raise HTTPException(status_code=422, detail=str(e))
