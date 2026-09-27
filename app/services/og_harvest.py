"""Collect an app's own brand material, so a card can be assembled from it.

Nothing here invents anything. Every ingredient a social card needs already
exists inside the platform for the app being edited:

    the mark        `properties.links[rel*=icon]` on the Application document
    the palette     the app's theme variables
    the typeface    the theme's `fontFamily`
    the words       the og fields, or `properties.title`
    the product     a headless screenshot of the app's own page

That last one is what makes a reference card look expensive, and it is free
here because the platform can already render its own pages.

**SVG is not refused.** Pillow cannot open one and this repo has no rasteriser,
which previously meant telling people to go and export a PNG. But a headless
Chromium is already pooled in this process for screenshots, and a browser is an
excellent SVG rasteriser. So an SVG mark is loaded into a page and shot with a
transparent background, at a size chosen for the card rather than whatever the
file happens to declare. The result is sharper than a supplied PNG, because it
is rendered at the size it will be used.

Everything degrades rather than failing. A harvest with no logo, no screenshot
and no palette still produces a usable `SiteFacts`, and the layouts that need
material they did not get are simply not offered.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

#: Nothing harvested is allowed to hold the whole render up. A card is worth
#: shipping without a screenshot; it is not worth a sixty-second wait.
LOGO_TIMEOUT = 12.0
SHOT_TIMEOUT = 30.0

#: The mark is rasterised at this width and scaled down by the layouts. Cards
#: place a logo at roughly 400px at the widest, so this leaves headroom for a
#: retina-grade downscale without paying for a 2000px render.
LOGO_RASTER_W = 720

#: What a page is shot at. A desktop viewport, because the panel in every
#: layout is landscape and a mobile shot letterboxes into it.
SHOT_W = 1440
SHOT_H = 900

#: Theme tokens worth reading, in the order a palette should prefer them.
#: `colorOne` is the app-level primary; the `color0..N` block is what the
#: builder writes for a generated site; `customColor*` is hand-authored.
_PALETTE_KEYS = (
    "colorOne", "colorTwo", "colorThree", "colorFour", "colorFive",
    "color0", "color1", "color2", "color3", "color4", "color5",
    "customColor0", "customColor1", "customColor2", "customColor3",
    "gradientColorOne", "gradientColorTwo", "gradientColorThree",
)

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

#: Greys and near-blacks make a dull card on their own, so they are separated
#: out rather than dropped: they are the right choice for a plate and the wrong
#: choice for an accent.
_CHROMA_FLOOR = 28


@dataclass
class SiteFacts:
    """What was found. Every field is optional, because any of it can be absent."""

    app_code: str = ""
    client_code: str = ""
    title: str = ""
    description: str = ""
    domain: str = ""
    #: Brand colours, most important first, greys removed.
    accents: list[str] = field(default_factory=list)
    #: Dark and light plate candidates pulled from the same palette.
    neutrals: list[str] = field(default_factory=list)
    font_family: str = ""
    logo: Image.Image | None = None
    logo_url: str = ""
    shot: Image.Image | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def accent(self) -> str:
        return self.accents[0] if self.accents else "#7c3aed"

    def note(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)


# ── colour helpers ───────────────────────────────────────────────────────────

def _chroma(hex_value: str) -> int:
    """Max minus min channel: 0 for any grey, high for a saturated colour."""
    from app.services.og_paint import parse_color

    r, g, b, _ = parse_color(hex_value)
    return max(r, g, b) - min(r, g, b)


def palette_from_theme(variables: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Split a theme's tokens into accents and neutrals.

    Returns `(accents, neutrals)`. A theme holds a couple of thousand variables
    and most are component-level; only the app-level brand tokens are read, in
    a fixed order, so two runs against the same theme choose the same colours.
    """
    if not isinstance(variables, dict):
        return [], []
    # Theme variables are stored per breakpoint. `ALL` is the base; the others
    # are overrides for a screen size a card does not have.
    tokens = variables.get("ALL") if isinstance(variables.get("ALL"), dict) else variables

    accents: list[str] = []
    neutrals: list[str] = []
    for key in _PALETTE_KEYS:
        value = tokens.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not _HEX.match(value):
            continue
        value = value.lower()
        bucket = accents if _chroma(value) >= _CHROMA_FLOOR else neutrals
        if value not in bucket:
            bucket.append(value)
    return accents, neutrals


