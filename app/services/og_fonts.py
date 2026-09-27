"""Real typefaces for Open Graph cards, including the app's own.

A social card is mostly type, so the face matters more than anything else this
pipeline does. Three sources, in order of how much they are worth:

1. **The app's own family**, read off its theme (`fontFamily`) or its installed
   font packs. A card set in the site's typeface looks like the site. This is
   the whole point and it is why the module goes to the trouble of fetching
   binaries rather than settling for whatever the host has.
2. **A curated fallback family** from Google Fonts, downloaded and cached on
   first use. Picked to be a workhorse geometric sans, because that is what the
   cards people ship are set in.
3. **Whatever the host happens to have installed**, which on a Linux container
   is usually DejaVu and on a Mac is Helvetica. Correct, ugly, and last.

There is no bundled font in this repo and none ships with Pillow, so without
1 or 2 a container with no font packages draws the headline in Pillow's bitmap
default: roughly eleven pixels tall, on a canvas six hundred and thirty pixels
tall. That failure is silent and looks like a bug in the layout, so
`resolve_family` reports it as a warning rather than letting it pass.

**Why a bare User-Agent.** `fonts.googleapis.com/css2` content-negotiates on the
UA and there are three outcomes, not two. A full modern browser string gets
woff2, which Pillow cannot open. An ancient MSIE string gets EOT, which Pillow
cannot open either, and which is easy to mistake for success because the URL
carries no extension to give it away. A bare `Mozilla/5.0` is the one that gets
plain TrueType with `format('truetype')` on it. Measured against Inter, not
assumed, and the downloaded bytes are checked against the sfnt magic numbers
before anything is cached, so a fourth negotiated format would be caught rather
than poisoning the cache.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GF_CSS = "https://fonts.googleapis.com/css2"

#: The UA that makes Google Fonts serve TrueType. See the module docstring:
#: this exact vagueness is the point. Adding browser details to it gets woff2
#: back, and replacing it with an MSIE string gets EOT.
_TTF_UA = "Mozilla/5.0"

#: sfnt magic numbers Pillow's FreeType can actually open. `wOF2` and the EOT
#: header are deliberately absent: those are the two formats this endpoint will
#: hand over if the negotiation goes wrong, and both fail at `truetype()` time
#: with an unhelpful error rather than at download time.
_SFNT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO")

#: Downloaded faces live here between runs. A card render should not pay for a
#: font fetch every time, and the same family is wanted over and over.
_CACHE_DIR = Path(os.environ.get("OG_FONT_CACHE_DIR")
                  or (Path(tempfile.gettempdir()) / "modlix-og-fonts"))

#: What we ask for when the app has no opinion. Inter is the de facto face of
#: this whole category of card and reads cleanly at both 96px and 22px.
DEFAULT_FAMILY = "Inter"

#: Weights a card needs: one for the headline, one for everything else.
DISPLAY_WEIGHT = 700
BODY_WEIGHT = 400

#: Families Google does not host under the name a theme is likely to use.
#: Mapping them keeps a perfectly good theme value from failing the fetch.
_FAMILY_ALIASES: dict[str, str] = {
    "helvetica": "Inter",
    "helvetica neue": "Inter",
    "arial": "Inter",
    "system-ui": "Inter",
    "-apple-system": "Inter",
    "segoe ui": "Inter",
    "sans-serif": "Inter",
    "serif": "Source Serif 4",
    "georgia": "Source Serif 4",
    "times new roman": "Source Serif 4",
    "monospace": "JetBrains Mono",
    "courier new": "JetBrains Mono",
}

#: Host paths, the last resort. Bold first: a card headline is never Regular.
_SYSTEM_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
)


class FontSet:
    """The faces one card is drawn with, loadable at any size.

    Holds paths rather than loaded fonts because Pillow wants a fresh
    `truetype()` per size, and a card asks for six or seven different sizes.
    """

    __slots__ = ("display_path", "body_path", "family", "warnings")

    def __init__(self, display_path: str | None, body_path: str | None,
                 family: str, warnings: list[str] | None = None) -> None:
        self.display_path = display_path
        self.body_path = body_path or display_path
        self.family = family
        self.warnings = warnings or []

    def display(self, size: int):
        """The headline face at `size` pixels."""
        return _load(self.display_path, size)

    def body(self, size: int):
        """The supporting face at `size` pixels."""
        return _load(self.body_path, size)

    def loader(self, weight: str = "display"):
        """A `size -> font` callable, which is what `og_paint.fit_text` takes."""
        return self.display if weight == "display" else self.body

    @property
    def is_real(self) -> bool:
        """False when both faces fell through to Pillow's bitmap default."""
        return bool(self.display_path)

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"FontSet(family={self.family!r}, display={self.display_path!r})"


def _load(path: str | None, size: int):
    from PIL import ImageFont

    if path:
        try:
            return ImageFont.truetype(path, max(1, int(size)))
        except Exception:  # noqa: BLE001 - a font that will not load is not fatal
            logger.warning("font failed to load, falling back: %s", path, exc_info=True)
    return ImageFont.load_default()


# ── finding a family ─────────────────────────────────────────────────────────

def normalise_family(value: str | None) -> str:
    """Take the first family out of a CSS font stack and tidy it.

    A theme's `fontFamily` is a CSS value, so it arrives as
    `'Inter', "Helvetica Neue", sans-serif` and the first entry is the one that
    was chosen. Everything after it is the author's fallback chain, not a
    preference, so taking a later one would quietly pick the thing they were
    trying to avoid.
    """
    if not value:
        return ""
    text = str(value)
    # A Modlix theme keeps both a family and a set of CSS font shorthands, and
    # the shorthands reference the family rather than repeating it:
    # `primaryFont = 14px/14px <fontFamily>`. Reading one of those as a family
    # name asks Google Fonts for "14px/14px <fontFamily>", which 400s. The
    # angle bracket and the size are each enough to tell them apart.
    if "<" in text or re.search(r"\d\s*px", text):
        return ""
    first = text.split(",")[0].strip().strip("'\"").strip()
    if not first:
        return ""
    alias = _FAMILY_ALIASES.get(first.lower())
    return alias or first


