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
        "synthesis", "micro_synthesis",
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
        assert "meta/llama-3.2-11b-vision-instruct" in models and "nvidia/nemotron-3.5-super-vl:free" in models


def test_the_second_gemini_free_model_doubles_the_daily_room_for_the_main_tasks():
    for task in ("general_qa", "reasoning", "repair", "synthesis", "extraction"):
        gem = [p["model"] for p in CONFIG["tasks"][task]["providers"] if p["provider"] == "gemini"]
        assert gem == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"], task


def test_provider_routing_puts_nvidia_before_groq_for_the_large_prompt_tasks():
    routing = CONFIG["provider_routing"]
    for task in ("reasoning", "repair", "synthesis"):
        assert routing[task]["preferred"] == ["gemini", "nvidia_nim"] and routing[task]["fallback"][0] == "groq"
    assert routing["micro_synthesis"]["preferred"][0] == "groq"


@pytest.mark.parametrize("model", ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-lightning-30b-a3b"])
def test_a_reasoning_model_asked_for_a_short_answer_gets_room_to_think(model: str):
    assert effective_max_tokens(model, 120) == MIN_THINKING_MAX_TOKENS
    assert effective_max_tokens(model, 4096) == 4096  # a bigger request is never lowered
    assert effective_max_tokens(model, MIN_THINKING_MAX_TOKENS) == MIN_THINKING_MAX_TOKENS


@pytest.mark.parametrize("model", ["meta/llama-3.2-11b-vision-instruct", "meta/llama-3.1-70b-instruct", "google/gemma-4-31b-it:free", "", "gemini-3.1-flash-lite"])
def test_other_models_keep_exactly_the_budget_they_asked_for(model: str):
    assert effective_max_tokens(model, 10) == 10 and effective_max_tokens(model, 120) == 120
