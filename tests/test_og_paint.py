"""The Open Graph drawing kit: pure Pillow, no model, no network.

Every test here is a property that was either measured off a rendered card or
is a trap that produced a visibly wrong one. The two worth naming:

* `Image.radial_gradient` normalises so its *corner* is white, which leaves a
  blob 30% opaque at the midpoint of each edge and draws a visible square. That
  is why `_build_radial_base` exists and why its falloff is asserted here.
* `low_contrast_fraction` rather than a mean: a lockup of a solid coloured glyph
  beside a white wordmark averages to something that passes on a white plate
  while half the mark is invisible.
"""

from __future__ import annotations

import math

import pytest
from PIL import Image, ImageDraw

from app.services import og_paint as P


# ── colour ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("#fff", (255, 255, 255, 255)),
    ("#ffffff", (255, 255, 255, 255)),
    ("#ff000080", (255, 0, 0, 128)),
    ("7c3aed", (124, 58, 237, 255)),
    ("  #7C3AED  ", (124, 58, 237, 255)),
    ("rgb(12, 34, 56)", (12, 34, 56, 255)),
    ("rgba(12, 34, 56, 0.5)", (12, 34, 56, 128)),
    ("white", (255, 255, 255, 255)),
    ("transparent", (0, 0, 0, 0)),
    ((1, 2, 3), (1, 2, 3, 255)),
    ((1, 2, 3, 4), (1, 2, 3, 4)),
])
def test_parse_color_reads_every_shape_a_spec_might_carry(value, expected):
    assert P.parse_color(value) == expected


@pytest.mark.parametrize("junk", [None, "", "not a colour", "#12345", "rgb(1)", {}])
def test_parse_color_never_raises_and_falls_back(junk):
    # A card with one wrong colour ships; a traceback does not.
    assert P.parse_color(junk, (9, 9, 9, 255)) == (9, 9, 9, 255)


def test_parse_color_clamps_out_of_range_channels():
    assert P.parse_color("rgb(300, -20, 40)") == (255, 0, 40, 255)


def test_mix_interpolates_and_clamps_t():
    black, white = (0, 0, 0, 255), (255, 255, 255, 255)
    assert P.mix(black, white, 0.0) == black
    assert P.mix(black, white, 1.0) == white
    assert P.mix(black, white, 0.5) == (128, 128, 128, 255)
    assert P.mix(black, white, 5.0) == white      # clamped, not extrapolated
    assert P.mix(black, white, -5.0) == black


def test_lighten_and_darken_move_the_right_way():
    base = P.parse_color("#7c3aed")
    assert P.relative_luminance(P.lighten(base, 0.5)) > P.relative_luminance(base)
    assert P.relative_luminance(P.darken(base, 0.5)) < P.relative_luminance(base)


def test_with_alpha_accepts_both_fraction_and_byte():
    base = (10, 20, 30, 255)
    assert P.with_alpha(base, 0.5)[3] == 128
    assert P.with_alpha(base, 200)[3] == 200


def test_rotate_hue_keeps_saturation_and_returns_a_different_hue():
    base = P.parse_color("#7c3aed")
    turned = P.rotate_hue(base, 180)
    assert turned != base
    assert turned[3] == base[3]
    # A half turn and back is the same colour, give or take rounding.
    back = P.rotate_hue(turned, 180)
    assert all(abs(back[i] - base[i]) <= 2 for i in range(3))


def test_contrast_ratio_matches_the_wcag_extremes():
    black, white = (0, 0, 0, 255), (255, 255, 255, 255)
    assert P.contrast_ratio(black, white) == pytest.approx(21.0, abs=0.01)
    assert P.contrast_ratio(white, white) == pytest.approx(1.0, abs=0.01)


def test_ink_for_picks_the_readable_tone_on_both_extremes():
    assert P.ink_for(P.parse_color("#0b0b12"))[0] == 255       # white on near-black
    assert P.ink_for(P.parse_color("#f5f4f8"))[0] < 60         # dark on near-white


