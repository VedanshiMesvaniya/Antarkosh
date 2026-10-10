"""Document management endpoints for UI API — listing, version history, deletion, user access."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)
router = APIRouter()


def _doc_view(entry: dict[str, Any], version_count: int = 1) -> dict[str, Any]:
    """Shape a registry entry into the document view the frontend expects."""
    return {
        "id": entry.get("document_id", ""),
        "name": entry.get("filename", "Unknown"),
        "sizeBytes": entry.get("file_size_bytes", 0),
        "chunks": entry.get("total_chunks", 0),
        "ingestedAt": entry.get("created_at", ""),
        "lineageRoot": entry.get("lineage_root", entry.get("document_id", "")),
        "supersedes": entry.get("supersedes"),
        "versionCount": version_count,
        "allowedUsers": entry.get("allowed_users", []),
    }


class DocumentAccessPayload(BaseModel):
    allowed_users: list[str]


@router.get("/documents")
async def get_documents(current_user: str = Depends(get_current_user)) -> list[dict[str, Any]]:
    """List the ingested documents (active versions only).

    Reads from the ingestion registry (ingested_files.json) — the single source
    of truth the ingestion pipeline populates. Only the current (active) version
    of each lineage is listed; superseded versions are hidden here but remain
    queryable via ``/documents/{id}/versions``.
    """
    from src.rag.ingestion_registry import IngestionRegistry

    registry = IngestionRegistry()
    all_entries = list(registry.get_all().values())

    if current_user and current_user not in ("*", "all", "anonymous", "admin"):
        all_entries = [
            e for e in all_entries
            if current_user in e.get("allowed_users", []) or (not e.get("allowed_users") and e.get("user_id") == current_user)
        ]

    # Count versions per lineage so the UI can show "v3" affordances.
    version_counts: dict[str, int] = {}
    for e in all_entries:
        root = e.get("lineage_root", e.get("document_id", ""))
        version_counts[root] = version_counts.get(root, 0) + 1

    documents = [
        _doc_view(e, version_counts.get(e.get("lineage_root", e.get("document_id", "")), 1))
        for e in all_entries
        if e.get("active", True)
    ]
    documents.sort(key=lambda d: d.get("ingestedAt", ""), reverse=True)
    return documents


@router.get("/documents/{document_id}/versions")
async def get_document_versions(
    document_id: str,
    current_user: str = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return the full version history of a document's lineage, oldest first."""
    from src.rag.ingestion_registry import IngestionRegistry

    registry = IngestionRegistry()
    entry = registry.get_by_document_id(document_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Document not found")

    if current_user and current_user not in ("*", "all", "anonymous", "admin"):
        allowed = entry.get("allowed_users", [])
        if current_user not in allowed and entry.get("user_id") != current_user:
            raise HTTPException(status_code=403, detail="Access denied to this document")

    root = entry.get("lineage_root", document_id)
    versions = registry.get_versions(root)
    return [
        {**_doc_view(v, len(versions)), "active": v.get("active", True)}
        for v in versions
    ]


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    current_user: str = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a document version from both the vector store and the registry (admin only)."""
    from src.rag.ingestion_registry import IngestionRegistry
    from src.rag.stages.s11_vector_store import QdrantStore

    registry = IngestionRegistry()
    entry = registry.get_by_document_id(document_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        await QdrantStore().delete_document(document_id)
    except Exception:
        logger.exception("Failed to delete vectors for document %s", document_id)
        raise HTTPException(status_code=500, detail="Failed to delete document vectors")

    registry.unregister(document_id)
    return {"status": "deleted", "document_id": document_id}


@router.get("/users")
async def get_users_list(_admin: str = Depends(require_admin)) -> dict[str, Any]:
    """List all available users for document access assignment (admin only)."""
    try:
        from config.alpha_users import ALPHA_USERS
    except ImportError:
        from src.api.auth import ALPHA_USERS
    users = [u for u in ALPHA_USERS if u != "admin"]
    return {"users": users, "all_users": list(ALPHA_USERS.keys())}


@router.get("/documents/{document_id}/access")
async def get_document_access(
    document_id: str,
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Get the list of allowed users for a document (admin only)."""
    from src.rag.ingestion_registry import IngestionRegistry

    registry = IngestionRegistry()
    entry = registry.get_by_document_id(document_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "document_id": document_id,
        "allowed_users": entry.get("allowed_users", []),
    }


@router.post("/documents/{document_id}/access")
async def update_document_access(
    document_id: str,
    body: DocumentAccessPayload,
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """Update user access for a document (admin only)."""
    from src.rag.ingestion_registry import IngestionRegistry

    registry = IngestionRegistry()
    ok = registry.update_document_access(document_id, body.allowed_users)
    if not ok:
        raise HTTPException(status_code=404, detail="Document not found")
    entry = registry.get_by_document_id(document_id)
    return {
        "status": "ok",
        "document_id": document_id,
        "allowed_users": entry.get("allowed_users", []) if entry else body.allowed_users,
    }
