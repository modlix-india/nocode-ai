"""User-attachment images survive the trip from the chat request to the model.

There are TWO independent image paths into the model, and wiring one does not
wire the other:

  (a) screenshots the agent captures itself, which ride in tool results and go
      through `_split_tool_result_content` / `_append_user_list_content`, gated
      on `supports_image_in_tool_result`;
  (b) images a user attaches in the chat UI, which go through
      `base_router.build_image_blocks` -> `provider.format_image_content`.

`test_appbuilder_vision_capability` covers the capability question both paths
ask. It does not cover (b)'s formatting, which is how DeepSeek shipped
answering "yes, I do vision" on path (a) while path (b) fell through to the
base class's `raise NotImplementedError` — every attachment 500'd the chat
endpoint while agent screenshots worked. These tests pin path (b).
"""

from __future__ import annotations

import base64
import io

import pytest

from app.config import settings
from app.core.base_router import ChatAttachment, build_image_blocks
from app.services.llm_provider import (
    DeepSeekProvider,
    MiniMaxProvider,
    get_llm_provider,
    reset_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """`get_llm_provider` caches per provider name, so a test that rebinds a
    model has to drop the cached instance built from the previous one."""
    yield
    reset_provider()


@pytest.fixture
def png_b64() -> str:
    """A real PNG, not a stub — `build_image_blocks` runs the bytes through
    `compress_image_base64`, which decodes them."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (20, 60, 200)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _use_deepseek(monkeypatch, model: str, tier: str = "balanced") -> None:
    monkeypatch.setattr(settings, "APPBUILDER_PROVIDER", "deepseek")
    monkeypatch.setattr(settings, "AGENT_MODEL_TIER", tier)
    monkeypatch.setattr(settings, "DEEPSEEK_MODEL_BALANCED", model)
    reset_provider()


def test_deepseek_formats_an_attachment_as_a_data_uri_image_part(monkeypatch, png_b64: str) -> None:
    """The shape DeepSeek's vision API documents, verified against the live
    endpoint: an OpenAI-compatible `image_url` part carrying a base64 data URI."""
    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp")
    block = get_llm_provider("deepseek").format_image_content(png_b64, "image/png")

    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == f"data:image/png;base64,{png_b64}"


def test_attachment_part_matches_the_tool_result_part(monkeypatch, png_b64: str) -> None:
    """Both paths must hand the model identical blocks.

    `_split_tool_result_content` converts an Anthropic image block into an
    `image_url` part for path (a). If path (b) emitted a different shape, one
    of the two would be silently dropped or 400'd by the API.
    """
    from app.services.llm_provider import _split_tool_result_content

    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp")
    _, tool_result_parts = _split_tool_result_content([{
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": png_b64},
    }])

    attachment_part = get_llm_provider("deepseek").format_image_content(png_b64, "image/png")
    assert tool_result_parts == [attachment_part]


@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg", "image/gif", "image/webp"])
def test_mime_type_is_carried_into_the_data_uri(monkeypatch, png_b64: str, media_type: str) -> None:
    """DeepSeek accepts these four types; the caller's mime must reach the URI
    rather than being flattened to the image/png default."""
    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp")
    block = get_llm_provider("deepseek").format_image_content(png_b64, media_type)
    assert block["image_url"]["url"].startswith(f"data:{media_type};base64,")


def test_build_image_blocks_end_to_end(monkeypatch, png_b64: str) -> None:
    """The exact call the chat endpoint makes. This is the line that raised."""
    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp")
    blocks = build_image_blocks(
        [ChatAttachment(type="image", name="shot.png", mime_type="image/png", data=png_b64)],
        "deepseek",
    )

    assert blocks is not None and len(blocks) == 1
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_non_image_attachments_are_skipped(monkeypatch, png_b64: str) -> None:
    """A file attachment alongside an image must not reach format_image_content
    (it would produce an image part out of arbitrary bytes)."""
    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp")
    blocks = build_image_blocks(
        [
            ChatAttachment(type="file", name="notes.txt", mime_type="text/plain", data=png_b64),
            ChatAttachment(type="image", name="shot.png", mime_type="image/png", data=png_b64),
            ChatAttachment(type="image", name="empty.png", mime_type="image/png", data=None),
        ],
        "deepseek",
    )
    assert blocks is not None and len(blocks) == 1


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_text_only_model_is_refused_with_a_clear_reason(monkeypatch, png_b64: str, model: str) -> None:
    """Text-only V4 chat models reject `image_url` parts with an opaque API
    400. Failing in-process names the model that cannot read the image."""
    _use_deepseek(monkeypatch, model)
    with pytest.raises(NotImplementedError, match=model):
        get_llm_provider("deepseek").format_image_content(png_b64, "image/png")


def test_refusal_follows_the_tier_the_agent_actually_runs(monkeypatch, png_b64: str) -> None:
    """With the vision model on `balanced`, an agent pinned to `fast` runs
    text-only DeepSeek and must not be handed an attachment."""
    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp", tier="fast")
    with pytest.raises(NotImplementedError):
        get_llm_provider("deepseek").format_image_content(png_b64, "image/png")


def test_minimax_inherits_the_deepseek_implementation() -> None:
    """MiniMaxProvider subclasses DeepSeekProvider, so its attachments broke
    the same way and are fixed by the same method. Instantiating it needs a
    MINIMAX_API_KEY that local envs do not set, so assert the binding.
    """
    assert MiniMaxProvider.format_image_content is DeepSeekProvider.format_image_content
    # Pinned True class-wide, so the guard never refuses a MiniMax attachment.
    assert MiniMaxProvider.supports_image_in_tool_result is True


def test_attached_image_survives_the_message_converter(monkeypatch, png_b64: str) -> None:
    """The second half of the trip, and the subtler bug.

    Formatting the block is not enough: `_append_user_list_content` converts
    the stored user turn into OpenAI messages, and it originally matched only
    `tool_result` and `text`. An `image_url` block matched no branch and was
    dropped on the floor — the endpoint returned 200 and the model politely
    answered that nothing was attached.
    """
    from app.services.llm_provider import _append_user_list_content

    _use_deepseek(monkeypatch, "deepseek-v4-flash-vision-exp")
    stored = [  # exactly what session.append_user_message builds
        {"type": "text", "text": "What are these?"},
        get_llm_provider("deepseek").format_image_content(png_b64, "image/png"),
    ]

    out: list[dict] = []
    _append_user_list_content(out, stored)

    assert len(out) == 1, "text and its attachment belong to ONE user turn"
    parts = out[0]["content"]
    assert isinstance(parts, list), "a turn carrying an image cannot be a plain string"
    assert parts[0] == {"type": "text", "text": "What are these?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].endswith(png_b64)


def test_anthropic_shaped_attachment_also_survives(monkeypatch, png_b64: str) -> None:
    """Sessions persisted by an Anthropic-backed run store `image` blocks. If
    the provider is later switched to DeepSeek, that history must still convert
    rather than silently lose its images."""
    from app.services.llm_provider import _append_user_list_content

    out: list[dict] = []
    _append_user_list_content(out, [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
    ])
    assert out[0]["content"][1]["image_url"]["url"] == f"data:image/png;base64,{png_b64}"


def test_attachment_with_no_text_still_reaches_the_model(png_b64: str) -> None:
    """An image sent with an empty message must not vanish."""
    from app.services.llm_provider import _append_user_list_content

    out: list[dict] = []
    _append_user_list_content(out, [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
    ])
    assert len(out) == 1 and out[0]["role"] == "user"
    assert out[0]["content"][0]["type"] == "image_url"


def test_tool_result_screenshots_are_unchanged() -> None:
    """Regression guard on the path that already worked: tool messages first,
    each keyed to its tool_call_id, then ONE user message with the screenshots.
    Interleaving a user message between tool messages breaks OpenAI ordering.
    """
    from app.services.llm_provider import _append_user_list_content

    out: list[dict] = []
    _append_user_list_content(out, [
        {"type": "tool_result", "tool_use_id": "call_1", "content": [
            {"type": "text", "text": "shot taken"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        ]},
        {"type": "tool_result", "tool_use_id": "call_2", "content": "plain result"},
    ])

    assert [m["role"] for m in out] == ["tool", "tool", "user"]
    assert [m.get("tool_call_id") for m in out[:2]] == ["call_1", "call_2"]
    assert out[2]["content"][0]["text"].startswith("Screenshot(s)")
    assert out[2]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_text_only_turn_stays_a_plain_string() -> None:
    """The overwhelmingly common case must not grow a content list."""
    from app.services.llm_provider import _append_user_list_content

    out: list[dict] = []
    _append_user_list_content(out, [{"type": "text", "text": "hello"}])
    assert out == [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize("provider_name", ["anthropic", "openai", "deepseek", "gemini"])
def test_every_vision_capable_provider_implements_the_attachment_path(provider_name: str) -> None:
    """The bug in one sentence: a provider claimed vision but left
    `format_image_content` on the base class. Any provider the AppBuilder can
    be pointed at must override it, or attachments 500 for that provider.
    """
    from app.services.llm_provider import LLMProvider

    provider_cls = {
        "anthropic": "AnthropicProvider",
        "openai": "OpenAIProvider",
        "deepseek": "DeepSeekProvider",
        "gemini": "GeminiProvider",
    }[provider_name]
    cls = getattr(__import__("app.services.llm_provider", fromlist=[provider_cls]), provider_cls)

    assert cls.format_image_content is not LLMProvider.format_image_content, (
        f"{provider_cls} does not override format_image_content; chat attachments "
        f"will raise NotImplementedError for this provider"
    )
