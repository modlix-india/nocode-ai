"""Deterministic drawing primitives for Open Graph cards.

Everything a social card is made of, drawn with Pillow and nothing else: no
model, no network, no randomness that is not seeded. Hand the same spec to this
module twice and you get the same bytes twice.

Why this exists at all. The cards people actually ship - the ones with a
headline, a logo lockup, a mesh-gradient backdrop and the product's own UI
bleeding off one edge - contain no generated imagery. What makes them look
expensive is type set on a grid, the real mark, and the real product. A
diffusion model cannot produce any of that: it redraws a logo rather than
placing it, renders lettering as mush, and ignores a layout grid entirely. So
the model's job moves upstream to choosing, and this module does the drawing.

The vocabulary here is deliberately small and composable:

    backgrounds   flat, linear_gradient, mesh_gradient, plus grid/noise/vignette
                  overlays that sit on top of any of them
    panels        rounded_panel and drop_shadow, for insetting a screenshot
    type          fit_text measures rather than counts, then draw_lines paints
    marks         fit_contain for a logo, paste_rgba for anything with alpha
    colour        parse_color, mix, ink_for and contrast_ratio

Coordinates are pixels with the origin top-left. Everything works in RGBA and
the caller flattens at the end, because compositing a shadow under a panel over
a gradient goes wrong the moment an intermediate loses its alpha.
"""

from __future__ import annotations

import colorsys
import logging
import math
from typing import Any, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

#: The card. LinkedIn documents 1200x627 as the minimum and 1.91:1 as the
#: recommended ratio; 1200x630 is the number everyone actually ships and is what
#: `og_image_ai.to_card` already crops to, so the two agree by construction.
CARD_W = 1200
CARD_H = 630
CARD_SIZE = (CARD_W, CARD_H)

RGBA = tuple[int, int, int, int]

#: Named colours we accept beyond hex, kept tiny on purpose. A card spec that
#: says "papayawhip" is a spec nobody wrote deliberately.
_NAMED: dict[str, str] = {
    "white": "#ffffff",
    "black": "#000000",
    "transparent": "#00000000",
}


# ── colour ───────────────────────────────────────────────────────────────────

def parse_color(value: Any, default: RGBA = (0, 0, 0, 255)) -> RGBA:
    """`#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb()`, `rgba()` or a tuple, to RGBA.

    Never raises. A colour that cannot be read is the caller's `default`, since
    a card with one wrong colour is worth shipping and a traceback is not.
    """
    if value is None:
        return default

    if isinstance(value, (tuple, list)):
        parts = [int(p) for p in value[:4]]
        while len(parts) < 3:
            parts.append(0)
        if len(parts) == 3:
            parts.append(255)
        return (_clamp8(parts[0]), _clamp8(parts[1]), _clamp8(parts[2]), _clamp8(parts[3]))

    text = str(value).strip().lower()
    if not text:
        return default
    text = _NAMED.get(text, text)

    if text.startswith(("rgb(", "rgba(")):
        inner = text[text.index("(") + 1: text.rindex(")")] if ")" in text else ""
        bits = [b.strip() for b in inner.replace("/", ",").split(",") if b.strip()]
        try:
            nums = [float(b.rstrip("%")) for b in bits]
        except ValueError:
            return default
        if len(nums) < 3:
            return default
        alpha = nums[3] if len(nums) > 3 else 1.0
        # `rgba(0,0,0,.5)` and `rgba(0,0,0,50%)` both arrive here; a fraction
        # under 1 is an opacity, anything larger is already 0-255.
        a = int(round(alpha * 255)) if alpha <= 1.0 else int(round(alpha))
        return (_clamp8(nums[0]), _clamp8(nums[1]), _clamp8(nums[2]), _clamp8(a))

    hexed = text.lstrip("#")
    if len(hexed) == 3:
        hexed = "".join(c * 2 for c in hexed)
    elif len(hexed) == 4:
        hexed = "".join(c * 2 for c in hexed)
    if len(hexed) not in (6, 8):
        return default
    try:
        r = int(hexed[0:2], 16)
        g = int(hexed[2:4], 16)
        b = int(hexed[4:6], 16)
        a = int(hexed[6:8], 16) if len(hexed) == 8 else 255
    except ValueError:
        return default
    return (r, g, b, a)


def _clamp8(v: float) -> int:
    return max(0, min(255, int(round(v))))


