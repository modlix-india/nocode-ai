"""The card assembler's deterministic half: no agent, no model, no network.

The agent chooses; these tools execute. What is tested here is the executing.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.services import og_card as C


def _png_bytes(w, h, colour=(20, 20, 60), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), colour if mode == "RGB" else colour + (255,)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def run(tmp_path):
    async def render(brief):
        return _png_bytes(1920, 1080), "minimax", "image-01", "", ""
    return {"work_dir": str(tmp_path), "render": render, "logos": [], "warnings": []}


class TestUrlsInPrompt:
    """A URL typed into the prompt is an instruction, not decoration."""

    def test_finds_the_logo_and_ignores_the_site(self):
        got = C.urls_in_prompt(
            "Can you pelase generate a og image for https://sitezump.ai? Use the logo "
            "here https://cdn.modlix.com/SYSTEM/updatedSiteZump/mainLogo.svg and add context")
        assert got == ["https://cdn.modlix.com/SYSTEM/updatedSiteZump/mainLogo.svg"]

    def test_strips_trailing_punctuation(self):
        assert C.urls_in_prompt("use https://x.test/a.png.") == ["https://x.test/a.png"]

    def test_keeps_a_query_string(self):
        got = C.urls_in_prompt("use https://x.test/a.png?w=400 please")
        assert got == ["https://x.test/a.png?w=400"]

    def test_deduplicates(self):
        assert len(C.urls_in_prompt("https://x.test/a.png and https://x.test/a.png")) == 1

    def test_nothing_in_a_plain_prompt(self):
        assert C.urls_in_prompt("a calm abstract plate") == []


class TestAnchor:
    def test_corners_respect_the_margin(self):
        assert C._anchor("top-left", (100, 50), (1200, 630), 40) == (40, 40)
        assert C._anchor("bottom-right", (100, 50), (1200, 630), 40) == (1060, 540)

    def test_centre_is_centred(self):
        assert C._anchor("center", (200, 100), (1200, 630), 40) == (500, 265)


class TestTools:
    @pytest.mark.asyncio
    async def test_make_plate_produces_a_card_sized_file(self, run):
        res = await C._execute_make_plate({"brief": "a calm gradient"}, {C.CTX: run})
        assert res.success, res.error
        assert (res.data["width"], res.data["height"]) == (1200, 630)
        assert Image.open(run["card"]).size == (1200, 630)

    @pytest.mark.asyncio
    async def test_a_plate_is_required_before_anything_else(self, run):
        ctx = {C.CTX: run}
        assert not (await C._execute_place_logo({}, ctx)).success
        assert not (await C._execute_draw_headline({"text": "hi"}, ctx)).success
        assert not (await C._execute_inspect_card({}, ctx)).success

    @pytest.mark.asyncio
    async def test_place_logo_composites_at_the_asked_size(self, run):
        ctx = {C.CTX: run}
        await C._execute_make_plate({"brief": "x"}, ctx)
        run["logos"] = [_png_bytes(400, 200, (255, 0, 0), mode="RGBA")]
        res = await C._execute_place_logo(
            {"position": "bottom-left", "width_pct": 25, "margin_px": 40}, ctx)
        assert res.success, res.error
        # 25% of 1200
        assert res.data["size"][0] == 300
        assert res.data["placed_at"][0] == 40

    @pytest.mark.asyncio
    async def test_place_logo_says_so_when_none_was_supplied(self, run):
        ctx = {C.CTX: run}
        await C._execute_make_plate({"brief": "x"}, ctx)
        res = await C._execute_place_logo({}, ctx)
        assert not res.success
        assert "no usable logo" in res.error

    @pytest.mark.asyncio
    async def test_draw_headline_wraps_by_measuring(self, run):
        ctx = {C.CTX: run}
        await C._execute_make_plate({"brief": "x"}, ctx)
        res = await C._execute_draw_headline(
            {"text": "Build high converting landing pages with AI in minutes",
             "size_px": 64, "width_pct": 50}, ctx)
        assert res.success, res.error
        # A long headline in half the card cannot be one line at 64px.
        assert len(res.data["lines"]) > 1

    @pytest.mark.asyncio
    async def test_draw_headline_changes_the_pixels(self, run):
        ctx = {C.CTX: run}
        await C._execute_make_plate({"brief": "x"}, ctx)
        before = Path(run["card"]).read_bytes()
        await C._execute_draw_headline({"text": "SiteZump", "colour": "#FFFFFF"}, ctx)
        assert Path(run["card"]).read_bytes() != before

    @pytest.mark.asyncio
    async def test_positions_are_validated(self, run):
        ctx = {C.CTX: run}
        await C._execute_make_plate({"brief": "x"}, ctx)
        res = await C._execute_draw_headline({"text": "x", "position": "middle"}, ctx)
        assert not res.success and "position must be one of" in res.error

    @pytest.mark.asyncio
    async def test_inspect_returns_the_image_so_the_agent_can_see_it(self, run):
        ctx = {C.CTX: run}
        await C._execute_make_plate({"brief": "x"}, ctx)
        res = await C._execute_inspect_card({}, ctx)
        assert res.success
        # `image_base64` is the key the agent loop turns into a real image block.
        assert res.data["image_base64"]

    @pytest.mark.asyncio
    async def test_finish_marks_done(self, run):
        ctx = {C.CTX: run}
        await C._execute_make_plate({"brief": "x"}, ctx)
        assert (await C._execute_finish_card({}, ctx)).success
        assert run["done"] is True


class TestFont:
    def test_returns_something_drawable(self):
        font, warning = C.resolve_font(48)
        assert font is not None
        # On a host with no scalable face this must SAY so rather than quietly
        # drawing a headline nobody can read.
        assert warning == "" or "no scalable font" in warning