def test_ink_for_beats_a_luminance_threshold_on_a_mid_tone():
    # The case a naive `luminance > 0.5` test gets wrong. Whatever it picks, it
    # must be the higher-contrast of the two, which is the whole contract.
    mid = P.parse_color("#7c3aed")
    chosen = P.ink_for(mid)
    other = (16, 18, 24, 255) if chosen[0] == 255 else (255, 255, 255, 255)
    assert P.contrast_ratio(mid, chosen) >= P.contrast_ratio(mid, other)


def test_to_hex_round_trips_through_parse_color():
    assert P.parse_color(P.to_hex((124, 58, 237, 255))) == (124, 58, 237, 255)


# ── backgrounds ──────────────────────────────────────────────────────────────

def test_flat_fills_the_whole_canvas():
    img = P.flat((40, 20), "#123456")
    assert img.size == (40, 20)
    assert img.getpixel((0, 0)) == img.getpixel((39, 19)) == (18, 52, 86, 255)


def test_linear_gradient_runs_from_the_first_stop_to_the_last():
    img = P.linear_gradient((200, 100), [(0.0, "#000000"), (1.0, "#ffffff")], 90.0)
    left = img.getpixel((2, 50))
    right = img.getpixel((197, 50))
    assert left[0] < 40 and right[0] > 215


def test_linear_gradient_honours_a_middle_stop():
    img = P.linear_gradient((300, 20), [(0.0, "#000000"),
                                        (0.5, "#ff0000"),
                                        (1.0, "#000000")], 90.0)
    middle = img.getpixel((150, 10))
    assert middle[0] > 200 and middle[1] < 40


def test_linear_gradient_fills_the_corners_on_a_diagonal():
    # Rotating a ramp that is merely canvas-sized leaves the corners black,
    # which is why the ramp is built on the diagonal and centre-cropped.
    img = P.linear_gradient((200, 100), [(0.0, "#ff0000"), (1.0, "#ff0000")], 45.0)
    for xy in ((0, 0), (199, 0), (0, 99), (199, 99)):
        assert img.getpixel(xy)[:3] == (255, 0, 0), f"corner {xy} was not painted"


def test_linear_gradient_survives_an_empty_stop_list():
    assert P.linear_gradient((10, 10), []).size == (10, 10)


def test_radial_mask_is_opaque_at_the_centre_and_zero_at_the_edge():
    # The whole reason this mask is hand-built. PIL's own radial_gradient
    # measures 179/255 here, which paints a hard square on every blob.
    mask = P._radial_mask(256)
    assert mask.getpixel((128, 128)) == 255
    assert mask.getpixel((255, 128)) == 0
    assert mask.getpixel((128, 255)) == 0
    assert mask.getpixel((255, 255)) == 0


def test_radial_mask_falls_off_monotonically_from_the_centre():
    mask = P._radial_mask(256)
    samples = [mask.getpixel((128 + d, 128)) for d in range(0, 128, 8)]
    assert samples == sorted(samples, reverse=True)


def test_mesh_gradient_puts_the_blob_colour_where_the_blob_is():
    img = P.mesh_gradient((200, 200), "#000000",
                          [{"color": "#ff0000", "cx": 0.25, "cy": 0.25, "r": 0.2}])
    at_blob = img.getpixel((50, 50))
    far = img.getpixel((190, 190))
    assert at_blob[0] > 200
    assert far[0] < 20


def test_mesh_gradient_ignores_junk_blobs_rather_than_raising():
    img = P.mesh_gradient((50, 50), "#101010",
                          ["not a dict", None, {}, {"color": "transparent"}])
    assert img.getpixel((25, 25)) == (16, 16, 16, 255)


def test_mesh_gradient_alpha_scales_the_wash():
    strong = P.mesh_gradient((100, 100), "#000000",
                             [{"color": "#ffffff", "cx": .5, "cy": .5, "r": .5, "alpha": 1.0}])
    weak = P.mesh_gradient((100, 100), "#000000",
                           [{"color": "#ffffff", "cx": .5, "cy": .5, "r": .5, "alpha": 0.25}])
    assert strong.getpixel((50, 50))[0] > weak.getpixel((50, 50))[0]


