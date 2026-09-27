"""The Open Graph card layouts.

These are the properties a template has to hold whatever it is handed: the card
comes out the documented size, the type stays inside the safe area, the ink is
readable against the plate the layout actually puts it on, and a layout asked
for without the material it needs steps down instead of drawing an empty
column.

Everything here is deterministic. No model, no network, no clock.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app.services import og_paint as P
from app.services import og_templates as T


# ── material ─────────────────────────────────────────────────────────────────

def _logo(fg=(255, 255, 255, 255)) -> Image.Image:
    """A wordmark-shaped mark: a solid block beside a bar, with alpha around it."""
    img = Image.new("RGBA", (320, 80), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([(0, 6), (68, 74)], radius=14, fill=(124, 58, 237, 255))
    d.rectangle([(84, 24), (316, 56)], fill=fg)
    return img


def _shot() -> Image.Image:
    """A tall page-shaped screenshot with a distinctive top band."""
    img = Image.new("RGBA", (1440, 900), (18, 20, 28, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([(0, 0), (1439, 120)], fill=(255, 64, 0, 255))
    d.rectangle([(0, 120), (280, 899)], fill=(30, 33, 44, 255))
    return img


DARK = T.Background("mesh", "#0b0b12",
                    [{"color": "#7c3aed", "cx": 0.8, "cy": 0.2, "r": 0.6}], noise=0.0)
LIGHT = T.Background("flat", "#f6f5f9", noise=0.0)

STATS = [{"value": "0s", "label": "to first draft"},
         {"value": "50+", "label": "templates"},
         {"value": "120k", "label": "pages"},
         {"value": "10M", "label": "visits"}]


def _spec(**kw) -> T.CardSpec:
    base = dict(headline="Build high-converting landing pages with AI",
                subline="Describe your product and get a site that ships.",
                domain="sitezump.ai", background=DARK)
    base.update(kw)
    return T.CardSpec(**base)


def _ink_pixels(card: Image.Image, plate: P.RGBA, tolerance: int = 48) -> list[tuple[int, int]]:
    """Every pixel that differs from the plate, i.e. everything drawn on it."""
    rgb = card.convert("RGB")
    out = []
    for y in range(0, rgb.height, 2):
        for x in range(0, rgb.width, 2):
            px = rgb.getpixel((x, y))
            if sum(abs(px[i] - plate[i]) for i in range(3)) > tolerance:
                out.append((x, y))
    return out


# ── the contract every layout holds ──────────────────────────────────────────

@pytest.mark.parametrize("info", T.TEMPLATES, ids=lambda i: i.name)
def test_every_template_renders_at_the_card_size(info, font_set):
    card = T.render(_spec(template=info.name, logo=_logo(), shot=_shot(), stats=STATS),
                    font_set)
    assert card.size == P.CARD_SIZE
    assert card.mode == "RGBA"


@pytest.mark.parametrize("info", T.TEMPLATES, ids=lambda i: i.name)
def test_every_template_draws_something(info, font_set):
    card = T.render(_spec(template=info.name, logo=_logo(), shot=_shot(), stats=STATS),
                    font_set)
    flat = P.flatten(card, "#0b0b12")
    assert len(set(flat.getdata())) > 50, "the card is a single flat colour"


@pytest.mark.parametrize("info", T.TEMPLATES, ids=lambda i: i.name)
def test_every_template_is_deterministic(info, font_set):
    spec_args = dict(template=info.name, logo=_logo(), shot=_shot(), stats=STATS)
    first = T.render(_spec(**spec_args), font_set)
    second = T.render(_spec(**spec_args), font_set)
    assert list(first.getdata()) == list(second.getdata())


@pytest.mark.parametrize("info", [t for t in T.TEMPLATES if not t.needs_shot],
                         ids=lambda i: i.name)
def test_text_only_layouts_keep_everything_inside_the_safe_area(info, font_set):
    """Nothing meaningful within 24px of an edge.

    Consumers crop a card slightly differently and WhatsApp rounds the corners,
    so a headline that reaches the edge loses a letter somewhere. Only the
    layouts without a screenshot are held to this: the product panels bleed off
    an edge deliberately.
    """
    plate = P.parse_color("#101014")
    card = T.render(_spec(template=info.name, background=T.Background("flat", "#101014",
                                                                     noise=0.0),
                          logo=_logo(), stats=STATS), font_set)
    edge = 24
    for x, y in _ink_pixels(card, plate):
        assert edge <= x < P.CARD_W - edge, f"{info.name} drew at x={x}"
        assert edge <= y < P.CARD_H - edge, f"{info.name} drew at y={y}"


@pytest.mark.parametrize("info", T.TEMPLATES, ids=lambda i: i.name)
def test_every_template_survives_missing_optional_copy(info, font_set):
    """No subline, no domain, no stats, no logo, no shot. It still renders."""
    card = T.render(T.CardSpec(template=info.name, headline="Ship it",
                               background=DARK), font_set)
    assert card.size == P.CARD_SIZE


@pytest.mark.parametrize("info", T.TEMPLATES, ids=lambda i: i.name)
def test_every_template_survives_an_absurd_headline(info, font_set):
    card = T.render(_spec(template=info.name, headline="Supercalifragilistic " * 12,
                          logo=_logo(), shot=_shot(), stats=STATS), font_set)
    assert card.size == P.CARD_SIZE


# ── choosing a layout ────────────────────────────────────────────────────────

def test_unknown_template_falls_back_rather_than_raising(font_set):
    # What a model hallucinating a plausible name produces.
    card = T.render(_spec(template="cinematic-hero-v2"), font_set)
    assert card.size == P.CARD_SIZE


def test_a_shot_layout_without_a_shot_steps_down(font_set):
    """The empty-column bug: a split layout with nothing to put in the split."""
    spec = _spec(template="headline-left-shot-inset", shot=None,
                 background=T.Background("flat", "#101014", noise=0.0))
    card = T.render(spec, font_set)
    plate = P.parse_color("#101014")
    drawn = _ink_pixels(card, plate)
    # Having stepped down to a full-width layout, type must reach past the
    # column the split would have confined it to.
    assert max(x for x, _ in drawn) > P.CARD_W * 0.45


def test_a_stats_layout_without_stats_steps_down(font_set):
    card = T.render(_spec(template="headline-stats", stats=[]), font_set)
    assert card.size == P.CARD_SIZE


def test_a_logo_layout_without_a_logo_steps_down(font_set):
    card = T.render(_spec(template="logo-center", logo=None), font_set)
    assert card.size == P.CARD_SIZE


def test_usable_templates_filters_on_what_is_actually_available():
    bare = T.usable_templates(has_logo=False, has_shot=False, has_stats=False)
    assert bare, "a card with nothing harvested still needs options"
    assert all(not t.needs_logo and not t.needs_shot and not t.needs_stats for t in bare)

    full = T.usable_templates(has_logo=True, has_shot=True, has_stats=True)
    assert len(full) == len(T.TEMPLATES)


def test_usable_templates_adds_the_shot_layouts_only_with_a_shot():
    without = {t.name for t in T.usable_templates(has_logo=True, has_shot=False,
                                                  has_stats=True)}
    with_shot = {t.name for t in T.usable_templates(has_logo=True, has_shot=True,
                                                    has_stats=True)}
    assert "headline-left-shot-bleed" in with_shot - without


def test_the_catalogue_and_the_renderer_table_agree():
    # A template in the catalogue with no renderer would be offered in the
    # picker and then silently draw something else.
    assert set(T.TEMPLATE_NAMES) == set(T._RENDERERS)
    assert len(T.TEMPLATE_NAMES) == len(set(T.TEMPLATE_NAMES))


def test_the_first_template_needs_nothing_harvested():
    # `render` falls back to it, so it must always be drawable.
    assert not T.TEMPLATES[0].needs_logo
    assert not T.TEMPLATES[0].needs_shot
    assert not T.TEMPLATES[0].needs_stats


# ── backgrounds ──────────────────────────────────────────────────────────────

def test_paint_background_draws_each_kind():
    flat = T.paint_background(T.CardSpec(background=T.Background("flat", "#123456",
                                                                noise=0.0)))
    assert flat.getpixel((600, 300))[:3] == (18, 52, 86)

    linear = T.paint_background(T.CardSpec(background=T.Background(
        "linear", "#000000", stops=[(0.0, "#000000"), (1.0, "#ffffff")],
        angle=90.0, noise=0.0)))
    assert linear.getpixel((4, 315))[0] < linear.getpixel((1195, 315))[0]

    mesh = T.paint_background(T.CardSpec(background=T.Background(
        "mesh", "#000000", [{"color": "#ff0000", "cx": 0.5, "cy": 0.5, "r": 0.4}],
        noise=0.0)))
    assert mesh.getpixel((600, 315))[0] > 180


def test_paint_background_falls_back_to_mesh_for_an_unknown_kind():
    card = T.paint_background(T.CardSpec(background=T.Background("kaleidoscope",
                                                                 "#222222", noise=0.0)))
    assert card.getpixel((10, 10))[:3] == (34, 34, 34)


def test_a_linear_background_with_no_stops_still_ramps():
    card = T.paint_background(T.CardSpec(background=T.Background(
        "linear", "#102030", angle=90.0, noise=0.0)))
    assert card.getpixel((4, 315)) != card.getpixel((1195, 315))


# ── ink ──────────────────────────────────────────────────────────────────────

def test_ink_is_light_on_a_dark_plate_and_dark_on_a_light_one():
    dark = T.paint_background(T.CardSpec(background=T.Background("flat", "#0b0b12",
                                                                 noise=0.0)))
    light = T.paint_background(T.CardSpec(background=T.Background("flat", "#f6f5f9",
                                                                  noise=0.0)))
    assert T.resolve_ink(T.CardSpec(), dark)[0] == 255
    assert T.resolve_ink(T.CardSpec(), light)[0] < 60


def test_an_explicit_ink_overrides_the_measurement():
    dark = T.paint_background(T.CardSpec(background=T.Background("flat", "#0b0b12",
                                                                 noise=0.0)))
    assert T.resolve_ink(T.CardSpec(ink="#ff0000"), dark) == (255, 0, 0, 255)


def test_ink_is_measured_where_the_type_sits_not_across_the_whole_card():
    """The split-plate trap.

    A card that is near-black on the left and near-white on the right averages
    to a mid grey that suits neither. A left-aligned layout must read the left.
    """
    card = P.linear_gradient(P.CARD_SIZE, [(0.0, "#000000"), (0.48, "#000000"),
                                           (0.52, "#ffffff"), (1.0, "#ffffff")], 90.0)
    left_box = (T.MARGIN, 200, 400, 500)
    right_box = (800, 200, P.CARD_W - T.MARGIN, 500)
    assert T.resolve_ink(T.CardSpec(), card, left_box)[0] == 255
    assert T.resolve_ink(T.CardSpec(), card, right_box)[0] < 60


def test_the_headline_is_readable_against_the_plate_it_is_drawn_on(font_set):
    """End to end: render on a light plate and confirm the type is dark."""
    card = T.render(_spec(template="headline-center-plain", background=LIGHT), font_set)
    plate = P.parse_color("#f6f5f9")
    drawn = _ink_pixels(card, plate)
    assert drawn, "nothing was drawn"
    darkest = min(sum(card.convert("RGB").getpixel(xy)) for xy in drawn)
    assert darkest < 200, "the headline is not dark enough to read on a light plate"


# ── the logo ─────────────────────────────────────────────────────────────────

def test_a_light_mark_on_a_light_plate_gets_a_chip(font_set):
    """The failure that a mean colour hides and this catches.

    A white wordmark beside a coloured glyph, on a near-white plate. Without
    the chip the brand name is simply not there.
    """
    card = T.render(_spec(template="headline-center-plain", background=LIGHT,
                          logo=_logo((255, 255, 255, 255))), font_set)
    # The chip is dark, so the top band must now contain dark pixels.
    band = card.convert("RGB").crop((300, 50, 900, 130))
    assert min(sum(px) for px in band.getdata()) < 200


def test_a_light_mark_on_a_dark_plate_is_left_alone(font_set):
    """And the converse: no chip where none is needed, or every card looks fussy."""
    plate = T.Background("flat", "#0b0b12", noise=0.0)
    card = T.render(_spec(template="logo-left-headline-left", background=plate,
                          logo=_logo((255, 255, 255, 255))), font_set)
    # Just left of the mark there should still be bare plate, not a chip edge.
    assert card.convert("RGB").getpixel((T.MARGIN - 20, T.MARGIN + 18)) == (11, 11, 18)


def test_a_card_with_no_logo_draws_no_chip(font_set):
    card = T.render(_spec(template="logo-left-headline-left", logo=None,
                          background=T.Background("flat", "#0b0b12", noise=0.0)),
                    font_set)
    top = card.convert("RGB").crop((0, 0, P.CARD_W, T.MARGIN + 40))
    assert len(set(top.getdata())) == 1


# ── the screenshot ───────────────────────────────────────────────────────────

def test_the_inset_layout_puts_the_panel_on_the_right(font_set):
    card = T.render(_spec(template="headline-left-shot-inset", shot=_shot(),
                          background=T.Background("flat", "#0b0b12", noise=0.0)),
                    font_set)
    # The screenshot's orange top band is unmistakable, and it belongs on the
    # right half only.
    rgb = card.convert("RGB")
    right = any(rgb.getpixel((x, y))[0] > 180 and rgb.getpixel((x, y))[2] < 90
                for x in range(700, 1190, 6) for y in range(120, 240, 6))
    assert right, "the screenshot is not on the right"


def test_the_bleed_layout_runs_the_panel_off_the_right_edge(font_set):
    card = T.render(_spec(template="headline-left-shot-bleed", shot=_shot(),
                          background=T.Background("flat", "#0b0b12", noise=0.0)),
                    font_set)
    rgb = card.convert("RGB")
    # Something other than plate must reach the final column.
    assert any(rgb.getpixel((P.CARD_W - 1, y)) != (11, 11, 18)
               for y in range(100, 530, 4))


def test_the_shot_is_cropped_from_the_top_of_the_page(font_set):
    """A page screenshot is tall; the part worth showing is the first screenful."""
    tall = Image.new("RGBA", (1000, 4000), (0, 0, 0, 255))
    ImageDraw.Draw(tall).rectangle([(0, 0), (999, 400)], fill=(0, 255, 0, 255))
    card = T.render(_spec(template="headline-left-shot-inset", shot=tall), font_set)
    rgb = card.convert("RGB")
    assert any(rgb.getpixel((x, y))[1] > 180 and rgb.getpixel((x, y))[0] < 90
               for x in range(760, 1120, 8) for y in range(130, 260, 8))


def test_stat_tiles_land_on_the_right_of_the_numbers_layout(font_set):
    card = T.render(_spec(template="headline-stats", stats=STATS,
                          background=T.Background("flat", "#0b0b12", noise=0.0)),
                    font_set)
    plate = P.parse_color("#0b0b12")
    drawn = _ink_pixels(card, plate)
    right_side = [x for x, _ in drawn if x > P.CARD_W * 0.55]
    assert len(right_side) > 500, "the tiles were not drawn"
