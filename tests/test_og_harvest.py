"""Harvesting an app's own brand material.

Pure functions only: no network, no browser, no database. The parts that do
reach out are exercised through their own seams in `test_og_advisor.py` and by
driving the endpoint.

Two behaviours here were bugs found by running it against the real sitezump
app, and both would be invisible in any test that used tidy fixtures:

* its favicon is registered as `image/png` and is in fact an SVG, so the
  declared type cannot be trusted;
* its home page screenshots as 1440x900 of pure white with no error reported,
  which without `is_blank` pastes a blank rectangle into the card.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app.services import og_harvest as H


# ── the mark's href ──────────────────────────────────────────────────────────

def test_first_icon_href_reads_the_keyed_map_shape():
    # `properties.links` is a map, not a list. This is the real shape.
    props = {"links": {"16c3ae46cd81": {"rel": "icon", "type": "image/png",
                                        "href": "api/files/static/file/SYSTEM/s/home/l.svg"}}}
    assert H._first_icon_href(props) == "api/files/static/file/SYSTEM/s/home/l.svg"


def test_first_icon_href_also_reads_a_list():
    props = {"links": [{"rel": "shortcut icon", "href": "/a.png"}]}
    assert H._first_icon_href(props) == "/a.png"


def test_first_icon_href_prefers_the_apple_touch_icon():
    # A decent square raster beats a 32px favicon when both are registered.
    props = {"links": {"a": {"rel": "icon", "href": "/small.ico"},
                       "b": {"rel": "apple-touch-icon", "href": "/big.png"}}}
    assert H._first_icon_href(props) == "/big.png"


@pytest.mark.parametrize("props", [
    {},
    {"links": None},
    {"links": {}},
    {"links": "nonsense"},
    {"links": {"a": {"rel": "stylesheet", "href": "/x.css"}}},
    {"links": {"a": {"rel": "icon"}}},
    {"links": {"a": "not a dict"}},
])
def test_first_icon_href_is_empty_when_there_is_none(props):
    assert H._first_icon_href(props) == ""


@pytest.mark.parametrize("href,expected", [
    ("api/files/x.svg", "https://gw.example/api/files/x.svg"),
    ("/api/files/x.svg", "https://gw.example/api/files/x.svg"),
    ("https://cdn.example/x.svg", "https://cdn.example/x.svg"),
    ("data:image/png;base64,AAAA", "data:image/png;base64,AAAA"),
    ("", ""),
])
def test_absolutise(href, expected):
    assert H.absolutise(href, "https://gw.example/") == expected


# ── sniffing an SVG ──────────────────────────────────────────────────────────

def test_looks_like_svg_trusts_the_bytes_over_the_declared_type():
    """The real trap: sitezump registers its SVG favicon as `image/png`."""
    payload = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>'
    assert H._looks_like_svg(payload, "image/png") is True


def test_looks_like_svg_accepts_a_bare_svg_root():
    assert H._looks_like_svg(b"  <svg viewBox='0 0 1 1'/>") is True


def test_looks_like_svg_says_no_to_a_png():
    assert H._looks_like_svg(b"\x89PNG\r\n\x1a\n", "image/png") is False


def test_looks_like_svg_believes_the_content_type_when_it_says_svg():
    assert H._looks_like_svg(b"anything", "image/svg+xml") is True


# ── the palette ──────────────────────────────────────────────────────────────

def test_palette_splits_brand_colours_from_greys():
    accents, neutrals = H.palette_from_theme({"ALL": {
        "colorOne": "#2C2E32",      # near-grey
        "colorTwo": "#F9FAFB",      # near-white
        "color0": "#3B82F6",        # blue
        "color2": "#FBBF24",        # amber
    }})
    assert accents == ["#3b82f6", "#fbbf24"]
    assert neutrals == ["#2c2e32", "#f9fafb"]


def test_palette_reads_the_all_breakpoint_not_the_overrides():
    """Theme variables are per breakpoint; a card has no screen size."""
    accents, _ = H.palette_from_theme({
        "ALL": {"color0": "#3b82f6"},
        "MOBILE_POTRAIT_SCREEN_ONLY": {"color0": "#ff0000"},
    })
    assert accents == ["#3b82f6"]


def test_palette_accepts_a_flat_map_without_breakpoints():
    accents, _ = H.palette_from_theme({"color0": "#3b82f6"})
    assert accents == ["#3b82f6"]


def test_palette_ignores_anything_that_is_not_a_hex_colour():
    accents, neutrals = H.palette_from_theme({"ALL": {
        "colorOne": "var(--something)",
        "color0": "rgb(1,2,3)",
        "color1": 12345,
        "color2": "#3b82f6",
    }})
    assert accents == ["#3b82f6"]
    assert neutrals == []


def test_palette_deduplicates_repeated_tokens():
    accents, _ = H.palette_from_theme({"ALL": {"color0": "#3B82F6",
                                               "color1": "#3b82f6"}})
    assert accents == ["#3b82f6"]


def test_palette_order_follows_the_token_table_not_the_dict():
    """Two runs against one theme must choose the same colours."""
    tokens = {"color2": "#fbbf24", "colorOne": "#3b82f6"}
    first, _ = H.palette_from_theme({"ALL": dict(tokens)})
    second, _ = H.palette_from_theme({"ALL": dict(reversed(list(tokens.items())))})
    assert first == second == ["#3b82f6", "#fbbf24"]


@pytest.mark.parametrize("junk", [None, "", [], 42])
def test_palette_of_junk_is_empty(junk):
    assert H.palette_from_theme(junk) == ([], [])


@pytest.mark.parametrize("value,is_grey", [
    ("#ffffff", True), ("#000000", True), ("#2c2e32", True), ("#808284", True),
    ("#3b82f6", False), ("#fbbf24", False), ("#ef4444", False),
])
def test_chroma_separates_greys_from_colours(value, is_grey):
    assert (H._chroma(value) < H._CHROMA_FLOOR) is is_grey


# ── blankness ────────────────────────────────────────────────────────────────

def test_is_blank_catches_the_all_white_screenshot():
    """Measured: a real sitezump home page came back 1440x900, no error, one colour."""
    assert H.is_blank(Image.new("RGBA", (1440, 900), (255, 255, 255, 255))) is True


def test_is_blank_catches_a_solid_plate_of_any_colour():
    assert H.is_blank(Image.new("RGBA", (400, 300), (18, 20, 28, 255))) is True


def test_is_blank_says_no_to_a_page_with_content():
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for i in range(40):
        d.rectangle([(i * 9, 10), (i * 9 + 6, 280)], fill=(i * 6, 90, 255 - i * 5))
    assert H.is_blank(img.convert("RGBA")) is False


def test_is_blank_tolerates_a_nearly_empty_but_real_page():
    # One heading on white. Antialiasing alone carries it past the floor.
    img = Image.new("RGB", (800, 600), (255, 255, 255))
    ImageDraw.Draw(img).ellipse([(100, 100), (700, 500)], fill=(240, 240, 245),
                                outline=(20, 20, 20), width=3)
    assert H.is_blank(img.convert("RGBA")) is False


# ── trimming ─────────────────────────────────────────────────────────────────

def test_trim_transparent_crops_a_padded_mark_to_its_own_bounds():
    """A logo file is usually padded, and the padding is what a layout would place."""
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([(80, 90), (119, 109)], fill=(255, 0, 0, 255))
    assert H.trim_transparent(img).size == (40, 20)


def test_trim_transparent_keeps_padding_when_asked():
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([(80, 90), (119, 109)], fill=(255, 0, 0, 255))
    assert H.trim_transparent(img, padding=5).size == (50, 30)


def test_trim_transparent_of_a_fully_transparent_image_is_unchanged():
    img = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    assert H.trim_transparent(img).size == (20, 20)


def test_trim_transparent_of_a_fully_opaque_image_is_unchanged():
    img = Image.new("RGBA", (20, 30), (1, 2, 3, 255))
    assert H.trim_transparent(img).size == (20, 30)


# ── the facts object ─────────────────────────────────────────────────────────

def test_site_facts_accent_falls_back_when_nothing_was_harvested():
    from app.services.og_paint import parse_color
    assert parse_color(H.SiteFacts().accent)[3] == 255


def test_site_facts_accent_is_the_first_harvested_colour():
    assert H.SiteFacts(accents=["#3b82f6", "#fbbf24"]).accent == "#3b82f6"


def test_site_facts_note_deduplicates_and_ignores_empty():
    facts = H.SiteFacts()
    facts.note("same")
    facts.note("same")
    facts.note("")
    assert facts.warnings == ["same"]