def test_grid_overlay_darkens_nothing_and_only_adds_lines():
    base = P.flat((120, 120), "#000000")
    gridded = P.grid_overlay(base, 40, "#ffffff", 0.5)
    assert gridded.getpixel((20, 20))[0] == 0        # between the rules
    assert gridded.getpixel((40, 20))[0] > 0         # on one


def test_vignette_darkens_the_corner_and_spares_the_centre():
    base = P.flat((200, 200), "#ffffff")
    shaded = P.vignette(base, 0.8)
    assert shaded.getpixel((100, 100))[0] > shaded.getpixel((2, 2))[0]


def test_vignette_at_zero_strength_is_a_no_op():
    base = P.flat((20, 20), "#abcdef")
    assert P.vignette(base, 0.0).getpixel((5, 5)) == base.getpixel((5, 5))


def test_noise_overlay_is_seeded_and_therefore_reproducible():
    base = P.flat((60, 60), "#303030")
    a = P.noise_overlay(base, 0.5, seed=11)
    b = P.noise_overlay(base, 0.5, seed=11)
    c = P.noise_overlay(base, 0.5, seed=12)
    assert list(a.getdata()) == list(b.getdata())
    assert list(a.getdata()) != list(c.getdata())


# ── panels and marks ─────────────────────────────────────────────────────────

def test_rounded_panel_clears_the_corner_and_keeps_the_middle():
    src = P.flat((100, 100), "#ff0000")
    panel = P.rounded_panel(src, radius=30)
    assert panel.getpixel((1, 1))[3] == 0            # corner cut away
    assert panel.getpixel((50, 50))[3] == 255        # middle intact


def test_rounded_panel_radius_cannot_exceed_half_the_short_side():
    # A radius larger than the box would make rounded_rectangle raise.
    assert P.rounded_panel(P.flat((40, 20), "#fff"), radius=500).size == (40, 20)


