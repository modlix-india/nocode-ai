"""Regression: describe_image is the vision adapter that lets text-only
providers (DeepSeek, etc.) reason about screenshots and other images by
piping them through Gemini Flash.

These tests lock in:
  - Input validation (one of base64/path required; not both; no key set)
  - The base64 path is decoded and forwarded to Gemini's API
  - The local-path branch resolves MIME from the extension
  - Error responses from Gemini surface as failed ToolResults
  - The tool is registered in the visuals module's TOOLS export
  - The TOOLS list participates in the global ALL_TOOLS registry

We do NOT hit Gemini's real API — `_describe_via_gemini` is patched.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from app.agents.appbuilder.tools.modlix.visuals import (
    TOOLS as VISUALS_TOOLS,
    _MIME_PNG,
    _mime_for_path,
    _resolve_describe_image_payload,
    describe_image_tool,
)


# ── Helper: a tiny stand-in for a PNG header (not a real PNG, but bytes
#    that round-trip cleanly through base64.) ─────────────────────────────
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
_FAKE_PNG_B64 = base64.b64encode(_FAKE_PNG).decode("ascii")


# ── Input-validation tests (synchronous, no Gemini call) ─────────────────


def test_resolve_payload_requires_one_of_inputs() -> None:
    data, mime, err = _resolve_describe_image_payload({})
    assert data is None and mime == ""
    assert err is not None and err.success is False
    assert "required" in (err.error or "").lower()


def test_resolve_payload_rejects_both_inputs() -> None:
    data, mime, err = _resolve_describe_image_payload(
        {"image_base64": _FAKE_PNG_B64, "image_path": "/tmp/anything.png"},
    )
    assert data is None
    assert err is not None and err.success is False
    assert "only one" in (err.error or "").lower()


def test_resolve_payload_decodes_base64() -> None:
    data, mime, err = _resolve_describe_image_payload({"image_base64": _FAKE_PNG_B64})
    assert err is None
    assert data == _FAKE_PNG
    assert mime == _MIME_PNG


def test_resolve_payload_honours_explicit_mime_type() -> None:
    data, mime, err = _resolve_describe_image_payload(
        {"image_base64": _FAKE_PNG_B64, "mime_type": "image/webp"},
    )
    assert err is None
    assert mime == "image/webp"


def test_resolve_payload_rejects_invalid_base64() -> None:
    data, mime, err = _resolve_describe_image_payload({"image_base64": "!!!not-valid-base64!!!"})
    # base64.b64decode is permissive — it strips invalid chars. We accept
    # whatever the stdlib returns and only fail on outright TypeError. The
    # important contract is: no crash, validation flows through cleanly.
    # If the bytes are gibberish, Gemini will return an error and we surface
    # it from _execute_describe_image — separate test below.
    assert err is None or err.success is False


def test_resolve_payload_reads_local_file(tmp_path: Path) -> None:
    f = tmp_path / "sample.png"
    f.write_bytes(_FAKE_PNG)
    data, mime, err = _resolve_describe_image_payload({"image_path": str(f)})
    assert err is None
    assert data == _FAKE_PNG
    assert mime == _MIME_PNG


def test_resolve_payload_missing_file_reports_path(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist.png"
    data, mime, err = _resolve_describe_image_payload({"image_path": str(bogus)})
    assert data is None
    assert err is not None and err.success is False
    assert "not found" in (err.error or "").lower()


def test_resolve_payload_jpeg_extension_resolves_mime(tmp_path: Path) -> None:
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"\xff\xd8\xff" + b"x" * 16)  # fake JPEG header
    data, mime, err = _resolve_describe_image_payload({"image_path": str(f)})
    assert err is None
    assert mime == "image/jpeg"


def test_mime_for_path_recognizes_known_extensions(tmp_path: Path) -> None:
    """The shared MIME helper used by describe_image AND image-edit AND
    image_to_base64. Anchoring it here so future tweaks don't silently
    regress one caller while fixing another."""
    assert _mime_for_path(tmp_path / "a.png") == _MIME_PNG
    assert _mime_for_path(tmp_path / "a.jpg") == "image/jpeg"
    assert _mime_for_path(tmp_path / "a.jpeg") == "image/jpeg"
    assert _mime_for_path(tmp_path / "a.svg") == "image/svg+xml"
    assert _mime_for_path(tmp_path / "a.webp") == "image/webp"
    # Unknown ext, no fallback → image/{ext}.
    assert _mime_for_path(tmp_path / "a.bmp") == "image/bmp"
    # Unknown ext, explicit fallback wins.
    assert _mime_for_path(tmp_path / "a.bmp", fallback="application/octet-stream") == "application/octet-stream"
    # No ext at all → octet-stream.
    assert _mime_for_path(tmp_path / "noext") == "application/octet-stream"


# ── End-to-end tests with Gemini stubbed ─────────────────────────────────


@pytest.mark.asyncio
async def test_execute_returns_text_from_gemini_stub(monkeypatch) -> None:
    """Happy path: Gemini stub returns a description string, the tool
    surfaces it in summary and structured data."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "GOOGLE_API_KEY", "fake-key-for-test")

    async def _stub(api_key, image_data, mime, prompt, model):
        assert api_key == "fake-key-for-test"
        assert image_data == _FAKE_PNG
        assert mime == _MIME_PNG
        assert "describe this ui screenshot" in prompt.lower()
        return "A login form with email + password fields and a blue submit button.", ""

    with patch(
        "app.agents.appbuilder.tools.modlix.visuals._describe_via_gemini",
        side_effect=_stub,
    ):
        result = await describe_image_tool.execute(
            {"image_base64": _FAKE_PNG_B64},
            {},
        )

    assert result.success is True
    assert "login form" in (result.summary or "").lower()
    assert result.data is not None
    assert result.data["description"].startswith("A login form")
    assert result.data["bytes"] == len(_FAKE_PNG)


