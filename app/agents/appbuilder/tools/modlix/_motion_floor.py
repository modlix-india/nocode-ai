"""Default motion for a generated site.

A site the agent builds ships with no animation at all: nothing transitions on
hover, nothing moves on scroll, and it reads as a static mock rather than a
built page.

What NOT to do is already on record. `appbuilderstyle` carries, as its entire
body, `* { transition: width 1s, height 1s, padding... }`. Everything on every
page then animates any width change over a full second, so the page visibly
sloshes sideways whenever a scrollbar appears. The lessons taken from it here:

- never `*`, and never geometry (width/height/padding) -- only paint properties
  (colour, shadow, opacity) plus `transform`, which are composited and cheap;
- durations in the 150-250ms band, not seconds;
- honour `prefers-reduced-motion`, which that rule never did.

The reveal animation is opt-in by class (`_revealUp` and friends). It is offered
rather than applied because attaching it is a per-section judgement -- a hero
that animates in on every load gets tiring fast.
"""

from __future__ import annotations

import re

MOTION_MARKER = "modlix-motion-floor"

# Property lists are explicit. `transition: all` would drag geometry back in and
# reintroduce the sloshing this block exists to avoid.
DEFAULT_MOTION_CSS = f"""
/* {MOTION_MARKER}: baseline motion. Paint + transform only, never geometry. */
a, button, ._button, ._link,
.comp.compButton, .comp.compLink, .comp.compDropdown, .comp.compTextBox,
.comp.compTextArea, .comp.compCheckBox, .comp.compRadioButton, .comp.compToggleButton {{
    transition: background-color 180ms ease, color 180ms ease,
                border-color 180ms ease, box-shadow 180ms ease,
                opacity 180ms ease, transform 180ms ease;
}}

/* Cards and tiles lift slightly on hover. Transform only: no reflow. */
._card:hover, ._tile:hover, ._hoverLift:hover {{
    transform: translateY(-2px);
}}

/* Images inside a clipping frame scale on hover without moving their box. */
._zoomOnHover img {{
    transition: transform 280ms ease;
}}
._zoomOnHover:hover img {{
    transform: scale(1.04);
}}

@keyframes _revealUp {{
    from {{ opacity: 0; transform: translateY(16px); }}
    to   {{ opacity: 1; transform: none; }}
}}
@keyframes _revealFade {{
    from {{ opacity: 0; }}
    to   {{ opacity: 1; }}
}}

/* Opt-in per section. Attach `_revealUp` to a Grid to have it rise in once. */
._revealUp {{ animation: _revealUp 520ms ease both; }}
._revealFade {{ animation: _revealFade 520ms ease both; }}
._revealDelay1 {{ animation-delay: 80ms; }}
._revealDelay2 {{ animation-delay: 160ms; }}
._revealDelay3 {{ animation-delay: 240ms; }}

/* Anything the page scrolls to should ease, not jump. */
html {{ scroll-behavior: smooth; }}

@media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
    }}
}}
""".strip()

# Does the author's CSS already say something about motion? If so, leave it be:
# a floor is for the case where nobody decided, not a house style to impose.
_HAS_MOTION = re.compile(r"\b(transition|animation|@keyframes)\b", re.IGNORECASE)


def needs_motion(css: str) -> bool:
    """True when this stylesheet says nothing about motion at all."""
    if not css or not css.strip():
        return True
    if MOTION_MARKER in css:
        return False
    return not _HAS_MOTION.search(css)


def with_motion_floor(css: str) -> tuple[str, bool]:
    """Append the baseline motion block if the CSS declares no motion.

    Returns (css, added). Appends rather than prepends so that anything the
    author wrote keeps its place in the cascade and still wins on a tie.
    """
    if not needs_motion(css):
        return css, False
    base = (css or "").rstrip()
    joined = f"{base}\n\n{DEFAULT_MOTION_CSS}\n" if base else f"{DEFAULT_MOTION_CSS}\n"
    return joined, True