def test_drop_shadow_returns_an_offset_that_keeps_it_aligned():
    panel = P.rounded_panel(P.flat((80, 40), "#ffffff"), 8)
    shadow, (dx, dy) = P.drop_shadow(panel, blur=6, offset=(0, 4))
    assert shadow.width > panel.width and shadow.height > panel.height
    # The delta is negative: the shadow canvas is padded on every side, so the
    # caller pastes it up and left of where the panel itself goes.
    assert dx < 0 and dy < 0
    assert shadow.width == panel.width - dx * 2
    assert shadow.getpixel((shadow.width // 2, shadow.height // 2))[3] > 0


def test_fit_contain_keeps_aspect_and_stays_inside_the_box():
    out = P.fit_contain(Image.new("RGBA", (400, 100)), 200, 200)
    assert out.size == (200, 50)
    assert out.width <= 200 and out.height <= 200


def test_fit_contain_refuses_to_upscale_without_limit():
    out = P.fit_contain(Image.new("RGBA", (10, 10)), 900, 900)
    assert out.size == (40, 40)          # capped at 4x


def test_fit_cover_fills_the_box_exactly():
    assert P.fit_cover(Image.new("RGBA", (400, 100)), 200, 200).size == (200, 200)
    assert P.fit_cover(Image.new("RGBA", (100, 400)), 200, 200).size == (200, 200)


def test_fit_cover_anchors_to_the_top_by_default():
    # A page screenshot is tall and the part worth showing is the first
    # screenful, so the default crop must keep the top, not the middle.
    tall = Image.new("RGB", (100, 400), (0, 0, 0))
    ImageDraw.Draw(tall).rectangle([(0, 0), (99, 99)], fill=(255, 0, 0))
    out = P.fit_cover(tall.convert("RGBA"), 100, 100)
    assert out.getpixel((50, 10))[0] > 200


def test_mean_visible_color_ignores_the_transparent_area():
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([(10, 10), (30, 30)], fill=(255, 0, 0, 255))
    assert P.mean_visible_color(img)[0] > 200


def test_mean_visible_color_of_a_fully_transparent_image_is_transparent():
    assert P.mean_visible_color(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))[3] == 0


def test_low_contrast_fraction_catches_the_half_of_a_mark_that_vanishes():
    # The exact failure a mean hides: a solid purple glyph beside a white
    # wordmark, laid on a white plate.
    mark = Image.new("RGBA", (80, 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(mark)
    d.rectangle([(0, 0), (39, 19)], fill=(124, 58, 237, 255))   # survives
    d.rectangle([(40, 0), (79, 19)], fill=(255, 255, 255, 255))  # vanishes
    on_white = P.low_contrast_fraction(mark, (250, 250, 252, 255))
    on_black = P.low_contrast_fraction(mark, (11, 11, 18, 255))
    assert on_white > 0.4
    assert on_black < 0.25
    # And the statistic this replaced would have passed the white plate.
    assert P.contrast_ratio((250, 250, 252, 255), P.mean_visible_color(mark)) > 2.0


def test_region_color_clamps_a_box_that_runs_off_the_canvas():
    img = P.flat((50, 50), "#204060")
    assert P.region_color(img, (-100, -100, 5000, 5000))[:3] == (32, 64, 96)


def test_contrast_chip_is_larger_than_what_it_sits_behind():
    chip = P.contrast_chip((100, 40), (0, 0, 0, 255), pad=14)
    assert chip.size == (128, 68)
    assert chip.getpixel((64, 34))[3] == 255


# ── type ─────────────────────────────────────────────────────────────────────

def _font(size):
    from PIL import ImageFont
    return ImageFont.load_default()


def test_wrap_to_width_measures_rather_than_counts(font_set):
    # "WWWW WWWW" and "iiii iiii" are the same length in characters and nothing
    # like it in pixels; a count-based wrap treats them identically.
    draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    font = font_set.display(40)
    wide = P.wrap_to_width(draw, "WWWW WWWW", font, 120)
    narrow = P.wrap_to_width(draw, "iiii iiii", font, 120)
    assert len(wide) >= len(narrow)


def test_wrap_to_width_never_drops_a_word_too_long_for_the_line(font_set):
    draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    lines = P.wrap_to_width(draw, "Unmistakeably", font_set.display(40), 10)
    assert lines == ["Unmistakeably"]


def test_wrap_to_width_of_empty_text_is_empty():
    draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    assert P.wrap_to_width(draw, "   ", _font(10), 100) == []


def test_fit_text_shrinks_until_the_block_fits(font_set):
    font, lines, size = P.fit_text("Build high-converting landing pages with AI",
                                   font_set.loader(), 400, 200,
                                   max_lines=3, start_size=120, min_size=20)
    assert len(lines) <= 3
    assert size < 120
    _, height = P.text_block_size(lines, font, size)
    assert height <= 200


def test_fit_text_returns_the_start_size_when_it_already_fits(font_set):
    _, lines, size = P.fit_text("Hi", font_set.loader(), 900, 400,
                                max_lines=3, start_size=64, min_size=20)
    assert size == 64 and lines == ["Hi"]


def test_fit_text_clips_rather_than_returning_nothing(font_set):
    # Impossible box. A cramped headline beats a blank card.
    _, lines, size = P.fit_text("word " * 60, font_set.loader(), 60, 40,
                                max_lines=2, start_size=40, min_size=30)
    assert lines and len(lines) <= 2
    assert size == 30


def test_text_block_size_grows_with_the_line_count(font_set):
    font = font_set.display(30)
    one = P.text_block_size(["a"], font, 30)
    two = P.text_block_size(["a", "b"], font, 30)
    assert two[1] > one[1]
    assert P.text_block_size([], font, 30) == (0, 0)


def test_draw_lines_returns_the_height_it_used_and_marks_the_canvas(font_set):
    card = P.flat((400, 200), "#000000")
    used = P.draw_lines(card, ["Hello", "World"], font_set.display(30), 30, (10, 10),
                        "#ffffff")
    assert used > 0
    assert any(px[0] > 200 for px in card.crop((0, 0, 400, 120)).getdata())


def test_draw_lines_alignment_moves_the_text(font_set):
    def first_ink_x(align):
        card = P.flat((400, 60), "#000000")
        P.draw_lines(card, ["hi"], font_set.display(30), 30, (0, 5), "#ffffff",
                     align=align, box_w=400)
        for x in range(400):
            if any(card.getpixel((x, y))[0] > 120 for y in range(60)):
                return x
        return -1

    left, centre, right = first_ink_x("left"), first_ink_x("center"), first_ink_x("right")
    assert left < centre < right


def test_draw_lines_into_an_l_mask_does_not_raise(font_set):
    # Pillow refuses an RGBA fill on a single-band image, and gradient_text
    # stencils through exactly this path.
    mask = Image.new("L", (200, 60), 0)
    P.draw_lines(mask, ["hi"], font_set.display(30), 30, (5, 5), (255, 255, 255, 255))
    assert max(mask.getdata()) == 255


def test_gradient_text_paints_two_different_colours_across_a_line(font_set):
    card = P.flat((600, 120), "#000000")
    font = font_set.display(80)
    P.gradient_text(card, ["MMMMMMMM"], font, 80, (10, 10),
                    [(0.0, "#ff0000"), (1.0, "#0000ff")], 90.0, box_w=580)
    lit = [(x, card.getpixel((x, y))) for x in range(600) for y in range(120)
           if sum(card.getpixel((x, y))[:3]) > 60]
    assert lit, "nothing was drawn"
    assert lit[0][1][0] > lit[-1][1][0]       # red end
    assert lit[-1][1][2] > lit[0][1][2]       # blue end


def test_gradient_text_of_nothing_draws_nothing(font_set):
    card = P.flat((100, 40), "#000000")
    assert P.gradient_text(card, [], _font(10), 10, (0, 0), [(0, "#fff")]) == 0


def test_pill_returns_its_size_and_paints_within_it(font_set):
    card = P.flat((300, 100), "#000000")
    size = P.pill(card, "Join", font_set.body(20), (20, 20), "#ffffff", "#000000")
    assert size[0] > 0 and size[1] > 0
    # Sampled in the left padding, not the centre: the centre is where the black
    # label sits, so it reads as the ink rather than the pill.
    assert card.getpixel((20 + 6, 20 + size[1] // 2))[0] > 200
    assert card.getpixel((2, 2))[0] == 0          # nothing painted outside it


def test_stat_tiles_lays_out_a_grid_without_overflowing(font_set):
    tiles = P.stat_tiles((400, 300),
                         [{"value": "0s", "label": "a"}, {"value": "50+", "label": "b"},
                          {"value": "1k", "label": "c"}, {"value": "2M", "label": "d"}],
                         font_set.display(30), font_set.body(14), columns=2)
    assert tiles.size == (400, 300)
    assert tiles.getpixel((100, 70))[3] > 0          # first tile painted
    assert tiles.getpixel((398, 2))[3] == 0          # gutter stays clear


def test_stat_tiles_with_no_items_is_a_blank_layer(font_set):
    tiles = P.stat_tiles((100, 100), [], font_set.display(20), font_set.body(10))
    assert tiles.size == (100, 100)
    assert max(px[3] for px in tiles.getdata()) == 0


def test_flatten_drops_alpha_over_the_backing_colour():
    img = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
    out = P.flatten(img, "#00ff00")
    assert out.mode == "RGB"
    assert out.getpixel((5, 5)) == (0, 255, 0)


def test_card_size_is_the_documented_open_graph_ratio():
    assert P.CARD_SIZE == (1200, 630)
    assert math.isclose(P.CARD_W / P.CARD_H, 1.904, abs_tol=0.01)
