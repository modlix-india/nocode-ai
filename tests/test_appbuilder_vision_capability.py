"""Vision-capability resolution for the AppBuilder's configured model.

The AppBuilder runs on DeepSeek, where vision is a per-MODEL property rather
than a per-provider one: `deepseek-flash` accepts image input and
`deepseek-v4-pro` rejects it. Everything downstream keys
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


# The one DeepSeek model that still rejects image parts. Named once so the
# tests below say WHY they expect no vision rather than repeating an id.
TEXT_ONLY = "deepseek-v4-pro"


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """`get_llm_provider` caches per provider name, so a test that rebinds a
    model has to drop the cached instance built from the previous one."""
    yield
    reset_provider()


def _use_deepseek(
    monkeypatch, model: str, tier: str = "balanced", fast_model: str = TEXT_ONLY,
) -> None:
    """Pin BOTH tiers, never just one.

    Pinning only `balanced` left the fast tier reading whatever production was
    configured to, so the tier test below passed for a while on the accident
    that the shipped fast model happened to be text-only. It is not any more --
    both tiers now name `deepseek-flash` -- and a test should not depend on
    that either way.
    """
    monkeypatch.setattr(settings, "APPBUILDER_PROVIDER", "deepseek")
    monkeypatch.setattr(settings, "AGENT_MODEL_TIER", tier)
    monkeypatch.setattr(settings, "DEEPSEEK_MODEL_BALANCED", model)
    monkeypatch.setattr(settings, "DEEPSEEK_MODEL_FAST", fast_model)
    reset_provider()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("deepseek-flash", True),
        # Both legacy ids are retired and now served by `deepseek-flash`, which
        # has vision -- so the one that used to be text-only reads as capable.
        ("deepseek-v4-flash-vision-exp", True),
        ("deepseek-v4-flash", True),
        ("deepseek-v4-pro", False),
        ("some-future-deepseek-model", False),  # unknown ids must not opt in
    ],
)
def test_deepseek_vision_is_decided_per_model(monkeypatch, model: str, expected: bool) -> None:
    _use_deepseek(monkeypatch, model)
    assert appbuilder_vision_capable() is expected


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
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

    With a vision model on `balanced` and a text-only one on `fast`, an agent
    pinned to `fast` must not be handed images.

    Shipped config now names `deepseek-flash` on BOTH tiers, so this reads off
    pinned values rather than the defaults -- the property under test is that
    capability follows AGENT_MODEL_TIER, not which model happens to be on it.
    """
    _use_deepseek(monkeypatch, "deepseek-flash", tier="balanced")
    assert appbuilder_vision_capable() is True

    _use_deepseek(monkeypatch, "deepseek-flash", tier="fast")
    assert appbuilder_vision_capable() is False  # -> fast is TEXT_ONLY

    _use_deepseek(monkeypatch, TEXT_ONLY, tier="fast", fast_model="deepseek-flash")
    assert appbuilder_vision_capable() is True


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
