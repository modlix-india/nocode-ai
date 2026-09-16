"""A floor under the typography of every generated site.

The problem, seen on every site the agent has built: they all render in the
platform's stock face. `create_app` bootstraps `properties.fontPacks` to `{}`
(the shell crashes if the key is missing), and nothing ever puts a font in it,
so no webfont is ever loaded and every theme's font tokens resolve to the
default. A site can look carefully designed in every other respect and still
read as a template because of it.

Two separate things have to line up, and the agent has reliably done neither:

1. **The pack** -- `app.properties.fontPacks`, a UUID-keyed map of
   `{name, code}` where `code` is literal HTML injected into the page head
   (`src/App/App.tsx:processFontPacks`). This is what actually LOADS the font.
   A malformed entry crashes the runtime with `undefined.trim()`.

2. **The tokens** -- `bodyFont` / `primaryFont` / ... `senaryFont` in the theme.
   These are `cp: 'font'`, the CSS `font` SHORTHAND, so a family-only value like
   `"Inter, sans-serif"` is invalid and gets dropped on the floor. Real themes
   write `"14px/16px 'Inter'"`. Six named slots exist precisely so headings,
   body and accents can differ.

Set the tokens without the pack and you get a font-family nobody downloaded.
Set the pack without the tokens and you download a font nothing uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

# Google's CSS2 endpoint. `display=swap` so text paints in a fallback instead of
# staying invisible while the font downloads.
_GF_BASE = "https://fonts.googleapis.com/css2"

# The six font slots a theme exposes, in the platform's own order.
FONT_SLOTS = (
    "primaryFont", "secondaryFont", "tertiaryFont",
    "quaternaryFont", "quinaryFont", "senaryFont",
)


@dataclass(frozen=True)
class Pairing:
    """A display face for headings and a text face for body copy."""

    key: str
    display: str
    body: str
    # Weights to request. Kept tight: every extra weight is another download.
    display_weights: tuple[int, ...] = (600, 700)
    body_weights: tuple[int, ...] = (400, 500, 600)
    note: str = ""
    display_fallback: str = "serif"
    body_fallback: str = "sans-serif"
    # Rough per-slot type scale, size/line-height. Deliberately modest: the
    # agent overrides these per design, this only has to be defensible.
    scale: dict[str, str] = field(default_factory=lambda: {
        "primaryFont": "40px/48px",
        "secondaryFont": "28px/36px",
        "tertiaryFont": "20px/28px",
        "quaternaryFont": "16px/24px",
        "quinaryFont": "14px/20px",
        "senaryFont": "12px/18px",
    })


# A small curated set rather than the whole Google catalogue. Each one is a
# pairing that is known to hold together, so the agent picks an intent instead of
# gambling on two family names. It may still name any Google family it likes --
# `pairing_from_families` builds a pack for anything.
PAIRINGS: dict[str, Pairing] = {
    "editorial": Pairing(
        key="editorial", display="Fraunces", body="Inter",
        note="Warm high-contrast serif over a neutral grotesque. Food, craft, retail.",
    ),
    "modern": Pairing(
        key="modern", display="Space Grotesk", body="Inter",
        display_fallback="sans-serif",
        note="Technical, slightly quirky. Software, hardware, anything engineering-adjacent.",
    ),
    "classic": Pairing(
        key="classic", display="Playfair Display", body="Source Sans 3",
        note="Formal, high contrast. Law, finance, luxury.",
    ),
    "friendly": Pairing(
        key="friendly", display="Poppins", body="Inter",
        display_fallback="sans-serif",
        note="Geometric and round. Consumer apps, education, childcare.",
    ),
    "neutral": Pairing(
        key="neutral", display="Inter", body="Inter",
        display_fallback="sans-serif",
        note="One family throughout. The safe choice when the brand has no voice yet.",
    ),
}

# Used only when the agent set no fonts at all. Neutral enough not to fight a
# design, real enough to beat the stock face.
DEFAULT_PAIRING = PAIRINGS["editorial"]


def _family_query(family: str, weights: tuple[int, ...]) -> str:
    """One `family=` term for the CSS2 endpoint."""
    fam = family.strip().replace(" ", "+")
    if not weights:
        return f"family={fam}"
    axis = ";".join(str(w) for w in sorted(set(weights)))
    return f"family={fam}:wght@{axis}"


def google_fonts_link(pairing: Pairing) -> str:
    """The literal HTML that loads this pairing.

    Preconnect first: the CSS and the font files come from two different hosts,
    and without it the browser pays a fresh handshake mid-render.
    """
    terms = [_family_query(pairing.display, pairing.display_weights)]
    if pairing.body.strip().lower() != pairing.display.strip().lower():
        terms.append(_family_query(pairing.body, pairing.body_weights))
    href = f"{_GF_BASE}?{'&'.join(terms)}&display=swap"
    return (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{href}">'
    )


def font_packs_for(pairing: Pairing) -> dict[str, dict[str, str]]:
    """A `properties.fontPacks` fragment for this pairing.

    One entry, not two: a single `<link>` loads both families, and two entries
    would mean two stylesheet requests for the same thing.
    """
    name = pairing.display if pairing.display == pairing.body else f"{pairing.display} + {pairing.body}"
    return {uuid.uuid4().hex: {"name": name, "code": google_fonts_link(pairing)}}


def pairing_from_families(display: str, body: str | None = None) -> Pairing:
    """Build a pairing for families the agent named itself."""
    d = (display or "").strip()
    b = (body or d).strip()
    return Pairing(key="custom", display=d, body=b or d)


def _shorthand(size_lh: str, family: str, fallback: str) -> str:
    """`cp: 'font'` is the CSS font SHORTHAND, which REQUIRES a size.

    A family-only value is invalid and the whole declaration is discarded, which
    is the quiet way a theme ends up on the stock face despite naming a font.
    """
    quoted = f"'{family}'" if " " in family else family
    return f"{size_lh} {quoted}, {fallback}"


def font_tokens_for(pairing: Pairing) -> dict[str, str]:
    """Theme variables for a pairing: body copy plus the six named slots."""
    out: dict[str, str] = {
        # bodyFont sets the page's base. Body face, at reading size.
        "bodyFont": _shorthand("16px/24px", pairing.body, pairing.body_fallback),
    }
    for slot in FONT_SLOTS:
        size_lh = pairing.scale.get(slot, "16px/24px")
        # The top two slots are headings and take the display face; the rest are
        # UI text and stay on the body face, so buttons and labels do not inherit
        # a display serif.
        is_heading = slot in ("primaryFont", "secondaryFont")
        family = pairing.display if is_heading else pairing.body
        fallback = pairing.display_fallback if is_heading else pairing.body_fallback
        out[slot] = _shorthand(size_lh, family, fallback)
    return out


def _has_any_font(all_vars: dict[str, Any]) -> bool:
    return any(all_vars.get(k) for k in ("bodyFont", *FONT_SLOTS))


def apply_font_floor(
    variables: Any, *, pairing: Pairing | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]] | None, list[str]]:
    """Give a theme real typography if it has none.

    Returns (variables, font_packs_to_register, notes). `font_packs_to_register`
    is None when the theme already named fonts -- in that case we must not guess
    which pack the agent meant, and the notes say so instead.

    Anything the agent set is kept. This only fills silence.
    """
    notes: list[str] = []
    if not isinstance(variables, dict):
        return {}, None, notes

    out = dict(variables)
    all_vars = out.get("ALL")
    if not isinstance(all_vars, dict):
        if "ALL" in out:
            # Malformed; leave it for the server to reject rather than destroy it.
            return out, None, notes
        all_vars = {}
        out["ALL"] = all_vars

    if _has_any_font(all_vars):
        # The agent chose. Check the shorthand is actually usable, because a
        # family-only value silently does nothing.
        for key in ("bodyFont", *FONT_SLOTS):
            val = all_vars.get(key)
            if isinstance(val, str) and val.strip() and "/" not in val and not val.startswith("<"):
                notes.append(
                    f"{key}={val!r} has no size: '{key}' is the CSS `font` SHORTHAND, "
                    f"so a family-only value is invalid and will be dropped. "
                    f"Write it as \"16px/24px 'Family', sans-serif\"."
                )
        notes.append(
            "Fonts were set explicitly; no pack was registered for them. A font "
            "token without a matching app.properties.fontPacks entry names a "
            "family the browser never downloads."
        )
        return out, None, notes

    chosen = pairing or DEFAULT_PAIRING
    all_vars.update(font_tokens_for(chosen))
    packs = font_packs_for(chosen)
    notes.append(
        f"No fonts were set, so the theme would have rendered in the platform's "
        f"stock face. Applied the '{chosen.key}' pairing "
        f"({chosen.display} for headings, {chosen.body} for text) and registered "
        f"the Google Fonts pack. Override either freely."
    )
    return out, packs, notes
