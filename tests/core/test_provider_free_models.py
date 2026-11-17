"""The shipped provider chains use only live free models, keep every task, and reasoning models get room to answer."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.config import settings
from src.core.provider_client import DEFAULT_ROUTES, MIN_THINKING_MAX_TOKENS, effective_max_tokens

CONFIG = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "providers.yaml").read_text(encoding="utf-8"))

# Verified dead on 2026-11-05 (OpenRouter 404 / free window ended / renamed): none may come back by accident.
DEAD = {
    "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwq-32b:free", "qwen/qwen3.6-plus-preview:free", "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free", "qwen/qwen3.6-27b",
}
PROVIDERS = {"gemini", "groq", "nvidia_nim", "openrouter"}


def all_pairs():
    for task, spec in CONFIG["tasks"].items():
        for p in spec["providers"]:
            yield task, p["provider"], p["model"], p["priority"]


def test_every_task_is_kept_and_no_dead_model_is_configured_anywhere():
    assert set(CONFIG["tasks"]) == {
        "semantic_classification", "ocr_vision", "layout_analysis", "table_extraction", "chart_analysis", "image_understanding",
        "general_qa", "reasoning", "extraction", "summarization", "fast_support", "chat_title", "classification", "repair",
        "synthesis", "micro_synthesis", "schema_sync",
    }
    for task, provider, model, _ in all_pairs():
        assert model not in DEAD and provider in PROVIDERS, (task, provider, model)
    for route in DEFAULT_ROUTES.values():
        assert all(o.model not in DEAD for o in route.options)
    assert settings.openrouter_text_model not in DEAD and settings.openrouter_vision_model not in DEAD
    assert settings.openrouter_text_model.endswith(":free") and settings.openrouter_vision_model.endswith(":free")


def test_priorities_are_one_to_n_and_every_chain_has_more_than_one_provider():
    for task, spec in CONFIG["tasks"].items():
        prios = [p["priority"] for p in spec["providers"]]
        assert prios == list(range(1, len(prios) + 1)), task
        assert len({p["provider"] for p in spec["providers"]}) >= 2, task


def test_openrouter_models_are_free_variants_and_vision_tasks_have_a_vision_model():
    for task, provider, model, _ in all_pairs():
        if provider == "openrouter":
            assert model.endswith(":free"), (task, model)
    for task in ("ocr_vision", "layout_analysis", "table_extraction", "chart_analysis", "image_understanding"):
        models = [p["model"] for p in CONFIG["tasks"][task]["providers"]]
        assert "google/gemma-4-26b-a4b-it:free" in models and "meta/llama-3.2-11b-vision-instruct" in models, task


def test_everyday_tasks_go_openrouter_then_groq_then_nvidia_with_gemini_last():
    for task in ("semantic_classification", "general_qa", "extraction", "summarization", "fast_support", "chat_title", "classification", "micro_synthesis"):
        order = [p["provider"] for p in CONFIG["tasks"][task]["providers"]]
        firsts = list(dict.fromkeys(order))
        assert firsts == ["openrouter", "groq", "nvidia_nim", "gemini"], (task, firsts)


def test_schema_sync_batches_use_their_own_chain_and_spare_the_interactive_free_quotas():
    from src.sql.sa.meaning_phase import LLM_TASK

    assert LLM_TASK == "schema_sync"
    order = list(dict.fromkeys(p["provider"] for p in CONFIG["tasks"]["schema_sync"]["providers"]))
    assert order[0] == "nvidia_nim" and order[-2:] == ["openrouter", "gemini"]


def test_large_thinking_goes_gemini_first_with_other_providers_reasoning_models_as_backup():
    for task in ("reasoning", "repair", "synthesis"):
        chain = CONFIG["tasks"][task]["providers"]
        assert chain[0]["provider"] == "gemini"
        assert {p["provider"] for p in chain[1:]} >= {"nvidia_nim"} and len({p["provider"] for p in chain}) >= 3, task
    assert "nvidia/nemotron-3-ultra-550b-a55b" in [p["model"] for p in CONFIG["tasks"]["reasoning"]["providers"]]
    routing = CONFIG["provider_routing"]
    for task in ("reasoning", "repair", "synthesis"):
        assert routing[task]["preferred"][0] == "gemini"
    assert routing["micro_synthesis"]["preferred"] == ["openrouter", "groq"]


def test_every_gemini_model_is_one_this_clients_request_format_accepts():
    """The client sends thinkingBudget:0 and temperature. Google's 3.5 Flash-Lite / 3.6 and newer need thinking_level and reject it
    (HTTP 400, seen in production), so only 3.1 and older may be configured until the client learns the new format."""
    import re

    for task, provider, model, _ in all_pairs():
        if provider == "gemini":
            major_minor = tuple(int(x) for x in re.match(r"gemini-(\d+)(?:\.(\d+))?", model).groups(default="0"))
            assert major_minor <= (3, 1), (task, model)


def test_default_provider_is_auto_so_the_yaml_order_is_not_overridden():
    assert settings.default_provider == "auto"


@pytest.mark.parametrize("model", ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-lightning-30b-a3b"])
def test_a_reasoning_model_asked_for_a_short_answer_gets_room_to_think(model: str):
    assert effective_max_tokens(model, 120) == MIN_THINKING_MAX_TOKENS
    assert effective_max_tokens(model, 4096) == 4096  # a bigger request is never lowered
    assert effective_max_tokens(model, MIN_THINKING_MAX_TOKENS) == MIN_THINKING_MAX_TOKENS


@pytest.mark.parametrize("model", ["meta/llama-3.2-11b-vision-instruct", "meta/llama-3.1-70b-instruct", "google/gemma-4-31b-it:free", "", "gemini-3.1-flash-lite"])
def test_other_models_keep_exactly_the_budget_they_asked_for(model: str):
    assert effective_max_tokens(model, 10) == 10 and effective_max_tokens(model, 120) == 120
