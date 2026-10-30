"""Multi-key rotation (Gemini), key-scaled rate limits, empty-completion diagnostics and the OpenRouter reasoning routes."""

import logging
from types import SimpleNamespace

import httpx
import pytest

from src.core import rate_limiter as rl
from src.core.provider_client import GeminiProvider, ProviderRouter, _log_empty_completion
from src.core.rate_limiter import DEFAULT_LIMITS, RateLimiter

_OK = {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=httpx.Request("POST", "http://x"), response=httpx.Response(self.status_code))

    def json(self):
        return self._payload


class _HTTP:
    """Fake client: answers per key (taken from the x-goog-api-key header)."""

    def __init__(self, status_by_key):
        self.status_by_key = status_by_key
        self.keys_used = []

    async def post(self, url, json=None, headers=None):
        key = headers["x-goog-api-key"]
        self.keys_used.append(key)
        status = self.status_by_key.get(key, 200)
        return _Resp(status, _OK if status == 200 else None)


def _provider(keys, status_by_key=None):
    p = GeminiProvider(RateLimiter())
    p._api_key = keys
    p._http = _HTTP(status_by_key or {})
    return p


_MSG = [{"role": "user", "content": "hi"}]


async def test_single_key_behaves_as_before():
    p = _provider("k1")
    assert p.is_available and await p.chat(_MSG, model="gemini-3.5-flash") == "answer"
    assert p._http.keys_used == ["k1"]


async def test_calls_are_spread_across_keys():
    p = _provider("k1,k2")
    for _ in range(4):
        await p.chat(_MSG, model="gemini-3.5-flash")
    assert p._http.keys_used == ["k1", "k2", "k1", "k2"]


async def test_rate_limited_key_rotates_to_the_next_one():
    p = _provider("k1,k2", {"k1": 429})
    assert await p.chat(_MSG, model="gemini-3.5-flash") == "answer"
    assert p._http.keys_used == ["k1", "k2"]


async def test_all_keys_rate_limited_raises_like_before():
    p = _provider("k1,k2", {"k1": 429, "k2": 429})
    with pytest.raises(httpx.HTTPStatusError):
        await p.chat(_MSG, model="gemini-3.5-flash")


async def test_non_429_error_is_not_retried_on_another_key():
    p = _provider("k1,k2", {"k1": 503})
    with pytest.raises(httpx.HTTPStatusError):
        await p.chat(_MSG, model="gemini-3.5-flash")
    assert p._http.keys_used == ["k1"]


def test_no_key_means_unavailable():
    assert not _provider("").is_available


def test_limits_scale_with_key_count(monkeypatch):
    import src.core.config as cfg

    monkeypatch.setattr(cfg.settings, "gemini_api_key", "a,b,c")
    monkeypatch.setattr(cfg.settings, "groq_api_key", "only-one")
    monkeypatch.setattr(cfg.settings, "openrouter_api_key", "")
    scaled = rl._scaled_default_limits()
    assert scaled["gemini"].rpm == DEFAULT_LIMITS["gemini"].rpm * 3
    assert scaled["gemini"].tpd == DEFAULT_LIMITS["gemini"].tpd * 3
    assert scaled["groq"] == DEFAULT_LIMITS["groq"]
    assert scaled["openrouter"] == DEFAULT_LIMITS["openrouter"]


def test_empty_completion_is_described_without_raising(caplog):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=None, reasoning_content="thinking...", model_extra={}))],
        usage=SimpleNamespace(completion_tokens=768),
    )
    with caplog.at_level(logging.WARNING):
        _log_empty_completion("nvidia_nim", "some-model", resp, 768)
    assert "finish_reason=length" in caplog.text and "reasoning_text_present=True" in caplog.text
    _log_empty_completion("x", "y", object(), 1)  # malformed response: still no exception


def test_reasoning_route_has_the_free_openrouter_models_last():
    from src.core.provider_client import get_task_routing_rules

    route = ProviderRouter()._get_route("reasoning")
    models = [(o.provider_name, o.model) for o in sorted(route.options, key=lambda o: o.priority)]
    assert models[0][0] == "gemini"
    tail = models[-2:]
    assert tail == [("openrouter", "qwen/qwen3.6-plus-preview:free"), ("openrouter", "qwen/qwq-32b:free")]
    assert all(m.endswith(":free") for p, m in models if p == "openrouter")
    assert "openrouter" in get_task_routing_rules()["reasoning"]["fallback"]
