"""Images must never exceed what the provider accepts.

Measured against the live API on 2026-09-16: the cap is 8192px per edge, and an
image over it is rejected with a message about unsupported *formats*, which is
misleading. The oversized image stays in the conversation history, so every
later request in that session fails too -- a single tall screenshot kills the
chat for good.
"""

import base64
import io

import pytest
from PIL import Image

from app.core.tools._image_guard import (
    MAX_IMAGE_EDGE,
    MAX_IMAGE_PIXELS,
    sanitize_image_b64,
)
from app.core.tools.base import ToolResult


def png_b64(width: int, height: int, mode: str = "RGB") -> str:
    buf = io.BytesIO()
    Image.new(mode, (width, height), (30, 40, 60) if mode == "RGB" else 128).save(
        buf, format="PNG"
    )
    return base64.b64encode(buf.getvalue()).decode()


def size_of(b64: str) -> tuple[int, int]:
    return Image.open(io.BytesIO(base64.b64decode(b64))).size


class TestSanitize:
    def test_small_image_passes_through_byte_identical(self):
        original = png_b64(800, 600)
        out, mime = sanitize_image_b64(original, "image/png")
        assert out == original and mime == "image/png"

    @pytest.mark.parametrize(
        "width,height",
        [(1440, 12000), (1440, 8193), (9000, 1440), (8000, 9000)],
    )
    def test_oversized_is_downscaled_within_the_cap(self, width, height):
        out, _ = sanitize_image_b64(png_b64(width, height), "image/png")
        assert max(size_of(out)) <= MAX_IMAGE_EDGE

    def test_absurd_image_is_dropped_not_decoded(self):
        """Pillow refuses to decode past ~179 megapixels, and so do we: decoding
        a bomb in order to shrink it is the attack this guards against. Nothing
        our own renderer produces comes close."""
        assert sanitize_image_b64(png_b64(20000, 20000), "image/png") is None

    def test_cap_is_below_the_measured_provider_limit(self):
        assert MAX_IMAGE_EDGE <= 8192

    @pytest.mark.parametrize(
        "width,height",
        [(1440, 4308), (1440, 12000), (1440, 8193), (9000, 1440), (4000, 4000)],
    )
    def test_aspect_ratio_is_preserved(self, width, height):
        out, _ = sanitize_image_b64(png_b64(width, height), "image/png")
        w, h = size_of(out)
        drift = abs((w / h) - (width / height)) / (width / height)
        assert drift < 0.001, f"{width}x{height} -> {w}x{h} drifted {drift:.4%}"

    @pytest.mark.parametrize("width,height", [(9000, 10), (10, 9000)])
    def test_extreme_ratios_are_not_distorted(self, width, height):
        """Regression: a per-axis minimum used to be applied with max(), which
        clamped the short edge on its own and changed the shape. 9000x10 came
        out as 8000x16 -- a 44% distortion. The floor now applies to the scale,
        so only integer rounding moves the ratio."""
        out, _ = sanitize_image_b64(png_b64(width, height), "image/png")
        w, h = size_of(out)
        drift = abs((w / h) - (width / height)) / (width / height)
        assert drift < 0.02, f"{width}x{height} -> {w}x{h} drifted {drift:.4%}"

    def test_pixel_budget_shrinks_tall_pages(self):
        """The edge cap alone leaves a real full-page shot at 6.2 megapixels."""
        out, _ = sanitize_image_b64(png_b64(1440, 4308), "image/png")
        w, h = size_of(out)
        assert w * h <= MAX_IMAGE_PIXELS * 1.001
        assert max(w, h) < MAX_IMAGE_EDGE  # the budget bound, not the edge cap

    def test_ordinary_viewport_shot_is_untouched(self):
        original = png_b64(1440, 900)
        out, _ = sanitize_image_b64(original, "image/png")
        assert out == original

    def test_exactly_at_the_cap_is_untouched(self):
        original = png_b64(100, MAX_IMAGE_EDGE)
        out, _ = sanitize_image_b64(original, "image/png")
        assert out == original

    def test_palette_image_survives_the_resize(self):
        """A P-mode image cannot always be saved as PNG after resize unless it
        is converted first."""
        out, mime = sanitize_image_b64(png_b64(9000, 200, mode="P"), "image/png")
        assert max(size_of(out)) <= MAX_IMAGE_EDGE and mime == "image/png"

    @pytest.mark.parametrize("value", ["", None, 123, "not-base64!!!"])
    def test_junk_input_is_dropped(self, value):
        assert sanitize_image_b64(value, "image/png") is None

    def test_valid_base64_that_is_not_an_image_is_dropped(self):
        assert sanitize_image_b64(base64.b64encode(b"nope").decode(), "image/png") is None

    def test_empty_payload_is_dropped(self):
        assert sanitize_image_b64(base64.b64encode(b"").decode(), "image/png") is None


class TestToolResultIntegration:
    def test_tall_screenshot_is_shrunk_before_reaching_the_model(self):
        res = ToolResult(
            success=True,
            data={"image_base64": png_b64(1440, 12000), "image_mime": "image/png"},
        )
        blocks = res.extract_anthropic_image_blocks()
        assert len(blocks) == 1
        assert max(size_of(blocks[0]["source"]["data"])) <= MAX_IMAGE_EDGE

    def test_unusable_image_yields_no_block_rather_than_a_bad_one(self):
        res = ToolResult(
            success=True,
            data={"image_base64": "bm90LWFuLWltYWdl", "image_mime": "image/png"},
        )
        assert res.extract_anthropic_image_blocks() == []

    def test_multi_shot_results_are_each_guarded(self):
        res = ToolResult(
            success=True,
            data={"shots": [
                {"image_base64": png_b64(9000, 300), "image_mime": "image/png"},
                {"image_base64": png_b64(400, 300), "image_mime": "image/png"},
            ]},
        )
        blocks = res.extract_anthropic_image_blocks()
        assert len(blocks) == 2
        assert all(max(size_of(b["source"]["data"])) <= MAX_IMAGE_EDGE for b in blocks)

    def test_block_cap_still_applies(self):
        res = ToolResult(
            success=True,
            data={"shots": [
                {"image_base64": png_b64(80, 60), "image_mime": "image/png"}
            ] * 12},
        )
        assert len(res.extract_anthropic_image_blocks()) == ToolResult.MAX_IMAGE_BLOCKS