def _first_icon_href(properties: dict[str, Any]) -> str:
    """The favicon href off an Application document, or "".

    `properties.links` is a keyed map, not a list, and the entries carry a
    declared `type` that is routinely wrong: sitezump's favicon is registered
    as `image/png` and is an SVG. So the href is what gets trusted and the
    declared type is ignored entirely.
    """
    links = properties.get("links")
    if isinstance(links, dict):
        entries = list(links.values())
    elif isinstance(links, list):
        entries = links
    else:
        return ""

    best = ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("rel") or "").lower()
        href = str(entry.get("href") or "").strip()
        if not href or "icon" not in rel:
            continue
        # An apple-touch-icon is a square raster at a decent size, which is a
        # better mark than a 32px favicon when both are present.
        if "apple" in rel:
            return href
        best = best or href
    return best


def absolutise(href: str, gateway: str) -> str:
    """A stored href to something fetchable. Already-absolute values pass through."""
    href = (href or "").strip()
    if not href or href.startswith(("http://", "https://", "data:")):
        return href
    return f"{gateway.rstrip('/')}/{href.lstrip('/')}"


# ── rasterising ──────────────────────────────────────────────────────────────

def _looks_like_svg(payload: bytes, content_type: str = "") -> bool:
    if "svg" in (content_type or "").lower():
        return True
    head = payload[:400].lstrip()
    return head.startswith(b"<?xml") or head.startswith(b"<svg")