def to_hex(color: RGBA) -> str:
    """`#rrggbb`, dropping alpha. For logging and for spec round-trips."""
    r, g, b = color[0], color[1], color[2]
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(a: RGBA, b: RGBA, t: float) -> RGBA:
    """Linear blend, `t=0` is all `a` and `t=1` is all `b`."""
    t = max(0.0, min(1.0, t))
    return tuple(_clamp8(a[i] + (b[i] - a[i]) * t) for i in range(4))  # type: ignore[return-value]


def lighten(color: RGBA, t: float) -> RGBA:
    return mix(color, (255, 255, 255, color[3]), t)


def darken(color: RGBA, t: float) -> RGBA:
    return mix(color, (0, 0, 0, color[3]), t)


def with_alpha(color: RGBA, alpha: float) -> RGBA:
    """Same hue, new opacity. `alpha` is 0..1 or 0..255."""
    a = int(round(alpha * 255)) if alpha <= 1.0 else int(round(alpha))
    return (color[0], color[1], color[2], _clamp8(a))


def rotate_hue(color: RGBA, degrees: float) -> RGBA:
    """Shift hue, keeping saturation and value. Builds a partner for a ramp."""
    r, g, b = (c / 255 for c in color[:3])
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h = (h + degrees / 360.0) % 1.0
    nr, ng, nb = colorsys.hsv_to_rgb(h, s, v)
    return (_clamp8(nr * 255), _clamp8(ng * 255), _clamp8(nb * 255), color[3])