def family_from_brand(look: dict[str, Any] | None) -> str:
    """Pick a family out of a harvested `look` map, or "" if it says nothing."""
    if not isinstance(look, dict):
        return ""
    # `fontFamily` is the app-level token; the per-slot ones are what a theme
    # sets when it distinguishes headings from body.
    for key in ("fontFamily", "primaryFont", "headingFont", "displayFont",
                "titleFont", "secondaryFont", "bodyFont"):
        found = normalise_family(look.get(key))
        if found:
            return found
    for key, value in look.items():
        if "font" in str(key).lower():
            found = normalise_family(value)
            if found:
                return found
    return ""


# ── fetching ─────────────────────────────────────────────────────────────────

def _cache_path(family: str, weight: int) -> Path:
    stamp = hashlib.sha1(f"{family}:{weight}".encode()).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-") or "font"
    return _CACHE_DIR / f"{slug}-{weight}-{stamp}.ttf"


async def fetch_google_font(family: str, weight: int = DISPLAY_WEIGHT,
                            timeout: float = 10.0) -> tuple[str | None, str]:
    """Download one weight of a Google family as TrueType, cached on disk.

    Returns `(path, warning)`. Never raises: a card set in a fallback face is a
    worse card, not a failed one.
    """
    import httpx

    family = (family or "").strip()
    if not family:
        return None, ""

    cached = _cache_path(family, weight)
    if cached.exists() and cached.stat().st_size > 1024:
        return str(cached), ""

    query = f"{_GF_CSS}?family={family.replace(' ', '+')}:wght@{weight}&display=swap"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            css = await client.get(query, headers={"User-Agent": _TTF_UA})
            if css.status_code == 400:
                # Google answers 400 for a family it does not host, which is the
                # common case for a theme naming a licensed or local face.
                return None, f"{family} is not a Google font, so the card uses a fallback face"
            if css.status_code >= 300:
                return None, f"could not look up the font {family} (HTTP {css.status_code})"

            # Prefer a src Google itself labelled truetype; fall back to any
            # `.ttf` URL. Both are checked by magic number below regardless,
            # because the label is Google's claim and the bytes are the fact.
            match = (re.search(r"src:\s*url\((https://[^)]+?)\)\s*format\('truetype'\)", css.text)
                     or re.search(r"src:\s*url\((https://[^)]+?\.ttf)\)", css.text))
            if not match:
                return None, (f"{family} is not available as TrueType, so the card "
                              "uses a fallback face")

            binary = await client.get(match.group(1), headers={"User-Agent": _TTF_UA})
            if binary.status_code >= 300 or len(binary.content) < 1024:
                return None, f"could not download the font {family}"
            if not binary.content.startswith(_SFNT_MAGIC):
                # woff2 or EOT arrived despite the UA. Caching it would turn one
                # bad negotiation into a permanently broken family, since every
                # later run would find the file and trust it.
                logger.warning("google font %s came back as %r, not an sfnt",
                               family, binary.content[:4])
                return None, (f"{family} came back in a format Pillow cannot read, "
                              "so the card uses a fallback face")
    except Exception as e:  # noqa: BLE001
        logger.warning("google font fetch failed for %s", family, exc_info=True)
        return None, f"could not fetch the font {family} ({type(e).__name__})"

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Written beside then renamed, so a torn download never becomes a cache
        # entry that every later run trusts and fails to open.
        temp = cached.with_suffix(".part")
        temp.write_bytes(binary.content)
        temp.replace(cached)
    except OSError as e:
        logger.warning("could not cache font %s: %s", family, e)
        return None, ""

    return str(cached), ""


def system_font() -> tuple[str | None, str]:
    """The best face already on this host, or `(None, why)`."""
    for pattern in _SYSTEM_CANDIDATES:
        for found in sorted(glob.glob(pattern)):
            return found, ""
    return None, ("no scalable font is installed on this host and none could be "
                  "downloaded, so text is drawn in a tiny bitmap face; install "
                  "fonts-dejavu-core in the image")


async def resolve_family(family: str = "", *, allow_network: bool = True) -> FontSet:
    """Everything a card needs to set type, with the best face available.

    Tries the requested family, then the default family, then the host. Each
    step that does not work adds a warning, so the caller can tell the user why
    their card is not in their typeface rather than leaving them to wonder.
    """
    warnings: list[str] = []
    wanted = normalise_family(family) or DEFAULT_FAMILY

    if allow_network:
        display, warn = await fetch_google_font(wanted, DISPLAY_WEIGHT)
        if warn:
            warnings.append(warn)
        body, _ = await fetch_google_font(wanted, BODY_WEIGHT) if display else (None, "")

        if not display and wanted != DEFAULT_FAMILY:
            display, warn2 = await fetch_google_font(DEFAULT_FAMILY, DISPLAY_WEIGHT)
            if display:
                body, _ = await fetch_google_font(DEFAULT_FAMILY, BODY_WEIGHT)
                wanted = DEFAULT_FAMILY
            elif warn2:
                warnings.append(warn2)

        if display:
            return FontSet(display, body, wanted, warnings)

    path, warn = system_font()
    if warn:
        warnings.append(warn)
    return FontSet(path, path, "system", warnings)
