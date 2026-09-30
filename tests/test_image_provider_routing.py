"""generate_image provider routing: gemini (Nano Banana) vs minimax (image-01).

The one thing worth a test here is the multi-image guard. image-01 has a single
`subject_reference` slot meaning "keep this person's likeness", so handing it a
3-image composite request would render off image 1 and return success. That is
a wrong image reported as a right one, which no caller can detect.
"""
from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix import visuals as V

_PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def routed(monkeypatch, tmp_path):
    """Patch both backends + upload; return the list of (provider, n_inputs)."""
    calls: list[tuple[str, int]] = []

    async def fake_gemini(key, prompt, model, input_images=None):
        calls.append(("gemini", len(input_images or [])))
        return _PNG, ""

    async def fake_minimax(key, prompt, model, aspect, input_images=None):
        calls.append(("minimax", len(input_images or [])))
        return _PNG, ""

    async def fake_upload(*a, **k):
        return "/rel/x.png", "http://abs/x.png", ""

    async def fake_record(**k):
        return None

    monkeypatch.setattr(V, "_generate_via_gemini", fake_gemini)
    monkeypatch.setattr(V, "_generate_via_minimax", fake_minimax)
    monkeypatch.setattr(V, "_upload_generated_static", fake_upload)
    monkeypatch.setattr(V, "record_generated_asset", fake_record)
    from app.config import settings
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", "g-key", raising=False)
    monkeypatch.setattr(settings, "MINIMAX_API_KEY", "m-key", raising=False)
    for i in range(3):
        (tmp_path / f"in{i}.png").write_bytes(_PNG)
    return calls, tmp_path


def _ctx():
    return {"app_code": "app", "client_code": "cc", "headers": {},
            "session_id": "s", "turn_number": 0}


def _params(tmp_path, n_inputs=0, **extra):
    p = {"prompt": "a cat", "filename": "x.png", **extra}
    if n_inputs:
        p["input_image_paths"] = [str(tmp_path / f"in{i}.png") for i in range(n_inputs)]
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,n_inputs,expected", [
    ("gemini", 0, "gemini"),
    ("gemini", 3, "gemini"),
    ("minimax", 0, "minimax"),
    ("minimax", 1, "minimax"),   # one reference is the subject_reference slot
    ("minimax", 2, "gemini"),    # more than one: only Gemini can composite
    ("minimax", 3, "gemini"),
])
async def test_routes_by_setting_and_input_count(routed, monkeypatch, setting, n_inputs, expected):
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", setting, raising=False)
    monkeypatch.setattr(settings, "IMAGE_EDIT_FALLBACK_TO_GEMINI", True, raising=False)
    r = await V._execute_generate_image(_params(tmp_path, n_inputs), _ctx())
    assert r.success, r.error
    assert calls[-1] == (expected, n_inputs)


@pytest.mark.asyncio
async def test_param_overrides_setting(routed, monkeypatch):
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", "gemini", raising=False)
    r = await V._execute_generate_image(_params(tmp_path, image_provider="minimax"), _ctx())
    assert r.success and calls[-1][0] == "minimax"


@pytest.mark.asyncio
async def test_multi_image_fails_loudly_when_fallback_disabled(routed, monkeypatch):
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", "minimax", raising=False)
    monkeypatch.setattr(settings, "IMAGE_EDIT_FALLBACK_TO_GEMINI", False, raising=False)
    r = await V._execute_generate_image(_params(tmp_path, 3), _ctx())
    assert not r.success
    assert "single reference image" in r.error
    assert not calls, "must not render anything when it cannot honour the request"


@pytest.mark.asyncio
async def test_unknown_provider_rejected(routed, monkeypatch):
    calls, tmp_path = routed
    r = await V._execute_generate_image(_params(tmp_path, image_provider="dalle"), _ctx())
    assert not r.success and "must be one of" in r.error
    assert not calls


@pytest.mark.asyncio
async def test_aspect_hint_only_appended_for_gemini(routed, monkeypatch):
    """image-01 takes aspect_ratio as a field, so the prompt must not repeat it."""
    calls, tmp_path = routed
    seen: dict[str, str] = {}

    async def spy_minimax(key, prompt, model, aspect, input_images=None):
        seen["minimax"] = prompt
        return _PNG, ""

    async def spy_gemini(key, prompt, model, input_images=None):
        seen["gemini"] = prompt
        return _PNG, ""

    monkeypatch.setattr(V, "_generate_via_minimax", spy_minimax)
    monkeypatch.setattr(V, "_generate_via_gemini", spy_gemini)
    await V._execute_generate_image(_params(tmp_path, image_provider="minimax", aspect_ratio="16:9"), _ctx())
    await V._execute_generate_image(_params(tmp_path, image_provider="gemini", aspect_ratio="16:9"), _ctx())
    assert "Aspect:" not in seen["minimax"]
    assert "Aspect:" in seen["gemini"]