def relative_luminance(color: RGBA) -> float:
    """WCAG relative luminance, 0 for black and 1 for white."""
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in color[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: RGBA, b: RGBA) -> float:
    """WCAG contrast between two colours, 1.0 to 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ink_for(background: RGBA,
            light: RGBA = (255, 255, 255, 255),
            dark: RGBA = (16, 18, 24, 255)) -> RGBA:
    """Readable text colour for `background`: whichever of the two wins.

    Picked by measured contrast rather than by a luminance threshold, because a
    mid-tone brand colour sits either side of any threshold you choose and the
    wrong call there is an unreadable headline.
    """
    return light if contrast_ratio(background, light) >= contrast_ratio(background, dark) else dark


# ── backgrounds ──────────────────────────────────────────────────────────────

def flat(size: tuple[int, int], color: Any) -> Image.Image:
    """A solid plate."""
    return Image.new("RGBA", size, parse_color(color))


def _ramp_luts(stops: Sequence[tuple[float, RGBA]]) -> tuple[list[int], list[int], list[int], list[int]]:
    """Four 256-entry lookup tables (R, G, B, A) sampling `stops`.

    Mapping a greyscale gradient through a LUT is what makes an arbitrary
    multi-stop ramp cheap: the gradient is computed once in `L`, then each
    channel is a single `point()` call in C rather than a Python pixel loop.
    """
    ordered = sorted(((max(0.0, min(1.0, p)), c) for p, c in stops), key=lambda s: s[0])
    if not ordered:
        ordered = [(0.0, (0, 0, 0, 255))]
    if ordered[0][0] > 0.0:
        ordered.insert(0, (0.0, ordered[0][1]))
    if ordered[-1][0] < 1.0:
        ordered.append((1.0, ordered[-1][1]))

    luts: tuple[list[int], list[int], list[int], list[int]] = ([], [], [], [])
    seg = 0
    for i in range(256):
        t = i / 255
        while seg < len(ordered) - 2 and t > ordered[seg + 1][0]:
            seg += 1
        p0, c0 = ordered[seg]
        p1, c1 = ordered[min(seg + 1, len(ordered) - 1)]
        span = (p1 - p0) or 1.0
        local = max(0.0, min(1.0, (t - p0) / span))
        blended = mix(c0, c1, local)
        for ch in range(4):
            luts[ch].append(blended[ch])
    return luts


def linear_gradient(size: tuple[int, int],
                    stops: Sequence[tuple[float, Any]],
                    angle: float = 90.0) -> Image.Image:
    """A multi-stop linear ramp across `size` at `angle` degrees.

    `angle` follows CSS: 0 runs bottom to top, 90 runs left to right, 180 top to
    bottom. Stops are `(position 0..1, colour)`.

    Built by rotating an oversized greyscale ramp and centre-cropping, so the
    corners of a diagonal are real gradient rather than the black Pillow pads a
    rotation with.
    """
    w, h = size
    parsed = [(p, parse_color(c)) for p, c in stops]
    if not parsed:
        parsed = [(0.0, (0, 0, 0, 255)), (1.0, (255, 255, 255, 255))]

    # The diagonal is the longest run any rotation can need; anything smaller
    # leaves rotated-in corners empty.
    span = int(math.ceil(math.hypot(w, h))) + 2
    ramp = Image.linear_gradient("L").resize((span, span), Image.BICUBIC)
    # Pillow's L gradient runs black at the top to white at the bottom, which is
    # CSS `180deg`. Rotate the difference, negated because PIL rotates counter-
    # clockwise and CSS angles run clockwise.
    ramp = ramp.rotate(-(angle - 180.0), resample=Image.BICUBIC, expand=False)

    left = (span - w) // 2
    top = (span - h) // 2
    ramp = ramp.crop((left, top, left + w, top + h))

    lut_r, lut_g, lut_b, lut_a = _ramp_luts(parsed)
    r = ramp.point(lut_r)
    g = ramp.point(lut_g)
    b = ramp.point(lut_b)
    a = ramp.point(lut_a)
    return Image.merge("RGBA", (r, g, b, a))


#: One radial alpha mask, built once and resized per blob.
_RADIAL_CACHE: dict[int, Image.Image] = {}

#: Resolution of that mask. It is only ever scaled up to blob size and the
#: result is a soft wash, so 256 is plenty and keeps the one-time build cheap.
_RADIAL_N = 256


def _build_radial_base() -> Image.Image:
    """An alpha mask that is opaque at the centre and reaches exactly zero at
    the inscribed circle.

    Built by hand rather than from `Image.radial_gradient`, which normalises so
    that its *corner* is white. That leaves it 30% opaque at the midpoint of
    each edge (measured: 179 of 255), so every blob drawn with it shows a hard
    square seam where the mask runs out. The difference is invisible in a unit
    test and glaring on a gradient.

    Falloff is smoothstep rather than linear, which is what makes a blob read as
    a light source rather than as a cone.
    """
    half = (_RADIAL_N - 1) / 2
    data = bytearray(_RADIAL_N * _RADIAL_N)
    for y in range(_RADIAL_N):
        dy = (y - half) / half
        row = y * _RADIAL_N
        for x in range(_RADIAL_N):
            dx = (x - half) / half
            distance = math.hypot(dx, dy)
            if distance >= 1.0:
                continue
            t = 1.0 - distance
            data[row + x] = _clamp8(255 * t * t * (3.0 - 2.0 * t))
    return Image.frombytes("L", (_RADIAL_N, _RADIAL_N), bytes(data))


def _radial_mask(diameter: int, falloff: float = 1.0) -> Image.Image:
    base = _RADIAL_CACHE.get(0)
    if base is None:
        base = _build_radial_base()
        _RADIAL_CACHE[0] = base
    mask = base.resize((max(1, diameter), max(1, diameter)), Image.BICUBIC)
    if falloff != 1.0:
        # A falloff above 1 tightens the blob toward its centre, below 1 spreads
        # it. Applied as a gamma on the mask so the edge stays smooth.
        gamma = max(0.05, falloff)
        mask = mask.point(lambda v: _clamp8(255 * ((v / 255) ** gamma)))
    return mask


def mesh_gradient(size: tuple[int, int],
                  base: Any,
                  blobs: Iterable[dict[str, Any]],
                  blur: float = 0.0) -> Image.Image:
    """Soft overlapping colour blobs on a flat base.

    This is the look behind most modern social cards: two or three wide radial
    washes of brand colour bleeding into a dark plate. Each blob is a dict:

        {"color": "#7c3aed", "cx": 0.7, "cy": 0.2, "r": 0.8,
         "alpha": 0.9, "falloff": 1.0}

    `cx`/`cy`/`r` are fractions of the card width, so a spec stays valid at any
    canvas size. `r` is the radius, not the diameter.
    """
    w, h = size
    canvas = Image.new("RGBA", size, parse_color(base))

    for blob in blobs or []:
        if not isinstance(blob, dict):
            continue
        color = parse_color(blob.get("color"), (0, 0, 0, 0))
        if color[3] == 0:
            continue
        radius = float(blob.get("r", 0.6)) * w
        diameter = max(2, int(round(radius * 2)))
        alpha = float(blob.get("alpha", 1.0))
        mask = _radial_mask(diameter, float(blob.get("falloff", 1.0)))
        if alpha < 1.0:
            mask = mask.point(lambda v, a=alpha: _clamp8(v * a))

        layer = Image.new("RGBA", (diameter, diameter), (color[0], color[1], color[2], 255))
        layer.putalpha(mask)

        cx = float(blob.get("cx", 0.5)) * w
        cy = float(blob.get("cy", 0.5)) * h
        canvas.alpha_composite(layer, (int(round(cx - diameter / 2)), int(round(cy - diameter / 2))))

    if blur > 0:
        canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    return canvas


def grid_overlay(image: Image.Image,
                 spacing: int = 48,
                 color: Any = "#ffffff",
                 alpha: float = 0.06,
                 width: int = 1) -> Image.Image:
    """Faint graph-paper rules over an existing plate."""
    w, h = image.size
    line = with_alpha(parse_color(color), alpha)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    step = max(4, int(spacing))
    for x in range(0, w + step, step):
        draw.line([(x, 0), (x, h)], fill=line, width=width)
    for y in range(0, h + step, step):
        draw.line([(0, y), (w, y)], fill=line, width=width)
    out = image.copy()
    out.alpha_composite(layer)
    return out


def vignette(image: Image.Image, strength: float = 0.35) -> Image.Image:
    """Darken the rim so a centred subject holds the eye."""
    if strength <= 0:
        return image
    w, h = image.size
    span = int(math.ceil(math.hypot(w, h)))
    # Inverting the blob mask gives transparent at the centre and fully opaque
    # past the inscribed circle, which is the shape of a vignette's alpha. Built
    # on the same oversized square as `linear_gradient` so the card's own
    # corners land outside the circle and darken fully.
    mask = _radial_mask(span).point(lambda v: _clamp8((255 - v) * max(0.0, min(1.0, strength))))
    left, top = (span - w) // 2, (span - h) // 2
    mask = mask.crop((left, top, left + w, top + h))
    shade = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    shade.putalpha(mask)
    out = image.copy()
    out.alpha_composite(shade)
    return out


def noise_overlay(image: Image.Image, amount: float = 0.03, seed: int = 7) -> Image.Image:
    """A little film grain, which stops a wide gradient from banding."""
    if amount <= 0:
        return image
    import random

    w, h = image.size
    rng = random.Random(seed)
    # Generated small and scaled up: full-resolution per-pixel noise in Python
    # costs 756,000 calls per card for something nobody can see at 1:1.
    small = Image.new("L", (max(2, w // 4), max(2, h // 4)))
    small.putdata([rng.randint(0, 255) for _ in range(small.width * small.height)])
    grain = small.resize((w, h), Image.BILINEAR)
    grain = grain.point(lambda v: _clamp8(abs(v - 128) * max(0.0, min(1.0, amount))))
    layer = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    layer.putalpha(grain)
    out = image.copy()
    out.alpha_composite(layer)
    return out


# ── panels and marks ─────────────────────────────────────────────────────────

def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    """An `L` mask with rounded corners, for clipping a screenshot."""
    w, h = size
    radius = max(0, min(int(radius), min(w, h) // 2))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=radius, fill=255)
    return mask


def rounded_panel(image: Image.Image,
                  radius: int = 16,
                  border: int = 0,
                  border_color: Any = "#ffffff",
                  border_alpha: float = 0.16) -> Image.Image:
    """Clip an image to rounded corners, optionally with a hairline edge."""
    panel = image.convert("RGBA")
    mask = rounded_mask(panel.size, radius)
    out = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    out.paste(panel, (0, 0), mask)
    if border > 0:
        stroke = Image.new("RGBA", panel.size, (0, 0, 0, 0))
        ImageDraw.Draw(stroke).rounded_rectangle(
            [(0, 0), (panel.width - 1, panel.height - 1)],
            radius=radius, outline=with_alpha(parse_color(border_color), border_alpha),
            width=int(border),
        )
        out.alpha_composite(stroke)
    return out


def drop_shadow(image: Image.Image,
                blur: float = 24.0,
                offset: tuple[int, int] = (0, 18),
                color: Any = "#000000",
                alpha: float = 0.45,
                spread: int = 0) -> tuple[Image.Image, tuple[int, int]]:
    """A blurred silhouette of `image`, plus where to paste it relative to it.

    Returns `(shadow, (dx, dy))`. Paste the shadow at `(x + dx, y + dy)` where
    `(x, y)` is where the image itself goes, then paste the image on top. The
    offset is returned rather than baked in because the shadow canvas is larger
    than the image and the caller needs the delta to keep the two aligned.
    """
    pad = int(math.ceil(blur * 3)) + abs(offset[0]) + abs(offset[1]) + spread + 2
    w, h = image.size
    canvas = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))

    silhouette = image.getchannel("A")
    if spread:
        silhouette = silhouette.filter(ImageFilter.MaxFilter(_odd(spread * 2 + 1)))
    tint = Image.new("RGBA", (w, h), with_alpha(parse_color(color), alpha))
    tint.putalpha(silhouette.point(lambda v: _clamp8(v * max(0.0, min(1.0, alpha)))))

    canvas.alpha_composite(tint, (pad + offset[0], pad + offset[1]))
    if blur > 0:
        canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    return canvas, (-pad, -pad)


def _odd(n: int) -> int:
    n = max(1, int(n))
    return n if n % 2 else n + 1


def fit_contain(image: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """Scale to fit inside a box, keeping the aspect ratio. Never upscales past 4x."""
    w, h = image.size
    if w <= 0 or h <= 0:
        return image
    scale = min(max_w / w, max_h / h)
    scale = min(scale, 4.0)
    return image.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                        Image.LANCZOS)


def fit_cover(image: Image.Image, box_w: int, box_h: int,
              anchor: str = "top-center") -> Image.Image:
    """Scale to cover a box and crop the overflow.

    `anchor` decides which part survives the crop. A product screenshot wants
    `top-center` so the header and the first screenful stay, never the middle of
    a long page.
    """
    w, h = image.size
    if w <= 0 or h <= 0 or box_w <= 0 or box_h <= 0:
        return image
    scale = max(box_w / w, box_h / h)
    scaled = image.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                          Image.LANCZOS)
    vert, _, horiz = anchor.partition("-")
    extra_x = scaled.width - box_w
    extra_y = scaled.height - box_h
    x = {"left": 0, "center": extra_x // 2, "right": extra_x}.get(horiz or "center", extra_x // 2)
    y = {"top": 0, "center": extra_y // 2, "bottom": extra_y}.get(vert or "top", 0)
    return scaled.crop((x, y, x + box_w, y + box_h))


def mean_visible_color(image: Image.Image) -> RGBA:
    """Average colour of the pixels that are actually opaque.

    A logo is mostly transparent, so a plain average is dominated by whatever
    the empty area happens to hold and reports near-black for a white wordmark.
    Weighting by alpha is what makes "is this mark light or dark" answerable.
    """
    rgba = image.convert("RGBA")
    # Sampled small: the answer is one colour and a 400x100 mark is 40,000
    # pixels of Python loop for a number that does not change.
    small = rgba.resize((min(48, rgba.width), min(48, rgba.height)), Image.BILINEAR)
    total = [0.0, 0.0, 0.0]
    weight = 0.0
    for r, g, b, a in small.getdata():
        if not a:
            continue
        w = a / 255
        total[0] += r * w
        total[1] += g * w
        total[2] += b * w
        weight += w
    if weight <= 0:
        return (0, 0, 0, 0)
    return (_clamp8(total[0] / weight), _clamp8(total[1] / weight),
            _clamp8(total[2] / weight), 255)


def low_contrast_fraction(image: Image.Image, background: RGBA,
                          threshold: float = 1.7) -> float:
    """Share of a mark's opaque pixels that would be hard to see on `background`.

    The statistic that matters for "will this logo disappear", and the reason
    `mean_visible_color` is not it: a lockup of a solid purple glyph next to a
    white wordmark averages to a mid purple that passes against a white plate,
    while the half of the mark that carries the brand name is invisible. Asking
    how much of the mark is in trouble catches that; asking for its average
    colour does not.
    """
    rgba = image.convert("RGBA")
    small = rgba.resize((min(48, max(1, rgba.width)), min(48, max(1, rgba.height))),
                        Image.BILINEAR)
    lum_bg = relative_luminance(background)
    opaque = 0
    poor = 0
    for r, g, b, a in small.getdata():
        if a < 32:
            continue
        opaque += 1
        lum = relative_luminance((r, g, b, 255))
        hi, lo = max(lum, lum_bg), min(lum, lum_bg)
        if (hi + 0.05) / (lo + 0.05) < threshold:
            poor += 1
    return (poor / opaque) if opaque else 0.0


def region_color(image: Image.Image, box: tuple[int, int, int, int]) -> RGBA:
    """The average colour of one rectangle of a card, clamped to its bounds."""
    w, h = image.size
    left = max(0, min(int(box[0]), w - 1))
    top = max(0, min(int(box[1]), h - 1))
    right = max(left + 1, min(int(box[2]), w))
    bottom = max(top + 1, min(int(box[3]), h))
    patch = image.crop((left, top, right, bottom)).convert("RGB").resize((1, 1), Image.BILINEAR)
    return (*patch.getpixel((0, 0)), 255)


def contrast_chip(size: tuple[int, int], color: RGBA, radius: int = 12,
                  pad: int = 14) -> Image.Image:
    """A soft rounded plate to sit behind a mark that would otherwise vanish."""
    w = size[0] + pad * 2
    h = size[1] + pad * 2
    chip = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(chip).rounded_rectangle([(0, 0), (w - 1, h - 1)],
                                           radius=radius, fill=color)
    return chip


def paste_rgba(base: Image.Image, overlay: Image.Image, xy: tuple[int, int]) -> None:
    """Alpha-correct paste, in place. Clipped rather than raising at the edge."""
    base.alpha_composite(overlay.convert("RGBA"), (int(xy[0]), int(xy[1])))


# ── type ─────────────────────────────────────────────────────────────────────

def wrap_to_width(draw: ImageDraw.ImageDraw, text: str, font: Any, max_w: int) -> list[str]:
    """Greedy wrap, measured in pixels.

    Character counting is the trap here: in a proportional face "Illinois" and
    "WWWWWWWW" are the same length in characters and nothing like it in pixels,
    so a count-based wrap overflows on caps and wastes a third of the line on
    lower case.
    """
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    line = ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_text(text: str,
             font_loader,
             max_w: int,
             max_h: int,
             max_lines: int = 3,
             start_size: int = 96,
             min_size: int = 28,
             line_height: float = 1.12,
             step: int = 4) -> tuple[Any, list[str], int]:
    """Largest size at which `text` fits the box in at most `max_lines`.

    `font_loader` is a callable taking a pixel size and returning a font. The
    search walks down rather than binary-searching, because wrapping is not
    monotonic in a way a bisection can trust: one word crossing a boundary can
    remove a whole line and make a larger size fit where a smaller one did not.

    Returns `(font, lines, size)`. Falls back to `min_size` with the text
    clipped to `max_lines` rather than returning nothing, since a slightly
    cramped headline beats a blank card.
    """
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    size = max(min_size, int(start_size))
    best: tuple[Any, list[str], int] | None = None

    while size >= min_size:
        font = font_loader(size)
        lines = wrap_to_width(probe, text, font, max_w)
        if lines and len(lines) <= max_lines:
            block_h = int(round(len(lines) * size * line_height))
            if block_h <= max_h:
                best = (font, lines, size)
                break
        size -= step

    if best is None:
        font = font_loader(min_size)
        lines = wrap_to_width(probe, text, font, max_w)[:max_lines]
        best = (font, lines, min_size)
    return best


def text_block_size(lines: Sequence[str], font: Any, size: int,
                    line_height: float = 1.12) -> tuple[int, int]:
    """Pixel width and height of a wrapped block, for centring it."""
    if not lines:
        return (0, 0)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    width = int(max(probe.textlength(line, font=font) for line in lines))
    height = int(round(len(lines) * size * line_height))
    return (width, height)


def draw_lines(image: Image.Image,
               lines: Sequence[str],
               font: Any,
               size: int,
               xy: tuple[int, int],
               fill: Any = "#ffffff",
               line_height: float = 1.12,
               align: str = "left",
               box_w: int | None = None) -> int:
    """Paint a wrapped block and return the height it used.

    `xy` is the top-left of the block. With `align` other than left, `box_w` is
    the width the lines are aligned within.
    """
    if not lines:
        return 0
    draw = ImageDraw.Draw(image)
    # `gradient_text` reuses this to stencil into an `L` mask, and Pillow refuses
    # an RGBA tuple on a single-band image. Taking the mode from the target keeps
    # one wrapping-and-leading implementation for both callers.
    color = 255 if image.mode == "L" else parse_color(fill)
    step = size * line_height
    width = box_w if box_w is not None else int(max(draw.textlength(l, font=font) for l in lines))
    x0, y0 = xy
    for i, line in enumerate(lines):
        run = draw.textlength(line, font=font)
        if align == "center":
            x = x0 + (width - run) / 2
        elif align == "right":
            x = x0 + (width - run)
        else:
            x = x0
        # `anchor="la"` puts the baseline maths in Pillow's hands, which keeps
        # multi-line leading even across fonts with odd internal metrics.
        draw.text((x, y0 + i * step), line, font=font, fill=color, anchor="la")
    return int(round(len(lines) * step))


def gradient_text(image: Image.Image,
                  lines: Sequence[str],
                  font: Any,
                  size: int,
                  xy: tuple[int, int],
                  stops: Sequence[tuple[float, Any]],
                  angle: float = 90.0,
                  line_height: float = 1.12,
                  align: str = "left",
                  box_w: int | None = None) -> int:
    """Type filled with a gradient rather than a flat colour.

    Drawn by painting the text into a mask and using it to stencil a gradient,
    which is the only way to get a per-glyph ramp out of Pillow.
    """
    if not lines:
        return 0
    mask = Image.new("L", image.size, 0)
    used = draw_lines(mask, lines, font, size, xy, fill=(255, 255, 255, 255),
                      line_height=line_height, align=align, box_w=box_w)
    ramp = linear_gradient(image.size, stops, angle)
    ramp.putalpha(mask)
    image.alpha_composite(ramp)
    return used


def pill(image: Image.Image,
         text: str,
         font: Any,
         xy: tuple[int, int],
         fill: Any = "#ffffff",
         ink: Any = "#111318",
         pad: tuple[int, int] = (18, 10),
         radius: int = 999) -> tuple[int, int]:
    """A rounded label, as used for a CTA or a badge. Returns its size."""
    draw = ImageDraw.Draw(image)
    run = int(draw.textlength(text, font=font))
    try:
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
    except AttributeError:  # the bitmap default font has no metrics
        line_h = font.size if hasattr(font, "size") else 16
    w = run + pad[0] * 2
    h = line_h + pad[1] * 2
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [(0, 0), (w - 1, h - 1)], radius=min(radius, h // 2), fill=parse_color(fill))
    ImageDraw.Draw(layer).text((pad[0], pad[1]), text, font=font, fill=parse_color(ink), anchor="la")
    paste_rgba(image, layer, xy)
    return (w, h)


def stat_tiles(size: tuple[int, int],
               items: Sequence[dict[str, str]],
               value_font, label_font,
               fill: Any = "#16181d",
               ink: Any = "#ffffff",
               muted: Any = "#9aa0aa",
               radius: int = 18,
               gap: int = 14,
               columns: int = 2) -> Image.Image:
    """A grid of number-over-caption tiles.

    Each item is `{"value": "120k", "label": "followers on socials"}`.
    """
    w, h = size
    tiles = list(items)[: max(1, columns) * 4]
    if not tiles:
        return Image.new("RGBA", size, (0, 0, 0, 0))
    cols = max(1, min(columns, len(tiles)))
    rows = int(math.ceil(len(tiles) / cols))
    tile_w = (w - gap * (cols - 1)) // cols
    tile_h = (h - gap * (rows - 1)) // rows

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for i, item in enumerate(tiles):
        cx = (i % cols) * (tile_w + gap)
        cy = (i // cols) * (tile_h + gap)
        tile = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
        ImageDraw.Draw(tile).rounded_rectangle(
            [(0, 0), (tile_w - 1, tile_h - 1)], radius=radius, fill=parse_color(fill))
        d = ImageDraw.Draw(tile)
        value = str(item.get("value", ""))
        label = str(item.get("label", ""))
        d.text((tile_w / 2, tile_h * 0.40), value, font=value_font,
               fill=parse_color(ink), anchor="mm")
        d.text((tile_w / 2, tile_h * 0.72), label, font=label_font,
               fill=parse_color(muted), anchor="mm")
        paste_rgba(canvas, tile, (cx, cy))
    return canvas


def flatten(image: Image.Image, background: Any = "#000000") -> Image.Image:
    """RGBA to RGB over a backing colour, ready for JPEG."""
    base = Image.new("RGB", image.size, parse_color(background)[:3])
    base.paste(image, (0, 0), image.getchannel("A"))
    return base
