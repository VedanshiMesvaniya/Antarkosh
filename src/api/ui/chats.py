"""Chat endpoints for UI API — conversations, streaming messages, titles, documents, feedback."""

from __future__ import annotations

import datetime
import json
import logging
import re
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.auth import get_current_user
from src.core.config import settings
from src.core.provider_client import ProviderRouter
from src.core.state import state_manager
from src.models.schemas import ThinkingStep
from src.pipeline.query import QueryPipeline
from src.sql.context import DEFAULT_DB_ID
from src.sql.knowledge.loaders import validate_db_id
from src.sql.learning.pipeline_metrics import log_event as _log_pipeline_event

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_chat_access(chat_id: str, user_id: str | None) -> None:
    """Ensure current user owns this chat (admin may open any chat)."""
    if user_id in ("admin", "*", "all"):
        return
    if not user_id or user_id == "anonymous":
        raise HTTPException(status_code=401, detail="Please login.")
    chats = state_manager.get_chats()
    target = next((c for c in chats if c.get("id") == chat_id), None)
    if target:
        owner = target.get("userId") or target.get("user_id")
        # A chat with no recorded owner is not anyone's to open except admin.
        if owner not in (user_id, "system", "shared"):
            raise HTTPException(status_code=403, detail="Access denied to this chat")


def json_serial(obj: Any) -> Any:
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


def sanitize_message_for_json(msg: Any) -> Any:
    """Recursively converts non-serializable objects (dates, decimals, etc.) into JSON primitives."""
    if msg is None or isinstance(msg, (str, int, float, bool)):
        return msg
    if isinstance(msg, (datetime.date, datetime.datetime, datetime.time)):
        return msg.isoformat()
    if isinstance(msg, Decimal):
        return int(msg) if msg % 1 == 0 else float(msg)
    if isinstance(msg, bytes):
        return msg.decode("utf-8", errors="replace")
    if isinstance(msg, dict):
        return {k: sanitize_message_for_json(v) for k, v in msg.items()}
    if isinstance(msg, (list, tuple, set)):
        return [sanitize_message_for_json(v) for v in msg]
    return str(msg)


# Human-readable labels + display order for the provider picker. OpenRouter
# leads because it's the default soft pin.
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "gemini": "Gemini",
    "groq": "Groq",
    "nvidia_nim": "NVIDIA NIM",
}
_PROVIDER_ORDER = ["openrouter", "gemini", "groq", "nvidia_nim"]


_DOCUMENT_PROMPT = """You are a professional report writer. Convert the following conversation between a user and an AI assistant into a polished, standalone professional document in Markdown.

Requirements:
- Begin with a single H1 title (`# Title`) and a one-paragraph executive summary.
- Organize the content into logical sections with `##` headings; use `###` for sub-points.
- Write in clear, professional prose. Do NOT reproduce the chat turns verbatim and do NOT refer to "the user" or "the assistant".
- Preserve every fact, figure, and inline citation marker (like [1], [2]) exactly as they appear.
- Where the conversation contains quantitative data (comparisons, rankings, distributions, trends, totals), ADD a fitting chart as a fenced ```mermaid code block:
  - Bar/line data → `xychart-beta`
  - Proportions/shares → `pie`
  Only add a chart when the underlying numbers are actually present in the conversation. Never invent data. If nothing is chartable, add no charts.
- If the conversation already includes charts, keep and refine them.
- If a References/Sources list is present, keep it at the end.

Output ONLY the Markdown document — no preamble, no code fences around the whole thing.

Conversation:
---
{conversation}
---"""