@pytest.mark.asyncio
async def test_minimax_failure_falls_back_to_gemini(routed, monkeypatch):
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", "minimax", raising=False)
    monkeypatch.setattr(settings, "IMAGE_ERROR_FALLBACK_TO_GEMINI", True, raising=False)

    async def dead_minimax(key, prompt, model, aspect, input_images=None):
        calls.append(("minimax", len(input_images or [])))
        return None, "MiniMax HTTP 503: upstream unavailable"

    monkeypatch.setattr(V, "_generate_via_minimax", dead_minimax)
    r = await V._execute_generate_image(_params(tmp_path, aspect_ratio="16:9"), _ctx())
    assert r.success, r.error
    assert [c[0] for c in calls] == ["minimax", "gemini"]
    # The caller must be told, because the fallback backend ignores aspect.
    assert "minimax failed" in r.summary
    assert "16:9 may not be honoured" in r.summary


@pytest.mark.asyncio
async def test_fallback_does_not_carry_the_model_override(routed, monkeypatch):
    """`model="image-01"` names a MiniMax model; sending it to Gemini 400s."""
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", "minimax", raising=False)
    seen: list[str] = []

    async def dead_minimax(key, prompt, model, aspect, input_images=None):
        return None, "boom"

    async def spy_gemini(key, prompt, model, input_images=None):
        seen.append(model)
        return _PNG, ""

    monkeypatch.setattr(V, "_generate_via_minimax", dead_minimax)
    monkeypatch.setattr(V, "_generate_via_gemini", spy_gemini)
    r = await V._execute_generate_image(_params(tmp_path, model="image-01"), _ctx())
    assert r.success, r.error
    assert seen == [V._DEFAULT_IMAGE_MODEL]


@pytest.mark.asyncio
async def test_both_providers_failing_reports_both(routed, monkeypatch):
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", "minimax", raising=False)

    async def dead_minimax(key, prompt, model, aspect, input_images=None):
        return None, "minimax down"

    async def dead_gemini(key, prompt, model, input_images=None):
        return None, "gemini down"

    monkeypatch.setattr(V, "_generate_via_minimax", dead_minimax)
    monkeypatch.setattr(V, "_generate_via_gemini", dead_gemini)
    r = await V._execute_generate_image(_params(tmp_path), _ctx())
    assert not r.success
    assert "minimax down" in r.error
    assert "gemini down" in r.error


@pytest.mark.asyncio
async def test_gemini_failure_does_not_retry_on_minimax(routed, monkeypatch):
    """Fallback is one-directional: Gemini is the safety net, not the reverse."""
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", "gemini", raising=False)

    async def dead_gemini(key, prompt, model, input_images=None):
        calls.append(("gemini", 0))
        return None, "gemini down"

    monkeypatch.setattr(V, "_generate_via_gemini", dead_gemini)
    r = await V._execute_generate_image(_params(tmp_path), _ctx())
    assert not r.success
    assert [c[0] for c in calls] == ["gemini"]


@pytest.mark.asyncio
async def test_error_fallback_can_be_disabled(routed, monkeypatch):
    calls, tmp_path = routed
    from app.config import settings
    monkeypatch.setattr(settings, "IMAGE_PROVIDER", "minimax", raising=False)
    monkeypatch.setattr(settings, "IMAGE_ERROR_FALLBACK_TO_GEMINI", False, raising=False)

    async def dead_minimax(key, prompt, model, aspect, input_images=None):
        calls.append(("minimax", 0))
        return None, "minimax down"

    monkeypatch.setattr(V, "_generate_via_minimax", dead_minimax)
    r = await V._execute_generate_image(_params(tmp_path), _ctx())
    assert not r.success
    assert [c[0] for c in calls] == ["minimax"]


def test_default_provider_is_minimax():
    from app.config import Settings
    assert Settings().IMAGE_PROVIDER == "minimax"


def test_to_png_passes_through_and_transcodes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="JPEG")
    out, err = V._to_png(buf.getvalue())
    assert not err and out[:8] == b"\x89PNG\r\n\x1a\n"

    buf2 = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buf2, format="PNG")
    same, err = V._to_png(buf2.getvalue())
    assert not err and same == buf2.getvalue()