async def rasterise_svg(payload: bytes, width: int = LOGO_RASTER_W,
                        timeout: float = LOGO_TIMEOUT) -> tuple[Image.Image | None, str]:
    """An SVG to a transparent PNG, rendered by the pooled headless browser.

    Returns `(image, warning)`. This is the piece that removes the "export your
    logo as a PNG" instruction: a browser already sits in this process for
    screenshots and renders SVG better than any Python rasteriser would.

    The SVG is sized by CSS rather than trusted to size itself. Plenty of marks
    carry only a `viewBox`, some declare a width in millimetres, and Chromium
    falls back to 300x150 for the rest, which would hand the card a squashed
    mark that looks like a layout bug.
    """
    from app.services.browser_pool import BrowserUnavailable, browser_context

    encoded = base64.b64encode(payload).decode("ascii")
    html = (
        "<!doctype html><html><body style=\"margin:0;background:transparent\">"
        f"<img id=\"m\" src=\"data:image/svg+xml;base64,{encoded}\" "
        f"style=\"display:block;width:{width}px;height:auto\">"
        "</body></html>"
    )
    try:
        async with browser_context("external", viewport={"width": width, "height": width}) as ctx:
            page = await ctx.new_page()
            await page.set_content(html, wait_until="load", timeout=int(timeout * 1000))
            # A data-URL image can be decoded a tick after `load`; without this
            # the element screenshot is occasionally an empty box.
            await page.wait_for_function(
                "() => { const i = document.getElementById('m');"
                " return i && i.complete && i.naturalWidth > 0; }",
                timeout=int(timeout * 1000))
            shot = await page.locator("#m").screenshot(omit_background=True, type="png")
            await page.close()
    except BrowserUnavailable as e:
        return None, f"could not rasterise the SVG logo, no browser available ({e})"
    except Exception as e:  # noqa: BLE001
        logger.warning("svg rasterisation failed", exc_info=True)
        return None, f"could not rasterise the SVG logo ({type(e).__name__})"

    try:
        image = Image.open(_buffer(shot)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None, "the rasterised logo could not be read back"
    return trim_transparent(image), ""


def _buffer(payload: bytes):
    import io
    return io.BytesIO(payload)


def trim_transparent(image: Image.Image, padding: int = 0) -> Image.Image:
    """Crop to the mark's own bounds.

    A logo file is usually padded, sometimes heavily, and that padding is what
    a layout would place rather than the mark. Trimming makes "put the logo at
    44px tall" mean the mark is 44px tall.
    """
    rgba = image.convert("RGBA")
    box = rgba.getchannel("A").getbbox()
    if not box:
        return rgba
    if padding:
        box = (max(0, box[0] - padding), max(0, box[1] - padding),
               min(rgba.width, box[2] + padding), min(rgba.height, box[3] + padding))
    return rgba.crop(box)


def is_own_gateway(url: str) -> bool:
    """Whether a URL points at this deployment's own platform.

    The distinction matters because the SSRF guard exists for URLs a *caller*
    supplied, and an app's own favicon href resolved against our own configured
    gateway is not one of those. On every deployment where the gateway is
    internal - which locally means `http://localhost:8080` - the guard refuses
    the platform's own asset as a loopback address. It is right to, by its own
    rules; the mistake is sending an internal fetch through it at all. Without
    this split, no app's logo is ever harvested anywhere but production.
    """
    from urllib.parse import urlparse

    gateway = _gateway()
    if not url or not gateway:
        return False
    try:
        return urlparse(url).netloc.lower() == urlparse(gateway).netloc.lower()
    except ValueError:
        return False


async def _fetch_internal(url: str, timeout: float) -> tuple[bytes, str]:
    """A plain GET against our own platform. Returns `(payload, content_type)`."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 verify=False) as client:  # noqa: S501 - local TLS
        response = await client.get(url)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "")


async def load_logo(url: str, timeout: float = LOGO_TIMEOUT) -> tuple[Image.Image | None, str]:
    """Fetch a mark from a URL and return it as trimmed RGBA, SVG included.

    A URL on our own gateway is fetched directly; anything else goes through
    the SSRF guard, because anything else came from a person.
    """
    from app.agents.appbuilder.tools.modlix._safe_fetch import BlockedURL, fetch_public_url

    if not url:
        return None, ""
    try:
        if is_own_gateway(url):
            payload, content_type = await _fetch_internal(url, timeout)
        else:
            asset = await fetch_public_url(url, timeout=timeout)
            payload, content_type = asset.content, asset.content_type
    except BlockedURL as e:
        return None, f"the logo URL was refused ({e})"
    except Exception as e:  # noqa: BLE001
        logger.warning("logo fetch failed for %s", url, exc_info=True)
        return None, f"could not fetch the logo ({type(e).__name__})"

    if not payload:
        return None, "the logo URL returned nothing"
    if _looks_like_svg(payload, content_type):
        return await rasterise_svg(payload, LOGO_RASTER_W, timeout)

    try:
        return trim_transparent(Image.open(_buffer(payload)).convert("RGBA")), ""
    except Exception:  # noqa: BLE001
        return None, "the logo file could not be read as an image"


#: Below this many distinct colours, a screenshot is a blank plate rather than
#: a page. Measured: an app that has not painted yet returns exactly 1, and a
#: genuinely minimal page still runs to thousands once type is antialiased.
_BLANK_COLOUR_FLOOR = 24

#: Waits to try, in order. A Modlix page fetches its definition, then its data,
#: then hydrates, and an SSO bounce can blank the first load entirely, so one
#: short wait is not evidence the page is empty.
_SHOT_WAITS = (2500, 6000)


def is_blank(image: Image.Image) -> bool:
    """Whether a screenshot is an empty plate.

    Worth its own check because the screenshot path reports success for a page
    that rendered nothing: a card then gets a pure white rectangle pasted into
    its product panel, which reads as a broken layout rather than as a missing
    screenshot. Measured against a real run: a blank sitezump home page came
    back 1440x900, no error, and exactly one distinct colour.
    """
    # Downsampled first: the question is "does this page have content", and a
    # full-resolution set of 1.3M pixels answers it no better than 40,000 do.
    sample = image.convert("RGB").resize((200, 200), Image.BILINEAR)
    return len(set(sample.getdata())) < _BLANK_COLOUR_FLOOR


async def screenshot_app_page(app_code: str, client_code: str, page_name: str,
                              headers: dict[str, str] | None = None,
                              timeout: float = SHOT_TIMEOUT
                              ) -> tuple[Image.Image | None, str]:
    """A picture of the app's own page, for the product panel in a split layout.

    Shot anonymously: a card is a public artefact, so what belongs in it is what
    a visitor would see, never a page rendered with the builder's session.

    Retried at a longer wait when the first attempt comes back blank, then
    refused outright rather than handed on. A blank panel on a card is worse
    than no panel, because the layout that would have been chosen instead is
    one that never needed a screenshot.
    """
    from app.agents.appbuilder.tools.modlix.clone_ops import _screenshot_modlix_page

    last_error = ""
    for attempt, wait_ms in enumerate(_SHOT_WAITS):
        try:
            payload, error = await asyncio.wait_for(
                _screenshot_modlix_page(page_name=page_name, ac=app_code, cc=client_code,
                                        width=SHOT_W, height=SHOT_H, wait_ms=wait_ms,
                                        headers=headers or {}),
                timeout=timeout)
        except asyncio.TimeoutError:
            return None, f"the site screenshot timed out after {int(timeout)}s"
        except Exception as e:  # noqa: BLE001
            logger.warning("site screenshot failed for %s/%s", app_code, page_name,
                           exc_info=True)
            return None, f"could not screenshot the site ({type(e).__name__})"

        if error or not payload:
            last_error = error or "no image came back"
            continue
        try:
            image = Image.open(_buffer(payload)).convert("RGBA")
        except Exception:  # noqa: BLE001
            last_error = "the file was not readable as an image"
            continue

        if not is_blank(image):
            return image, ""
        last_error = "the page rendered blank"
        logger.info("og harvest: %s/%s blank at %dms (attempt %d)",
                    app_code, page_name, wait_ms, attempt + 1)

    return None, (f"the site screenshot was not usable ({last_error}), so the card "
                  "is being made without a picture of the product")


# ── the harvest ──────────────────────────────────────────────────────────────

async def harvest(app_code: str, client_code: str, headers: dict[str, str], *,
                  page_name: str = "", title: str = "", description: str = "",
                  domain: str = "", want_shot: bool = True) -> SiteFacts:
    """Everything a card can be built from, gathered concurrently.

    `title`, `description` and `domain` are what the og pane already holds, and
    they win over anything discovered: the user typed them for this purpose.
    """
    facts = SiteFacts(app_code=app_code, client_code=client_code,
                      title=title, description=description, domain=domain)

    definition = await _read_application(app_code, client_code, headers, facts)
    properties = (definition or {}).get("properties") or {}

    if not facts.title:
        facts.title = str(properties.get("title") or app_code)

    gateway = _gateway()
    facts.logo_url = absolutise(_first_icon_href(properties), gateway)
    shot_page = page_name or str(properties.get("defaultPage") or "") or "home"

    theme_task = asyncio.create_task(_read_theme(app_code, client_code, headers, facts))
    logo_task = asyncio.create_task(load_logo(facts.logo_url))
    shot_task = (asyncio.create_task(
        screenshot_app_page(app_code, client_code, shot_page, headers))
        if want_shot else None)

    variables = await theme_task
    facts.accents, facts.neutrals = palette_from_theme(variables)
    if not facts.accents:
        facts.note("this app's theme has no brand colour set, so the card uses a default palette")

    from app.services.og_fonts import family_from_brand
    tokens = variables.get("ALL") if isinstance(variables.get("ALL"), dict) else variables
    facts.font_family = family_from_brand(tokens if isinstance(tokens, dict) else {})

    facts.logo, warning = await logo_task
    facts.note(warning)
    if facts.logo is None and facts.logo_url:
        facts.note("the card is being made without the site's logo")

    if shot_task is not None:
        facts.shot, warning = await shot_task
        facts.note(warning)

    return facts


def _gateway() -> str:
    from app.config import settings
    return (getattr(settings, "PREVIEW_HOST", "") or settings.GATEWAY_URL or "").rstrip("/")


async def _read_application(app_code: str, client_code: str, headers: dict[str, str],
                            facts: SiteFacts) -> dict[str, Any] | None:
    """The Application document, with `properties` intact.

    Read through the blueprint object reader rather than the listing route: the
    listing strips `properties`, which is where every brand fact lives, and a
    harvest against a stripped row silently reports an app with no logo, no
    title and no links.
    """
    from app.services.blueprint.objects import BlueprintObjectError, read_object

    try:
        return await read_object("application", app_code, app_code, headers)
    except BlueprintObjectError as e:
        facts.note(f"could not read the app definition ({e})")
    except Exception as e:  # noqa: BLE001
        logger.warning("application read failed for %s", app_code, exc_info=True)
        facts.note(f"could not read the app definition ({type(e).__name__})")
    return None


async def _read_theme(app_code: str, client_code: str, headers: dict[str, str],
                      facts: SiteFacts) -> dict[str, Any]:
    """The app's theme variables, or an empty map."""
    from app.services.blueprint.compose import _first_theme

    try:
        theme = await _first_theme(app_code, headers)
    except Exception as e:  # noqa: BLE001
        logger.warning("theme read failed for %s", app_code, exc_info=True)
        facts.note(f"could not read the app theme ({type(e).__name__})")
        return {}
    if not theme:
        return {}
    variables = theme.get("variables")
    return variables if isinstance(variables, dict) else {}
