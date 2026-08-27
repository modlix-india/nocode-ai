"""Vision-capability resolution for the AppBuilder's configured model.

The AppBuilder runs on DeepSeek, where vision is a per-MODEL property rather
than a per-provider one: `deepseek-v4-flash-vision-exp` accepts image input,
`deepseek-v4-pro` / `deepseek-v4-flash` reject it. Everything downstream keys
off this one answer — whether screenshots ride along as image parts, and
whether the Gemini-describe fallback is wired up — so a wrong answer either
sends images to a model that 400s on them or silently pays Gemini to describe
images the model could already read.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.llm_provider import (
    _DEEPSEEK_VISION_MODELS,
    appbuilder_vision_capable,
    get_llm_provider,
    reset_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """`get_llm_provider` caches per provider name, so a test that rebinds a
    model has to drop the cached instance built from the previous one."""
    yield
    reset_provider()


def _use_deepseek(monkeypatch, model: str, tier: str = "balanced") -> None:
    monkeypatch.setattr(settings, "APPBUILDER_PROVIDER", "deepseek")
    monkeypatch.setattr(settings, "AGENT_MODEL_TIER", tier)
    monkeypatch.setattr(settings, "DEEPSEEK_MODEL_BALANCED", model)
    reset_provider()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("deepseek-v4-flash-vision-exp", True),
        ("deepseek-v4-pro", False),
        ("deepseek-v4-flash", False),
        ("some-future-deepseek-model", False),  # unknown ids must not opt in
    ],
)
def test_deepseek_vision_is_decided_per_model(monkeypatch, model: str, expected: bool) -> None:
    _use_deepseek(monkeypatch, model)
    assert appbuilder_vision_capable() is expected


@pytest.mark.parametrize("model", ["deepseek-v4-flash-vision-exp", "deepseek-v4-pro"])
def test_provider_flag_agrees_with_module_helper(monkeypatch, model: str) -> None:
    """`DeepSeekProvider.supports_image_in_tool_result` drives the message
    converter; `appbuilder_vision_capable()` drives tool registration and
    screenshot routing. If the two ever disagree, the agent gets a model that
    is sent images but told to expect text, or vice versa."""
    _use_deepseek(monkeypatch, model)
    provider = get_llm_provider("deepseek")
    assert provider.supports_image_in_tool_result == appbuilder_vision_capable()


def test_vision_follows_the_tier_the_agent_actually_runs(monkeypatch) -> None:
    """Capability must be read off AGENT_MODEL_TIER, not off "balanced".

    With the vision model on `balanced`, an agent pinned to `fast` is running
    text-only DeepSeek and must not be handed images.
    """
    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp", tier="balanced")
    assert appbuilder_vision_capable() is True

    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp", tier="fast")
    assert appbuilder_vision_capable() is False  # -> deepseek-v4-flash


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", True),
        ("openai", True),
        ("minimax", True),
        # Gemini reads images natively but GeminiProvider does not implement
        # the image-in-tool_result path, so claiming vision here would drop
        # screenshots instead of forwarding them.
        ("gemini", False),
    ],
)
def test_non_deepseek_providers_keep_their_prior_answer(
    monkeypatch, provider: str, expected: bool,
) -> None:
    """Regression guard: replacing the old hardcoded provider allowlist with a
    model-aware check must not have moved any other provider."""
    monkeypatch.setattr(settings, "APPBUILDER_PROVIDER", provider)
    assert appbuilder_vision_capable() is expected


@pytest.mark.parametrize(
    ("llm_provider", "expected"), [("anthropic", True), ("gemini", False)],
)
def test_unset_appbuilder_provider_follows_llm_provider(
    monkeypatch, llm_provider: str, expected: bool,
) -> None:
    """An empty APPBUILDER_PROVIDER resolves to LLM_PROVIDER.

    That mirrors `get_llm_provider`, which does `provider_name or
    settings.LLM_PROVIDER` — so the model the agent actually runs on is the
    LLM_PROVIDER one, and capability has to be read from there too. The old
    provider-name allowlist skipped this fallback and reported text-only for
    an unset override, which meant an Anthropic-backed AppBuilder paid Gemini
    to describe screenshots Claude could already see.
    """
    monkeypatch.setattr(settings, "APPBUILDER_PROVIDER", "")
    monkeypatch.setattr(settings, "LLM_PROVIDER", llm_provider)
    assert appbuilder_vision_capable() is expected


def test_configured_appbuilder_model_is_a_known_vision_model() -> None:
    """The shipped default is meant to be the vision model — catch a config
    edit that silently drops the AppBuilder back to text-only."""
    assert settings.DEEPSEEK_MODEL_BALANCED in _DEEPSEEK_VISION_MODELS