_TITLE_PROMPT = """You generate a short semantic title for a user's request.

Identify the main subject and the user's intent. Return exactly one natural title containing
4 to 6 meaningful words. Do not simply copy the first few words of the question.

Examples:
- Which warehouses are low on stock right now? -> Low Stock Across Warehouses
- show me sales by state -> Sales Performance By State
- what is the apple tax rate -> Apple Tax Rate Information
- how many stock adjustments happened today -> Today's Stock Adjustment Count
- what is the unit name of product CAP03 -> Unit Name For Product CAP03

Use Title Case. Do not write a question, add punctuation, or include words like "What", "How",
"Why", "Can", "Give", "Show", "Tell", or "Chat". Preserve important product names, IDs,
years, database entities, and technical terms. If there is no meaningful topic, reply exactly:
New Chat.

Question:
{question}

Title:"""


def _clean_title(raw: str) -> str:
    """Normalize an LLM title response into a clean, bounded 4-6 word title string."""
    text = (raw or "").strip()
    if not text:
        return ""
    # Take the first non-empty line only.
    text = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    # Drop a leading "Title:" / "Chat title -" the model may echo back.
    text = re.sub(r"^(chat\s+)?title\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    # Strip surrounding quotes and trailing sentence punctuation.
    text = text.strip().strip("\"'“”‘’").strip()
    text = text.rstrip(".!?,;:").strip()
    words = text.split()
    if len(words) > 6:
        words = words[:6]
    stop_words = {"of", "for", "in", "on", "at", "to", "from", "by", "and", "the", "a", "an", "with"}
    while len(words) > 1 and words[-1].lower() in stop_words:
        words.pop()
    if len(words) < 4:
        return ""
    text = " ".join(words)
    if len(text) > 45:
        text = text[:42].rstrip() + "..."
    return text


def _fallback_title(prompt: str) -> str:
    """Deterministic fallback when LLM titling is unavailable: extract a 5-6 word topic."""
    text = (prompt or "").strip()
    if not text:
        return "New Chat"

    prefix_pat = (
        r"^(?:"
        r"(?:what|which)\s+(?:is\s+(?:the\s+|a\s+)?|are\s+(?:the\s+)?|was\s+(?:the\s+|a\s+)?|were\s+|specific\s+|products?\s+does\s+|does\s+|do\s+|kind\s+of\s+|type\s+of\s+)"
        r"|what\s+"
        r"|which\s+"
        r"|give\s+me(?:\s+(?:the|a|all))?\s+"
        r"|show\s+me(?:\s+(?:the|a|all))?\s+"
        r"|list(?:\s+(?:all\s+the|all|of|the|distinct))?\s+"
        r"|how\s+(?:many|much|to|do\s+i|can\s+i)\s+"
        r"|can\s+you(?:\s+(?:tell|give|show|list))?(?:\s+me)?(?:\s+about)?\s+"
        r"|tell\s+me(?:\s+about)?\s+"
        r"|find(?:\s+(?:all\s+the|all|the))?\s+"
        r"|please\s+"
        r")"
    )
    cleaned = re.sub(prefix_pat, "", text, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"[\?\.!\'\"`]+$", "", cleaned).strip()
    if not cleaned:
        cleaned = re.sub(r"[\?\.!\'\"`]+$", "", text).strip()

    words = cleaned.split()
    if not words:
        return "New Chat"

    stop_words = {
        "of", "for", "in", "on", "at", "to", "from", "by", "and", "the", "a", "an", "with",
        "are", "is", "were", "was", "does", "do",
    }
    selected = words[:6]
    while len(selected) > 1 and selected[-1].lower() in stop_words:
        selected.pop()

    title_words = [w if (w.isupper() and len(w) <= 5) else w.capitalize() for w in selected]
    return " ".join(title_words)


def _resolve_provider(requested: str | None) -> str | None:
    """Resolve the effective soft-pin provider for a request.

    Precedence: explicit request value → saved UI setting → app default.
    Returns None (no pin, "auto" routing) when the resolved value is "auto".
    """
    candidate = requested
    if candidate is None:
        candidate = state_manager.get_settings().get("provider")
    if candidate is None:
        candidate = settings.default_provider

    normalized = (candidate or "").strip().lower()
    return normalized if normalized and normalized != "auto" else None


class ChatCreate(BaseModel):
    title: str = "New Chat"


class ChatUpdate(BaseModel):
    title: str


class SendMessage(BaseModel):
    message: str
    provider: str | None = None
    mode: str = "auto"
    db_id: str | None = None


class MessageFeedback(BaseModel):
    feedback: str | None = None
    comment: str | None = None


class IngestionCard(BaseModel):
    """A persisted ingestion-progress card (the step-by-step upload trace)."""
    id: str
    fileName: str
    status: str
    steps: list[dict[str, Any]]
    summary: dict[str, Any] | None = None
    content: str = ""
    createdAt: str


@router.get("/chats")
async def get_chats(current_user: str = Depends(get_current_user)) -> list[dict[str, Any]]:
    """List all chats."""
    return state_manager.get_chats(user_id=current_user)


@router.post("/chats")
async def create_chat(
    chat_data: ChatCreate,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a new chat."""
    chat = {
        "id": f"chat-{uuid.uuid4().hex[:8]}",
        "title": chat_data.title,
        "updatedAt": datetime.datetime.now(datetime.UTC).isoformat(),
        "userId": current_user,
        "user_id": current_user,
    }
    state_manager.create_chat(chat)
    return chat


@router.patch("/chats/{chat_id}")
async def update_chat(
    chat_id: str,
    chat_data: ChatUpdate,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Rename a chat."""
    _check_chat_access(chat_id, current_user)
    updated = state_manager.update_chat(chat_id, {"title": chat_data.title})
    if not updated:
        raise HTTPException(status_code=404, detail="Chat not found")
    return updated


@router.delete("/chats/{chat_id}")
async def delete_chat(
    chat_id: str,
    current_user: str = Depends(get_current_user),
) -> dict[str, str]:
    """Delete a chat."""
    _check_chat_access(chat_id, current_user)
    state_manager.delete_chat(chat_id)
    return {"status": "success"}


@router.get("/chats/{chat_id}/messages")
async def get_messages(
    chat_id: str,
    current_user: str = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Get all messages for a chat."""
    _check_chat_access(chat_id, current_user)
    return state_manager.get_messages(chat_id)


@router.post("/chats/{chat_id}/messages")
async def send_message(
    chat_id: str,
    msg: SendMessage,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Send a message to a chat, process it via RAG, and return the response."""
    _check_chat_access(chat_id, current_user)
    history = state_manager.get_messages(chat_id)

    user_message = {
        "id": f"msg-u-{uuid.uuid4().hex[:8]}",
        "role": "user",
        "content": msg.message,
        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
        "chatId": chat_id,
        "userId": current_user,
        "user_id": current_user,
    }
    state_manager.add_message(chat_id, user_message)

    target_db_id = DEFAULT_DB_ID
    if msg.db_id is not None:
        try:
            validate_db_id(msg.db_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        target_db_id = msg.db_id.strip()
        from src.sql.registry import can_access_database

        if not can_access_database(target_db_id, current_user):
            raise HTTPException(status_code=403, detail="Access denied to this database")

    try:
        pipeline = QueryPipeline(preferred_provider=_resolve_provider(msg.provider), db_id=target_db_id)
        filters = {"user_id": current_user}
        result = await pipeline.query(msg.message, filters=filters, history=history, mode=msg.mode, db_id=target_db_id)

        assistant_message = {
            "id": f"msg-a-{uuid.uuid4().hex[:8]}",
            "role": "assistant",
            "content": result.answer,
            "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
            "chatId": chat_id,
            "citations": [c.model_dump() for c in result.citations],
            "modelUsed": result.model_used,
            "usage": result.usage.model_dump(),
            "sqlPayload": result.sql_payload,
        }
        assistant_message = sanitize_message_for_json(assistant_message)
        state_manager.add_message(chat_id, assistant_message)
        state_manager.update_chat(chat_id, {"updatedAt": datetime.datetime.now(datetime.UTC).isoformat()})
        
        return assistant_message

    except Exception:
        logger.exception("Failed to process message")
        error_message = {
            "id": f"msg-e-{uuid.uuid4().hex[:8]}",
            "role": "assistant",
            "content": "Sorry, I wasn't able to process your request. Our AI providers may be temporarily unavailable — please try again in a moment.",
            "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
            "chatId": chat_id,
        }
        state_manager.add_message(chat_id, error_message)
        return error_message


@router.post("/chats/{chat_id}/messages/stream")
async def send_message_stream(
    chat_id: str,
    msg: SendMessage,
    current_user: str = Depends(get_current_user),
):
    """Send a message to a chat and stream the RAG response via SSE."""
    _check_chat_access(chat_id, current_user)
    history = state_manager.get_messages(chat_id)

    user_message = {
        "id": f"msg-u-{uuid.uuid4().hex[:8]}",
        "role": "user",
        "content": msg.message,
        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
        "chatId": chat_id,
        "userId": current_user,
        "user_id": current_user,
    }
    state_manager.add_message(chat_id, user_message)

    target_db_id = DEFAULT_DB_ID
    if msg.db_id is not None:
        try:
            validate_db_id(msg.db_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        target_db_id = msg.db_id.strip()
        from src.sql.registry import can_access_database

        if not can_access_database(target_db_id, current_user):
            raise HTTPException(status_code=403, detail="Access denied to this database")

    pipeline = QueryPipeline(preferred_provider=_resolve_provider(msg.provider), db_id=target_db_id)
    filters = {"user_id": current_user}

    async def event_generator():
        try:
            async for chunk in pipeline.query_stream(msg.message, filters=filters, history=history, mode=msg.mode, db_id=target_db_id):
                if isinstance(chunk, ThinkingStep):
                    yield f"data: {json.dumps({'type': 'thinking', 'step': chunk.model_dump()}, default=json_serial)}\n\n"
                elif isinstance(chunk, str):
                    yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"
                else:
                    assistant_message = {
                        "id": f"msg-a-{uuid.uuid4().hex[:8]}",
                        "role": "assistant",
                        "content": chunk.answer,
                        "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
                        "chatId": chat_id,
                        "citations": [c.model_dump() for c in chunk.citations],
                        "modelUsed": chunk.model_used,
                        "thinking": [t.model_dump() for t in chunk.thinking],
                        "usage": chunk.usage.model_dump(),
                        "sqlPayload": chunk.sql_payload,
                    }
                    assistant_message = sanitize_message_for_json(assistant_message)
                    state_manager.add_message(chat_id, assistant_message)
                    state_manager.update_chat(chat_id, {"updatedAt": datetime.datetime.now(datetime.UTC).isoformat()})
                    
                    yield f"data: {json.dumps({'type': 'done', 'message': assistant_message}, default=json_serial)}\n\n"
        except Exception:
            logger.exception("Failed to process stream message")
            error_message = {
                "id": f"msg-e-{uuid.uuid4().hex[:8]}",
                "role": "assistant",
                "content": "Sorry, I wasn't able to process your request. Our AI providers may be temporarily unavailable.",
                "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
                "chatId": chat_id,
            }
            state_manager.add_message(chat_id, error_message)
            yield f"data: {json.dumps({'type': 'error', 'message': error_message}, default=json_serial)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/chats/{chat_id}/messages/ingestion")
async def persist_ingestion_card(chat_id: str, card: IngestionCard) -> dict[str, Any]:
    """Persist a finished ingestion-progress card so it survives a reload."""
    message = {
        "id": card.id,
        "role": "assistant",
        "kind": "ingestion",
        "fileName": card.fileName,
        "status": card.status,
        "steps": card.steps,
        "summary": card.summary,
        "content": card.content,
        "createdAt": card.createdAt,
        "chatId": chat_id,
    }
    state_manager.add_message(chat_id, message)
    state_manager.update_chat(chat_id, {"updatedAt": datetime.datetime.now(datetime.UTC).isoformat()})
    return {"status": "ok"}


@router.post("/chats/{chat_id}/messages/{message_id}/feedback")
async def set_message_feedback(
    chat_id: str, message_id: str, body: MessageFeedback
) -> dict[str, Any]:
    """Persist a thumbs up/down rating and/or comment on an assistant message."""
    if body.feedback not in (None, "up", "down"):
        raise HTTPException(status_code=400, detail="feedback must be 'up', 'down', or null")

    updated = state_manager.set_message_feedback(chat_id, message_id, body.feedback, body.comment)
    if not updated:
        raise HTTPException(status_code=404, detail="Message not found")

    if body.feedback or body.comment:
        messages = state_manager.get_messages(chat_id)
        msg = next((m for m in messages if m.get("id") == message_id), {})
        
        event_type = f"user_feedback_{body.feedback}" if body.feedback else "user_feedback_comment"
        score_delta = 0
        if body.feedback == "up":
            score_delta = +1
        elif body.feedback == "down":
            score_delta = -1

        _log_pipeline_event(
            event_type,
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "comment": body.comment,
                "answer_preview": (msg.get("content") or "")[:200],
                "model_used": msg.get("modelUsed"),
            },
            score_delta=score_delta,
        )

    return {"status": "ok", "feedback": body.feedback}


@router.post("/chats/{chat_id}/document")
async def generate_chat_document(
    chat_id: str,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Restructure a chat into a professional Markdown document (with charts)."""
    _check_chat_access(chat_id, current_user)
    messages = state_manager.get_messages(chat_id)
    turns: list[str] = []
    for m in messages:
        if m.get("kind") == "ingestion" or m.get("status") == "loading":
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        role = "User" if m.get("role") == "user" else "Assistant"
        turns.append(f"{role}: {content}")

    if not turns:
        raise HTTPException(status_code=400, detail="This chat has no content to build a document from.")

    conversation = "\n\n".join(turns)[:12000]

    try:
        provider_router = ProviderRouter()
        markdown = await provider_router.chat(
            "general_qa",
            messages=[{"role": "user", "content": _DOCUMENT_PROMPT.format(conversation=conversation)}],
            temperature=0.4,
            max_tokens=4096,
        )
    except Exception:
        logger.exception("Document generation failed for chat %s", chat_id)
        raise HTTPException(status_code=502, detail="Could not generate the document. Please try again.")

    markdown = (markdown or "").strip()
    markdown = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", markdown).strip()
    if not markdown:
        raise HTTPException(status_code=502, detail="The generated document was empty. Please try again.")

    title_match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Document"

    return {"markdown": markdown, "title": title}


@router.post("/chats/{chat_id}/title")
async def generate_chat_title(
    chat_id: str,
    current_user: str = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate a concise, topic-aware title from a chat's first exchange."""
    _check_chat_access(chat_id, current_user)
    messages = state_manager.get_messages(chat_id)
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    if not first_user or not (first_user.get("content") or "").strip():
        return {"title": None}

    question = first_user["content"].strip()[:600]

    title = ""
    try:
        provider_router = ProviderRouter()
        raw = await provider_router.chat(
            "chat_title",
            messages=[{"role": "user", "content": _TITLE_PROMPT.format(question=question)}],
            temperature=0.3,
            max_tokens=120,
        )
        title = _clean_title(raw)
    except Exception:  # noqa: BLE001
        logger.warning("Title generation LLM call failed — falling back to trimmed prompt")

    if not title or title.lower() == "new chat":
        title = _fallback_title(first_user["content"])

    state_manager.update_chat(chat_id, {"title": title})
    return {"title": title}
