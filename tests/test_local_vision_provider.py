"""Local ingestion router, end to end through the real ProviderRouter.

A tiny HTTP server stands in for Ollama / llama.cpp (same OpenAI-compatible
/v1/chat/completions contract). The real OpenAICompatibleProvider talks to it, so
this covers request format, model selection, the longer timeout, and the fact
that there is NO cloud fallback: when the local model errors, is too slow, or is
not running, the call fails (and the ingestion stage handles that).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.core.config import settings
from src.core.provider_client import build_ingestion_router

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"


class _FakeOllama:
    def __init__(self, mode: str = "ok", delay: float = 0.0) -> None:
        self.mode, self.delay = mode, delay
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.mode == "error":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error": "model crashed"}')
                    return
                payload = {
                    "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": "VISIBLE TEXT:\nHello\n\nMEANING:\nA greeting card."}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                }
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # silence
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


def _router(base_url: str, timeout: float, monkeypatch):
    monkeypatch.setattr(settings, "local_vision_base_url", base_url)
    monkeypatch.setattr(settings, "local_vision_model", "qwen3-vl:4b")
    monkeypatch.setattr(settings, "local_vision_timeout_seconds", timeout)
    return build_ingestion_router()


async def test_local_model_answers_and_request_is_well_formed(monkeypatch):
    with _FakeOllama() as srv:
        router = _router(srv.base_url, 5.0, monkeypatch)
        out = await router.vision("chart_analysis", PNG, "describe", mime_type="image/png")
    assert "greeting card" in out
    assert router.last_used == "local/qwen3-vl:4b"
    req = srv.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["body"]["model"] == "qwen3-vl:4b"
    parts = req["body"]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert req["auth"] == "Bearer local"


async def test_classification_text_call_goes_to_the_local_model(monkeypatch):
    with _FakeOllama() as srv:
        router = _router(srv.base_url, 5.0, monkeypatch)
        out = await router.chat(
            "semantic_classification", messages=[{"role": "user", "content": "classify"}], max_tokens=50
        )
    assert "greeting card" in out
    assert srv.requests[0]["body"]["model"] == "qwen3-vl:4b"


async def test_local_error_raises_with_no_cloud_fallback(monkeypatch):
    with _FakeOllama(mode="error") as srv:
        router = _router(srv.base_url, 5.0, monkeypatch)
        with pytest.raises(RuntimeError, match="All vision providers exhausted"):
            await router.vision("image_understanding", PNG, "describe")
    assert len(srv.requests) >= 1


async def test_local_timeout_raises_instead_of_waiting_forever(monkeypatch):
    with _FakeOllama(delay=2.0) as srv:
        router = _router(srv.base_url, 0.4, monkeypatch)  # tiny timeout just for the test
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="All vision providers exhausted"):
            await router.vision("ocr_vision", PNG, "read this")
        assert time.monotonic() - started < 1.8


async def test_local_server_not_running_raises(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens on this port now
    router = _router(f"http://127.0.0.1:{port}/v1", 5.0, monkeypatch)
    with pytest.raises(RuntimeError, match="All vision providers exhausted"):
        await router.vision("table_extraction", PNG, "x")


async def test_stage_survives_a_failing_local_model(monkeypatch, tmp_path):
    """The vision stages catch the failure: the document still ingests, minus that content."""
    from src.stages import s04_ocr

    monkeypatch.setattr(settings, "local_vision_timeout_seconds", 5.0)
    with _FakeOllama(mode="error") as srv:
        router = _router(srv.base_url, 5.0, monkeypatch)
        assert await s04_ocr._vision_llm_ocr(PNG, router) == ""
