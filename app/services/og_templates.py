"""Open Graph card layouts.

A template is a pure function of a `CardSpec` and a `FontSet`. Same spec, same
pixels, every time, with no model in the loop and nothing to inspect afterwards.

The catalogue is taken from what shipped cards actually look like rather than
invented: a headline set large on a brand-coloured backdrop, a small logo
lockup, sometimes the product's own UI bleeding off an edge or inset under a
shadow, sometimes a row of numbers, and a muted domain line. Nine layouts cover
essentially all of it, and the differences between them are position and
emphasis, not decoration.

**Why the geometry lives here and not in an agent.** Every one of these layouts
is a grid: one margin, one measured type block, one panel aligned to a column.
An agent placing things by coordinate has to look at its own output and correct
itself, and it still lands a headline under a screenshot now and then. A
function cannot. So the model's job is to choose a template and a palette, and
the pixels are this module's problem.

Each template declares what it needs through `TEMPLATES`, so a caller can show
only the ones it can actually fill: `logo-center` is pointless without a logo,
and the split layouts are pointless without a screenshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from PIL import Image

from app.services import og_paint as P
from app.services.og_fonts import FontSet

logger = logging.getLogger(__name__)

#: The outer gutter. Consumers crop a card slightly differently from each other
#: and WhatsApp rounds the corners, so nothing meaningful goes nearer the edge.
MARGIN = 72

#: Height reserved for the logo lockup wherever one sits above the headline.
LOGO_H = 44

#: Everything below the headline in a stack: the gap before a subline, before a
#: domain line, and the domain's own size. Kept together so a change to the
#: rhythm is one edit rather than nine.
GAP_SUB = 26
GAP_DOMAIN = 30
SIZE_SUB = 27
SIZE_DOMAIN = 22


@dataclass
class Background:
    """How the plate behind everything is painted.

    `kind` is `mesh`, `linear` or `flat`. The others are read per kind and
    ignored otherwise, so one shape carries every background the catalogue can
    draw and a spec never has to be a union type.
    """

    kind: str = "mesh"
    base: str = "#0b0b12"
    blobs: list[dict[str, Any]] = field(default_factory=list)
    stops: list[tuple[float, str]] = field(default_factory=list)
    angle: float = 120.0
    grid: float = 0.0
    noise: float = 0.03
    vignette: float = 0.0


@dataclass
class CardSpec:
    """Everything one card is made of.

    Images arrive as Pillow objects rather than paths because the harvester
    already has them in memory and a card render should not be a series of
    round trips through the filesystem.
    """

    template: str = "logo-top-headline-center"
    headline: str = ""
    subline: str = ""
    domain: str = ""
    background: Background = field(default_factory=Background)
    #: Text colour. Left empty, each template measures the plate and picks.
    ink: str = ""
    #: Used for an accent word, a gradient headline fill and tile numbers.
    accent: str = "#7c3aed"
    logo: Image.Image | None = None
    shot: Image.Image | None = None
    stats: list[dict[str, str]] = field(default_factory=list)
    #: Fill the headline with an accent ramp rather than flat ink.
    gradient_headline: bool = False


@dataclass(frozen=True)
class TemplateInfo:
    """What a layout is called, and what it cannot be drawn without."""

    name: str
    label: str
    description: str
    needs_logo: bool = False
    needs_shot: bool = False
    needs_stats: bool = False


# ── shared pieces ────────────────────────────────────────────────────────────

def paint_background(spec: CardSpec) -> Image.Image:
    """The plate, before anything is placed on it."""
    bg = spec.background
    if bg.kind == "flat":
        card = P.flat(P.CARD_SIZE, bg.base)
    elif bg.kind == "linear":
        stops = bg.stops or [(0.0, bg.base), (1.0, P.to_hex(P.lighten(P.parse_color(bg.base), 0.3)))]
        card = P.linear_gradient(P.CARD_SIZE, stops, bg.angle)
    else:
        card = P.mesh_gradient(P.CARD_SIZE, bg.base, bg.blobs)

    if bg.grid > 0:
        card = P.grid_overlay(card, 60, "#ffffff", bg.grid)
    if bg.vignette > 0:
        card = P.vignette(card, bg.vignette)
    if bg.noise > 0:
        card = P.noise_overlay(card, bg.noise)
    return card


#: The default text region: the middle band, which is where every layout puts
#: its headline even when the horizontal alignment differs.
_TEXT_BOX = (MARGIN, int(P.CARD_H * 0.28), P.CARD_W - MARGIN, int(P.CARD_H * 0.82))


def resolve_ink(spec: CardSpec, card: Image.Image,
                box: tuple[int, int, int, int] | None = None) -> P.RGBA:
    """Text colour: what the spec asked for, else what the plate can carry.

    Sampled from the rectangle the type actually occupies, which each layout
    passes for itself. Averaging the whole card is the trap: a plate that is
    near-black on the left and bright magenta on the right averages to a
    mid-tone that suits neither half, and the headline only ever sits in one of
    them.
    """
    if spec.ink:
        return P.parse_color(spec.ink)
    return P.ink_for(P.region_color(card, box or _TEXT_BOX))


def _muted(ink: P.RGBA) -> P.RGBA:
    """The domain and subline colour: the ink, stepped back."""
    return P.with_alpha(ink, 0.68)


#: A mark is chipped when this much of it would be hard to see on the plate.
#: Not zero: a logo with one pale accent in it is fine, and chipping every mark
#: that has a highlight would look fussy. A quarter of the mark in trouble means
#: a whole element of it, usually the wordmark, has gone.
_LOGO_LOST_FRACTION = 0.25


def _place_logo(card: Image.Image, spec: CardSpec, xy: tuple[int, int],
                height: int = LOGO_H, align: str = "left") -> int:
    """Draw the lockup and return the width it used. 0 when there is no logo.

    A white wordmark on a light plate disappears completely, and that is not a
    hypothetical: brands ship one logo file, usually the light one, and the
    backdrop here is chosen by palette rather than by what the mark needs. So
    the mark is measured against the pixels it is about to land on, and given a
    chip when it would otherwise vanish. Tinting the mark instead is not an
    option, because a two-colour logo tints into something that is no longer
    the brand's mark.
    """
    if spec.logo is None:
        return 0
    mark = P.fit_contain(spec.logo, int(P.CARD_W * 0.34), height)

    x, y = xy
    if align == "center":
        x = int(x - mark.width / 2)
    elif align == "right":
        x = int(x - mark.width)

    plate = P.region_color(card, (x, y, x + mark.width, y + mark.height))
    if P.low_contrast_fraction(mark, plate) >= _LOGO_LOST_FRACTION:
        # The chip takes the tone the LOST pixels need behind them, which is the
        # opposite of the plate they are currently lost against.
        chip = P.contrast_chip((mark.width, mark.height),
                               P.with_alpha(P.ink_for(plate), 0.94), 12, 14)
        P.paste_rgba(card, chip, (x - 14, y - 14))

    P.paste_rgba(card, mark, (x, y))
    return mark.width


def _headline(card: Image.Image, spec: CardSpec, fonts: FontSet, ink: P.RGBA,
              xy: tuple[int, int], box: tuple[int, int], align: str = "left",
              start: int = 96, max_lines: int = 3) -> int:
    """Set the headline into a box and return the height it used."""
    if not spec.headline:
        return 0
    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   box[0], box[1], max_lines=max_lines,
                                   start_size=start, min_size=34, line_height=1.08)
    if spec.gradient_headline:
        accent = P.parse_color(spec.accent)
        return P.gradient_text(card, lines, font, size, xy,
                               [(0.0, ink), (1.0, accent)], 100.0,
                               1.08, align, box[0])
    return P.draw_lines(card, lines, font, size, xy, ink, 1.08, align, box[0])


def _subline(card: Image.Image, spec: CardSpec, fonts: FontSet, ink: P.RGBA,
             xy: tuple[int, int], width: int, align: str = "left",
             max_lines: int = 2) -> int:
    if not spec.subline:
        return 0
    font, lines, size = P.fit_text(spec.subline, fonts.loader("body"),
                                   width, 200, max_lines=max_lines,
                                   start_size=SIZE_SUB, min_size=19, line_height=1.34)
    return P.draw_lines(card, lines, font, size, xy, _muted(ink), 1.34, align, width)


def _domain(card: Image.Image, spec: CardSpec, fonts: FontSet, ink: P.RGBA,
            xy: tuple[int, int], width: int, align: str = "left") -> int:
    if not spec.domain:
        return 0
    font = fonts.body(SIZE_DOMAIN)
    return P.draw_lines(card, [spec.domain], font, SIZE_DOMAIN, xy,
                        _muted(ink), 1.2, align, width)


def _shot_panel(shot: Image.Image, width: int, height: int,
                radius: int = 14, anchor: str = "top-center") -> Image.Image:
    """A screenshot cropped to a box, rounded, with a hairline edge.

    Cropped `cover` from the top by default: a page screenshot is tall and the
    part worth showing is the first screenful, never the middle of the scroll.
    """
    panel = P.fit_cover(shot, width, height, anchor)
    return P.rounded_panel(panel, radius, 1, "#ffffff", 0.18)


def _with_shadow(card: Image.Image, panel: Image.Image, xy: tuple[int, int],
                 blur: float = 34.0, offset: tuple[int, int] = (0, 22),
                 alpha: float = 0.5) -> None:
    shadow, (dx, dy) = P.drop_shadow(panel, blur, offset, "#000000", alpha)
    P.paste_rgba(card, shadow, (xy[0] + dx, xy[1] + dy))
    P.paste_rgba(card, panel, xy)


# ── the layouts ──────────────────────────────────────────────────────────────

def _logo_center(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Just the mark, centred. The plainest card there is, and often the best."""
    card = paint_background(spec)
    if spec.logo is not None:
        mark = P.fit_contain(spec.logo, int(P.CARD_W * 0.46), int(P.CARD_H * 0.30))
        P.paste_rgba(card, mark, ((P.CARD_W - mark.width) // 2,
                                  (P.CARD_H - mark.height) // 2))
        return card
    # No mark: fall back to the wordmark set as type, so the card is never empty.
    ink = resolve_ink(spec, card)
    font, lines, size = P.fit_text(spec.headline or spec.domain, fonts.loader("display"),
                                   P.CARD_W - MARGIN * 2, 240, 2, 104, 40)
    w, h = P.text_block_size(lines, font, size, 1.08)
    P.draw_lines(card, lines, font, size, ((P.CARD_W - w) // 2, (P.CARD_H - h) // 2),
                 ink, 1.08, "center", w)
    return card


def _logo_top_headline_center(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Small lockup at the top, headline in the middle, domain underneath.

    The most common card on the internet, and the one that survives every
    consumer's crop because everything sits in the middle third.
    """
    card = paint_background(spec)
    ink = resolve_ink(spec, card)
    box_w = P.CARD_W - MARGIN * 2

    has_logo = spec.logo is not None
    if has_logo:
        _place_logo(card, spec, (P.CARD_W // 2, MARGIN), 40, "center")

    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   int(box_w * 0.86), 260, 3, 92, 36, 1.1)
    block_w, block_h = P.text_block_size(lines, font, size, 1.1)
    tail = (GAP_DOMAIN + SIZE_DOMAIN) if spec.domain else 0
    sub_h = 0
    if spec.subline:
        sub_h = GAP_SUB + SIZE_SUB * 2
    top = (P.CARD_H - (block_h + sub_h + tail)) // 2 + (12 if has_logo else 0)

    x0 = (P.CARD_W - block_w) // 2
    if spec.gradient_headline:
        P.gradient_text(card, lines, font, size, (x0, top),
                        [(0.0, ink), (1.0, P.parse_color(spec.accent))],
                        100.0, 1.1, "center", block_w)
    else:
        P.draw_lines(card, lines, font, size, (x0, top), ink, 1.1, "center", block_w)

    y = top + block_h
    if spec.subline:
        y += GAP_SUB
        y += _subline(card, spec, fonts, ink, (MARGIN, y), box_w, "center")
    if spec.domain:
        _domain(card, spec, fonts, ink, (MARGIN, y + GAP_DOMAIN), box_w, "center")
    return card


def _logo_left_headline_left(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Lockup top-left, headline on the left, domain at the foot.

    A left rag reads faster than centred type at this size, so this is the one
    to use when the headline is long.
    """
    card = paint_background(spec)
    box_w = int((P.CARD_W - MARGIN * 2) * 0.82)
    ink = resolve_ink(spec, card, (MARGIN, int(P.CARD_H * 0.28),
                                   MARGIN + box_w, int(P.CARD_H * 0.82)))

    _place_logo(card, spec, (MARGIN, MARGIN), 40)

    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   box_w, 280, 3, 96, 36, 1.06)
    _, block_h = P.text_block_size(lines, font, size, 1.06)
    sub_h = (GAP_SUB + SIZE_SUB * 2) if spec.subline else 0
    top = (P.CARD_H - (block_h + sub_h)) // 2 + 10

    P.draw_lines(card, lines, font, size, (MARGIN, top), ink, 1.06, "left", box_w)
    y = top + block_h
    if spec.subline:
        _subline(card, spec, fonts, ink, (MARGIN, y + GAP_SUB), box_w)
    _domain(card, spec, fonts, ink, (MARGIN, P.CARD_H - MARGIN - SIZE_DOMAIN), box_w)
    return card


def _headline_bottom_left(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Lockup top-left, the headline set large and dropped to the foot.

    Gives the plate room to breathe at the top, which suits a backdrop with a
    strong wash in one corner.
    """
    card = paint_background(spec)
    box_w = int((P.CARD_W - MARGIN * 2) * 0.72)
    # Lower band: this layout's type sits against the foot of the plate.
    ink = resolve_ink(spec, card, (MARGIN, int(P.CARD_H * 0.45),
                                   MARGIN + box_w, P.CARD_H - MARGIN))

    _place_logo(card, spec, (MARGIN, MARGIN), 38)

    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   box_w, 300, 3, 108, 40, 1.04)
    _, block_h = P.text_block_size(lines, font, size, 1.04)
    tail = (GAP_DOMAIN + SIZE_DOMAIN) if spec.domain else 0
    top = P.CARD_H - MARGIN - tail - block_h

    P.draw_lines(card, lines, font, size, (MARGIN, top), ink, 1.04, "left", box_w)
    if spec.domain:
        _domain(card, spec, fonts, ink, (MARGIN, top + block_h + GAP_DOMAIN), box_w)
    return card


def _headline_left_shot_bleed(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Headline on the left, the product running off the right edge.

    The bleed is the point: a panel that stops short reads as a picture of an
    app, and one that runs off the edge reads as the app itself.
    """
    card = paint_background(spec)
    ink = resolve_ink(spec, card, (MARGIN, int(P.CARD_H * 0.24),
                                   int(P.CARD_W * 0.46), int(P.CARD_H * 0.86)))

    if spec.shot is not None:
        panel_w = int(P.CARD_W * 0.52)
        panel_h = int(P.CARD_H * 0.74)
        panel = _shot_panel(spec.shot, panel_w, panel_h, 14, "top-left")
        x = P.CARD_W - int(panel_w * 0.82)   # the last fifth hangs off the edge
        y = (P.CARD_H - panel_h) // 2
        _with_shadow(card, panel, (x, y), 40.0, (-8, 18), 0.55)

    box_w = int(P.CARD_W * 0.42)
    _place_logo(card, spec, (MARGIN, MARGIN), 38)

    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   box_w, 300, 4, 74, 32, 1.08)
    _, block_h = P.text_block_size(lines, font, size, 1.08)
    sub_h = (GAP_SUB + SIZE_SUB * 2) if spec.subline else 0
    top = (P.CARD_H - (block_h + sub_h)) // 2

    P.draw_lines(card, lines, font, size, (MARGIN, top), ink, 1.08, "left", box_w)
    y = top + block_h
    if spec.subline:
        _subline(card, spec, fonts, ink, (MARGIN, y + GAP_SUB), box_w, "left", 3)
    _domain(card, spec, fonts, ink, (MARGIN, P.CARD_H - MARGIN - SIZE_DOMAIN), box_w)
    return card


def _headline_left_shot_inset(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Headline and subline left, the product inset on the right under a shadow.

    The considered version of the bleed layout: everything is inside the card,
    so it holds up when a consumer crops tight.
    """
    card = paint_background(spec)
    ink = resolve_ink(spec, card, (MARGIN, int(P.CARD_H * 0.24),
                                   int(P.CARD_W * 0.46), int(P.CARD_H * 0.86)))

    if spec.shot is not None:
        panel_w = int(P.CARD_W * 0.44)
        panel_h = int(P.CARD_H * 0.64)
        panel = _shot_panel(spec.shot, panel_w, panel_h, 16, "top-center")
        x = P.CARD_W - MARGIN - panel_w
        y = (P.CARD_H - panel_h) // 2
        _with_shadow(card, panel, (x, y), 30.0, (0, 20), 0.5)

    box_w = int(P.CARD_W * 0.40)
    logo_h = _place_logo(card, spec, (MARGIN, MARGIN), 36)

    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   box_w, 250, 4, 66, 30, 1.1)
    _, block_h = P.text_block_size(lines, font, size, 1.1)
    sub_h = (GAP_SUB + SIZE_SUB * 2) if spec.subline else 0
    top = (P.CARD_H - (block_h + sub_h)) // 2 + (8 if logo_h else 0)

    P.draw_lines(card, lines, font, size, (MARGIN, top), ink, 1.1, "left", box_w)
    y = top + block_h
    if spec.subline:
        _subline(card, spec, fonts, ink, (MARGIN, y + GAP_SUB), box_w, "left", 3)
    _domain(card, spec, fonts, ink, (MARGIN, P.CARD_H - MARGIN - SIZE_DOMAIN), box_w)
    return card


def _headline_top_shot_below(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Headline across the top, the product below it and cropped by the foot.

    Best when the headline is short. The panel is deliberately allowed to run
    past the bottom edge so the card feels like a window onto something larger.
    """
    card = paint_background(spec)
    box_w = P.CARD_W - MARGIN * 2
    # Top band: everything below it is about to be covered by the panel.
    ink = resolve_ink(spec, card, (MARGIN, 20, P.CARD_W - MARGIN, int(P.CARD_H * 0.30)))

    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   int(box_w * 0.76), 150, 2, 68, 30, 1.08)
    block_w, block_h = P.text_block_size(lines, font, size, 1.08)
    top = int(MARGIN * 0.62)
    x0 = (P.CARD_W - block_w) // 2
    if spec.gradient_headline:
        P.gradient_text(card, lines, font, size, (x0, top),
                        [(0.0, ink), (1.0, P.parse_color(spec.accent))],
                        100.0, 1.08, "center", block_w)
    else:
        P.draw_lines(card, lines, font, size, (x0, top), ink, 1.08, "center", block_w)

    if spec.shot is not None:
        panel_top = top + block_h + 40
        panel_w = int(P.CARD_W * 0.74)
        panel_h = P.CARD_H - panel_top + 30   # runs off the bottom on purpose
        panel = _shot_panel(spec.shot, panel_w, panel_h, 14, "top-center")
        _with_shadow(card, panel, ((P.CARD_W - panel_w) // 2, panel_top),
                     36.0, (0, 14), 0.55)
    return card


def _headline_stats(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Headline top-left, a grid of numbers on the right, domain at the foot."""
    card = paint_background(spec)
    tiles_w = int(P.CARD_W * 0.46)
    box_w = P.CARD_W - MARGIN * 3 - tiles_w
    ink = resolve_ink(spec, card, (MARGIN, int(P.CARD_H * 0.24),
                                   MARGIN + box_w, int(P.CARD_H * 0.86)))

    _place_logo(card, spec, (MARGIN, MARGIN), 36)
    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   box_w, 260, 4, 62, 28, 1.1)
    _, block_h = P.text_block_size(lines, font, size, 1.1)
    top = (P.CARD_H - block_h) // 2
    P.draw_lines(card, lines, font, size, (MARGIN, top), ink, 1.1, "left", box_w)
    _domain(card, spec, fonts, ink, (MARGIN, P.CARD_H - MARGIN - SIZE_DOMAIN), box_w)

    if spec.stats:
        tiles_h = int(P.CARD_H * 0.62)
        plate = P.parse_color(spec.background.base)
        tile_fill = P.lighten(plate, 0.10) if P.relative_luminance(plate) < 0.4 \
            else P.darken(plate, 0.08)
        tiles = P.stat_tiles((tiles_w, tiles_h), spec.stats[:4],
                             fonts.display(46), fonts.body(17),
                             tile_fill, ink, _muted(ink), 18, 14, 2)
        P.paste_rgba(card, tiles, (P.CARD_W - MARGIN - tiles_w, (P.CARD_H - tiles_h) // 2))
    return card


def _headline_center_plain(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Lockup, headline, subline, all centred on a quiet plate.

    The layout for a light card. No panel and no numbers, so the type has to
    carry it, which is why the headline starts larger here than anywhere else.
    """
    card = paint_background(spec)
    ink = resolve_ink(spec, card)
    box_w = int((P.CARD_W - MARGIN * 2) * 0.88)

    logo_w = _place_logo(card, spec, (P.CARD_W // 2, MARGIN - 6), 38, "center")

    font, lines, size = P.fit_text(spec.headline, fonts.loader("display"),
                                   box_w, 280, 3, 100, 38, 1.06)
    block_w, block_h = P.text_block_size(lines, font, size, 1.06)
    sub_h = (GAP_SUB + SIZE_SUB * 2) if spec.subline else 0
    top = (P.CARD_H - (block_h + sub_h)) // 2 + (10 if logo_w else 0)

    P.draw_lines(card, lines, font, size, ((P.CARD_W - block_w) // 2, top),
                 ink, 1.06, "center", block_w)
    if spec.subline:
        _subline(card, spec, fonts, ink, (MARGIN, top + block_h + GAP_SUB),
                 P.CARD_W - MARGIN * 2, "center")
    return card


#: The catalogue. Order is the order a picker shows them in, so the layouts that
#: work with the least harvested material come first: a card with no screenshot
#: and no logo still has five options.
TEMPLATES: tuple[TemplateInfo, ...] = (
    TemplateInfo("logo-top-headline-center", "Centred",
                 "Small logo at the top, headline in the middle, domain underneath."),
    TemplateInfo("logo-left-headline-left", "Left rag",
                 "Logo top-left and the headline on the left. Best for long headlines."),
    TemplateInfo("headline-bottom-left", "Dropped",
                 "Headline set large at the foot, leaving the plate open above."),
    TemplateInfo("headline-center-plain", "Quiet",
                 "Everything centred on a plain plate. For light, typographic cards."),
    TemplateInfo("headline-stats", "Numbers",
                 "Headline on the left with a grid of figures on the right.",
                 needs_stats=True),
    TemplateInfo("headline-left-shot-inset", "Product, inset",
                 "Headline left, the site's own screen inset on the right under a shadow.",
                 needs_shot=True),
    TemplateInfo("headline-left-shot-bleed", "Product, bleeding",
                 "Headline left, the site's own screen running off the right edge.",
                 needs_shot=True),
    TemplateInfo("headline-top-shot-below", "Product, below",
                 "Short headline across the top with the screen cropped by the foot.",
                 needs_shot=True),
    TemplateInfo("logo-center", "Mark only",
                 "The logo alone, centred. Plain and hard to get wrong.",
                 needs_logo=True),
)

_RENDERERS: dict[str, Callable[[CardSpec, FontSet], Image.Image]] = {
    "logo-top-headline-center": _logo_top_headline_center,
    "logo-left-headline-left": _logo_left_headline_left,
    "headline-bottom-left": _headline_bottom_left,
    "headline-center-plain": _headline_center_plain,
    "headline-stats": _headline_stats,
    "headline-left-shot-inset": _headline_left_shot_inset,
    "headline-left-shot-bleed": _headline_left_shot_bleed,
    "headline-top-shot-below": _headline_top_shot_below,
    "logo-center": _logo_center,
}

TEMPLATE_NAMES: tuple[str, ...] = tuple(t.name for t in TEMPLATES)

_INFO: dict[str, TemplateInfo] = {t.name: t for t in TEMPLATES}


def usable_templates(*, has_logo: bool, has_shot: bool,
                     has_stats: bool) -> tuple[TemplateInfo, ...]:
    """The layouts that can actually be drawn with the material on hand."""
    return tuple(t for t in TEMPLATES
                 if (not t.needs_logo or has_logo)
                 and (not t.needs_shot or has_shot)
                 and (not t.needs_stats or has_stats))


def render(spec: CardSpec, fonts: FontSet) -> Image.Image:
    """Draw one card. Falls back to the default layout for an unknown name.

    An unknown template is what a model hallucinating a plausible-sounding name
    produces, and the right answer to that is a card in the default layout with
    a line in the log, not an exception that loses the user their click.
    """
    name = spec.template if spec.template in _RENDERERS else TEMPLATE_NAMES[0]
    if name != spec.template:
        logger.warning("unknown og template %r, drawing %r instead", spec.template, name)

    info = _INFO[name]
    # A layout asked for without its material draws an empty column, which looks
    # broken rather than plain. Step down to something that works instead.
    if (info.needs_shot and spec.shot is None) or (info.needs_logo and spec.logo is None) \
            or (info.needs_stats and not spec.stats):
        fallback = usable_templates(has_logo=spec.logo is not None,
                                    has_shot=spec.shot is not None,
                                    has_stats=bool(spec.stats))
        name = fallback[0].name if fallback else TEMPLATE_NAMES[0]
        logger.info("og template %r lacks its material, drawing %r", spec.template, name)

    return _RENDERERS[name](spec, fonts)
