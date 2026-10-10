"""UI API package — aggregated router for chats, documents, databases, settings, and telemetry."""

from __future__ import annotations

import sys
import types
from typing import Any

from fastapi import APIRouter

from src.api.ui import chats
from src.api.ui import databases as dbs_mod
from src.api.ui import documents as docs_mod
from src.api.ui import settings as settings_mod
from src.api.ui import telemetry as telem_mod
from src.api.ui.chats import (
    _DOCUMENT_PROMPT,
    _PROVIDER_LABELS,
    _PROVIDER_ORDER,
    _TITLE_PROMPT,
    ChatCreate,
    ChatUpdate,
    IngestionCard,
    MessageFeedback,
    SendMessage,
    _check_chat_access,
    _clean_title,
    _fallback_title,
    _resolve_provider,
    json_serial,
    sanitize_message_for_json,
)
from src.api.ui.chats import router as chats_router
from src.api.ui.databases import (
    DatabaseAccessPayload,
    DBConnectionPayload,
)
from src.api.ui.databases import router as databases_router
from src.api.ui.documents import (
    DocumentAccessPayload,
    _doc_view,
)
from src.api.ui.documents import router as documents_router
from src.api.ui.settings import (
    _PROVIDER_METADATA,
    get_provider_usage,
)
from src.api.ui.settings import router as settings_router
from src.api.ui.telemetry import router as telemetry_router

router = APIRouter()

# Register sub-routers preserving complete route catalogue
router.include_router(settings_router)
router.include_router(chats_router)
router.include_router(documents_router)
router.include_router(databases_router)
router.include_router(telemetry_router)

__all__ = [
    "_DOCUMENT_PROMPT",
    "_PROVIDER_LABELS",
    "_PROVIDER_METADATA",
    "_PROVIDER_ORDER",
    "_TITLE_PROMPT",
    "ChatCreate",
    "ChatUpdate",
    "DBConnectionPayload",
    "DatabaseAccessPayload",
    "DocumentAccessPayload",
    "IngestionCard",
    "MessageFeedback",
    "SendMessage",
    "_check_chat_access",
    "_clean_title",
    "_doc_view",
    "_fallback_title",
    "_resolve_provider",
    "chats_router",
    "databases_router",
    "documents_router",
    "get_provider_usage",
    "json_serial",
    "router",
    "sanitize_message_for_json",
    "settings_router",
    "telemetry_router",
]


class _UIModule(types.ModuleType):
    """Dynamic module proxy that forwards attribute access and monkeypatching to sub-routers."""

    _SUBMODULES = (chats, docs_mod, dbs_mod, settings_mod, telem_mod)

    def __getattr__(self, name: str) -> Any:
        for sub in self._SUBMODULES:
            if hasattr(sub, name):
                return getattr(sub, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name != "_SUBMODULES":
            for sub in getattr(self, "_SUBMODULES", ()):
                if hasattr(sub, name):
                    setattr(sub, name, value)


sys.modules[__name__].__class__ = _UIModule