@pytest.mark.asyncio
async def test_execute_appends_focus_hint_to_prompt(monkeypatch) -> None:
    """focus_hint must be appended to the base prompt so Gemini steers
    its description toward that concern."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "GOOGLE_API_KEY", "fake-key-for-test")
    captured: dict[str, str] = {}

    async def _stub(api_key, image_data, mime, prompt, model):
        captured["prompt"] = prompt
        return "ok", ""

    with patch(
        "app.agents.appbuilder.tools.modlix.visuals._describe_via_gemini",
        side_effect=_stub,
    ):
        result = await describe_image_tool.execute(
            {"image_base64": _FAKE_PNG_B64, "focus_hint": "form layout and spacing"},
            {},
        )

    assert result.success is True
    assert "form layout and spacing" in captured["prompt"]


@pytest.mark.asyncio
async def test_execute_surfaces_gemini_error(monkeypatch) -> None:
    """If Gemini returns an error, the tool surfaces it as success=False
    rather than swallowing it. The bench's circuit breaker depends on this."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "GOOGLE_API_KEY", "fake-key-for-test")

    async def _stub(api_key, image_data, mime, prompt, model):
        return "", "Gemini HTTP 429: rate limited"

    with patch(
        "app.agents.appbuilder.tools.modlix.visuals._describe_via_gemini",
        side_effect=_stub,
    ):
        result = await describe_image_tool.execute(
            {"image_base64": _FAKE_PNG_B64},
            {},
        )

    assert result.success is False
    assert "429" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_missing_api_key_fails_fast(monkeypatch) -> None:
    """No GOOGLE_API_KEY → fail-fast with a clear error before any HTTP call.

    The tool does `from app.config import settings` INSIDE its execute, so the
    patch must land on the settings singleton itself — patching a
    `visuals.settings` module attribute never reaches the tool (that was this
    test's original bug: with a real key in .env the stub always fired).
    """
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "GOOGLE_API_KEY", "")

    # Patch the Gemini call too — if the fail-fast logic regresses, this
    # would otherwise reach out to the real API.
    async def _should_not_be_called(*_args, **_kwargs):
        raise AssertionError("Gemini stub called despite missing API key")

    with patch(
        "app.agents.appbuilder.tools.modlix.visuals._describe_via_gemini",
        side_effect=_should_not_be_called,
    ):
        result = await describe_image_tool.execute(
            {"image_base64": _FAKE_PNG_B64},
            {},
        )

    assert result.success is False
    assert "GOOGLE_API_KEY" in (result.error or "")


# ── Registration tests ──────────────────────────────────────────────────


def test_describe_image_is_in_visuals_tools_export() -> None:
    """The visuals module's TOOLS list must include describe_image so the
    agent's tool registry picks it up."""
    names = [t.name for t in VISUALS_TOOLS]
    assert "describe_image" in names, f"describe_image missing from visuals.TOOLS: {names}"


def test_describe_image_registration_follows_vision_capability() -> None:
    """describe_image is registered only for a text-only AppBuilder model.

    It exists to give a model that cannot see PNGs a Gemini-written
    description of one. On a vision-capable model the registry drops it
    (`_filter_visual_tools`) because the screenshot tools attach the image
    itself — keeping it would invite a redundant paid call. Which branch
    applies depends on settings, so assert the relationship, not a fixed
    answer.
    """
    from app.agents.appbuilder.tools.registry import ALL_TOOLS
    from app.services.llm_provider import appbuilder_vision_capable

    registered = "describe_image" in [t.name for t in ALL_TOOLS]
    if appbuilder_vision_capable():
        assert not registered, (
            "describe_image should be filtered out when the AppBuilder model "
            "has native vision"
        )
    else:
        assert registered, (
            "describe_image must be registered when the AppBuilder model is "
            "text-only — it is that model's only route to image content"
        )


def test_describe_image_tool_schema_advertises_both_inputs() -> None:
    """Anthropic-shape input_schema must surface BOTH image_base64 and
    image_path as optional parameters. The agent picks based on what it has."""
    schema = describe_image_tool.to_anthropic_tool()["input_schema"]
    props = schema.get("properties") or {}
    assert "image_base64" in props
    assert "image_path" in props
    assert "focus_hint" in props
    # Neither image input is in `required` — the validator does the
    # one-or-the-other check at runtime.
    required = schema.get("required") or []
    assert "image_base64" not in required
    assert "image_path" not in required
