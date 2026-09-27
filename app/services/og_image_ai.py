"""Generate the 1200x630 plate that a shared link unfurls into.

Prompt in, image out. No reasoning loop and no tool choice, so this is a narrow
service with its own endpoint rather than a pass through the AppBuilder agent --
that agent spends roughly 44K tokens of fixed prefix before the first word of
the request, which would make the cheapest step in this flow the most expensive
one and put a tool-use loop around a call with nothing to call. Same reasoning
as `scene_ai`, `template_ai` and `version_diff`, and the same shape.

Two things here are not obvious and are load-bearing:

**Any reference image routes to Gemini.** MiniMax's image-01 is the better
renderer for a bare prompt because it honours `aspect_ratio` as a real field,
where Gemini ignores it and returns 1024x1024 every time (benched 5/5 against
0/5, see the comment block at `config.py:438`). But image-01's only reference
mode is `subject_reference` with `type: "character"`, purpose-built for keeping
a person's likeness across scenes. Handed a logo or a product shot it does
something confidently wrong. `visuals._render` only reroutes when there is more
than one input image, so the single-reference case is exactly the one that
misbehaves quietly. This module picks the provider itself instead.

**The filename carries a hash of the bytes.** Re-POSTing over a static path does
not change what is served: `override` is a `@RequestPart` that nothing sends as
one, and the download cache is keyed on a different path than the invalidation,
so the first version a host ever served is the version it keeps serving. A
content-stamped name sidesteps both, and LinkedIn's image cache with them.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# LinkedIn documents a 1200x627 minimum and recommends the 1.91:1 ratio; 630 is
# the conventional height that satisfies it. Everything downstream (the crop,
# the preview cards in the builders) is measured against these two numbers, so
# they live here and are imported, never retyped.
OG_WIDTH = 1200
OG_HEIGHT = 630

# WhatsApp falls back to its small-icon layout above roughly this, which turns a
# designed card into a thumbnail beside two lines of text.
OG_MAX_BYTES = 300_000

# image-01 has no 1.91:1 profile and rejects anything outside its own enum, so
# the render asks for the nearest ratio it does accept and the plate is cut from
# that. 16:9 is 1.78 against the target's 1.91, so the crop takes a little off
# the top and bottom and nothing off the sides.
RENDER_ASPECT = "16:9"

# Progressive quality ladder, the same approach `app/utils/image.py` uses for
# API payloads. Starts high because most cards are flat colour and type, which
# survives compression badly and is small to begin with.
QUALITY_STEPS = (92, 85, 78, 70, 60, 50)

_MIME_JPEG = "image/jpeg"

# Guards on the URL leg. The caller supplies these and the server fetches them,
# which is server-side request forgery surface: without the address check, any
# signed-in user could read the cloud metadata endpoint or anything else inside
# the network through this endpoint.
MAX_REFERENCE_IMAGES = 4
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
REFERENCE_FETCH_TIMEOUT = 10.0


class OgImageError(Exception):
    """A failure the caller should see verbatim. Raised for 400-shaped problems."""


# What the renderer is handed is a BRIEF, never the box's contents.
#
# A text-to-image model takes a description of a picture. People type a request:
# "Can you please generate a og image for https://sitezump.ai?" is a perfectly
# reasonable thing to write into a box labelled Prompt, and it is not a
# description of anything. Handed to image-01 verbatim it free-associates off
# the only concrete nouns it can see and returns, measurably, a crowd of anime
# characters. The URL is worse than useless: the model cannot open it, so it
# reads as decorative tokens.
#
# One cheap `fast`-tier call turns whatever was typed, plus what we already know
# about the site, into a concrete brief. The site name and description come from
# the document the pane is already editing, so a request naming a URL still
# produces a card about the right product.
_BRIEF_SYSTEM = """\
You write briefs for an image generator that is making one Open Graph social \
card: the picture someone sees when a link is shared in WhatsApp, LinkedIn, \
Slack or Teams.

You are given what a person typed into a prompt box, and what the site is. What \
they typed is often a REQUEST ("make me an og image for example.com"), not a \
description. Read their intent, then describe the picture.

Rules:
- Output the brief only. No preamble, no quotes, no markdown, no explanation.
- One paragraph, at most 60 words.
- Describe a SCENE or a composition: subject, style, palette, mood, lighting.
- The card is wide, 1200x630, and consumers crop it. Keep the subject clear of \
the edges and do not rely on anything near a corner.
- No lettering, no words, no logos and no user interface in the image unless \
the person explicitly asked for text. Generators render type badly, and a card \
with garbled words on it is worse than one with none.
- No people unless the person asked for people.
- If they described a picture, keep their description and only make it concrete.
- If they only made a request, invent a fitting abstract or illustrative scene \
for what the site does. Do not put the URL in the picture."""


def _brief_request(prompt: str, site_name: str, site_description: str) -> str:
    lines = [f"They typed: {prompt}"]
    if site_name:
        lines.append(f"The site is called: {site_name}")
    if site_description:
        lines.append(f"The site does: {site_description}")
    return "\n".join(lines)


async def build_brief(prompt: str, site_name: str = "", site_description: str = "") -> tuple[str, str]:
    """Turn what was typed into a description of a picture.

    Returns (brief, warning). Never raises: a brief is an improvement on the raw
    prompt, not a precondition for one, so a dead model degrades to the old
    behaviour rather than losing the user their click.
    """
    from app.services.llm_provider import get_llm_provider

    try:
        provider = get_llm_provider()
        result = await provider.create_completion(
            system_prompt=_BRIEF_SYSTEM,
            messages=[{"role": "user",
                       "content": _brief_request(prompt, site_name, site_description)}],
            model_tier="fast",
            max_tokens=300,
            use_cache=True,
        )
        brief = ((result or {}).get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("og brief failed, using the prompt as written", exc_info=True)
        return prompt, f"could not rewrite the prompt into a brief ({type(e).__name__}); used it as typed"

    if not brief:
        return prompt, "the brief came back empty; used the prompt as typed"
    # A model that answers with a refusal or a question has not written a brief.
    if brief.endswith("?") or len(brief) < 15:
        return prompt, "the brief came back unusable; used the prompt as typed"
    return brief, ""


def _assembly_instruction(prompt: str, site_name: str, site_description: str,
                          references: list) -> str:
    """What the assembling agent is told. Their words first, then the facts."""
    lines = [f"They asked: {prompt}"]
    if site_name:
        lines.append(f"The site is called: {site_name}")
    if site_description:
        lines.append(f"The site does: {site_description}")
    if references:
        lines.append(f"{len(references)} logo image(s) were supplied and are ready to "
                     "composite with place_logo. Use one unless they said not to.")
    else:
        lines.append("No logo was supplied. Do not invent one and do not ask the "
                     "renderer to draw one.")
    return "\n".join(lines)


def _is_public_address(host: str) -> bool:
    """Resolve a hostname and refuse anything that is not publicly routable.

    Checks every address the name resolves to, not just the first: a name that
    answers with one public and one loopback address is the standard way past a
    check that looks at one of them.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False

    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local  # 169.254.0.0/16, where cloud metadata lives
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return False
    return True


async def fetch_reference_image(url: str) -> tuple[str, bytes]:
    """Fetch one caller-supplied reference image, with the guards above."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise OgImageError(f"reference image URL must be http or https, got {parsed.scheme or 'nothing'!r}")
    if not parsed.hostname:
        raise OgImageError(f"reference image URL has no host: {url!r}")
    if not _is_public_address(parsed.hostname):
        raise OgImageError(f"reference image URL does not resolve to a public address: {parsed.hostname}")

    try:
        async with httpx.AsyncClient(timeout=REFERENCE_FETCH_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url)
    except Exception as e:  # noqa: BLE001
        raise OgImageError(f"could not fetch {url}: {type(e).__name__}: {e}") from e

    if 300 <= resp.status_code < 400:
        # Redirects are not followed on purpose: a public URL that redirects to
        # an internal address is the standard way past the check above. Said
        # plainly, because "HTTP 302" reads like a transient failure.
        raise OgImageError(
            f"{url} redirects, and redirects are not followed here. "
            f"Use the final URL directly ({resp.headers.get('location') or 'unknown target'})."
        )
    if resp.status_code >= 400:
        raise OgImageError(f"could not fetch {url}: HTTP {resp.status_code}")

    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise OgImageError(f"{url} is {mime or 'of unknown type'}, not an image")
    # SVG is an image and is still unusable here: Pillow cannot open one, and
    # rasterising it needs cairo or a browser, neither of which this service
    # carries. Refused by name rather than skipped, because a card that quietly
    # arrives with no mark on it looks like the feature not working.
    from app.services.og_card import _RASTER_MIMES

    if mime not in _RASTER_MIMES:
        raise OgImageError(
            f"{url} is {mime}, which cannot be composited. Export the logo as a PNG "
            "(transparent background, at least 400px wide) and use that URL instead.")
    if len(resp.content) > MAX_REFERENCE_BYTES:
        raise OgImageError(f"{url} is {len(resp.content)} bytes, over the {MAX_REFERENCE_BYTES} limit")
    if not resp.content:
        raise OgImageError(f"{url} returned an empty body")

    return mime, resp.content


def _decode_attachments(attachments: list[dict[str, Any]] | None) -> list[tuple[str, bytes]]:
    """Reference images sent inline, in the existing ChatAttachment shape."""
    import base64

    out: list[tuple[str, bytes]] = []
    for att in attachments or []:
        data = att.get("data")
        if not data or att.get("type", "image") != "image":
            continue
        try:
            raw = base64.b64decode(data)
        except Exception as e:  # noqa: BLE001
            raise OgImageError(f"attachment {att.get('name') or '?'} is not valid base64") from e
        if len(raw) > MAX_REFERENCE_BYTES:
            raise OgImageError(f"attachment {att.get('name') or '?'} is over the {MAX_REFERENCE_BYTES} limit")
        out.append(((att.get("mime_type") or "image/png").lower(), raw))
    return out


async def collect_reference_images(
    image_urls: list[str] | None,
    attachments: list[dict[str, Any]] | None,
) -> list[tuple[str, bytes]]:
    """Both legs, converged on the (mime, bytes) shape `_render` already takes."""
    images = _decode_attachments(attachments)
    for url in image_urls or []:
        if not (url or "").strip():
            continue
        images.append(await fetch_reference_image(url.strip()))

    if len(images) > MAX_REFERENCE_IMAGES:
        raise OgImageError(
            f"{len(images)} reference images given, the limit is {MAX_REFERENCE_IMAGES}"
        )
    return images


def to_card(raw: bytes) -> tuple[bytes, int, int]:
    """Centre-crop to the card ratio, resize to exactly 1200x630, encode JPEG.

    The crop is centred because neither backend is told where the subject is,
    and a centred cut is the one that is wrong least often. A render that came
    back square loses a third of its height here, which is why the caller is
    told when that happened rather than being handed a quietly different
    composition.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode not in ("RGB", "L"):
        # JPEG has no alpha channel, and an RGBA image flattens to black
        # without this. A card with a black wash over it looks like a bug in
        # the model rather than in the encoder.
        img = img.convert("RGB")

    target = OG_WIDTH / OG_HEIGHT
    source = img.width / img.height
    if source > target:
        # Wider than the card: take a full-height slice out of the middle.
        new_w = round(img.height * target)
        offset = (img.width - new_w) // 2
        img = img.crop((offset, 0, offset + new_w, img.height))
    elif source < target:
        new_h = round(img.width / target)
        offset = (img.height - new_h) // 2
        img = img.crop((0, offset, img.width, offset + new_h))

    img = img.resize((OG_WIDTH, OG_HEIGHT), Image.LANCZOS)

    # Walk the ladder down until it fits. The last step is kept whatever its
    # size: a card slightly over the limit still renders large on LinkedIn and
    # Teams, where returning nothing renders nowhere.
    encoded = b""
    for quality in QUALITY_STEPS:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        encoded = buf.getvalue()
        if len(encoded) <= OG_MAX_BYTES:
            break

    return encoded, OG_WIDTH, OG_HEIGHT


def card_filename(payload: bytes) -> str:
    """A name nothing has served before, derived from the bytes it names."""
    return f"og-{hashlib.sha256(payload).hexdigest()[:8]}.jpg"


def pick_provider(reference_images: list[tuple[str, bytes]], requested: str) -> tuple[str, str]:
    """Choose a backend, and say why when the choice is not the caller's.

    Returns (provider, warning).
    """
    if requested:
        return requested, ""
    if reference_images:
        return "gemini", ""
    return "minimax", ""


async def generate_og_cards(
    *,
    prompt: str = "",
    app_code: str,
    client_code: str,
    page_name: str = "",
    count: int = 6,
    image_urls: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    site_name: str = "",
    site_description: str = "",
    domain: str = "",
    auth: Any = None,
) -> dict[str, Any]:
    """Harvest the app's brand material, propose cards, draw and publish them.

    The whole pipeline, and no image-generation model anywhere in it:

        og_harvest    the app's logo, palette, typeface and a shot of its page
        og_advisor    a model chooses layouts, palettes and headlines, as data
        og_templates  a pure function draws each one
        here          encode as JPEG under the WhatsApp threshold, and publish

    Returns `{cards: [...], warnings: [...]}`, a set to pick from rather than
    one card to accept or regenerate. Rendering is cheap and deterministic, so
    offering six costs a fraction of what one diffusion render used to.

    Nothing here fails closed. A missing logo, an unreachable model, a blank
    screenshot and an app with no theme each remove an option and add a
    sentence; none of them stops a card being made.
    """
    from app.services import og_advisor, og_fonts, og_harvest, og_paint, og_templates

    if not app_code:
        raise OgImageError("app_code is required")
    prompt = (prompt or "").strip()
    count = max(1, min(int(count or 1), og_advisor.MAX_PROPOSALS))

    facts = await og_harvest.harvest(
        app_code, client_code, headers or {}, page_name=page_name,
        title=site_name, description=site_description, domain=domain)
    warnings: list[str] = list(facts.warnings)

    # A URL typed INTO the prompt is an instruction, not decoration. "Use the
    # logo at https://.../mark.png" used to go nowhere.
    from app.services.og_card import urls_in_prompt

    supplied = list(image_urls or []) + urls_in_prompt(prompt)
    if supplied or attachments:
        override, note = await _supplied_mark(supplied, attachments)
        if note:
            warnings.append(note)
        if override is not None:
            facts.logo = override

    fonts = await og_fonts.resolve_family(facts.font_family)
    warnings.extend(fonts.warnings)

    specs, advice_warnings = await og_advisor.suggest(facts, prompt, count, auth=auth)
    warnings.extend(advice_warnings)

    cards: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        try:
            image = og_templates.render(spec, fonts)
            payload, width, height = to_card(_encode(og_paint.flatten(
                image, spec.background.base)))
        except Exception as e:  # noqa: BLE001 - one bad card must not lose the rest
            logger.warning("og card %d failed to draw", index, exc_info=True)
            warnings.append(f"one layout could not be drawn ({type(e).__name__})")
            continue

        published, error = await _publish(payload, app_code, client_code, page_name,
                                          headers or {})
        if error:
            warnings.append(error)
            continue
        cards.append({**published, "width": width, "height": height,
                      "bytes": len(payload), "type": _MIME_JPEG,
                      **_describe(spec)})

    if not cards:
        raise OgImageError("no card could be produced; " + ("; ".join(warnings)
                                                            or "no reason was reported"))

    return {
        "cards": cards,
        # The first card, flattened onto the response, so a caller that wants
        # one image does not have to reach into the list.
        **{k: v for k, v in cards[0].items() if k in ("url", "rel", "width",
                                                      "height", "bytes", "type")},
        "font": fonts.family,
        "palette": facts.accents,
        "logo": bool(facts.logo),
        "screenshot": facts.shot is not None,
        "warnings": warnings,
    }


def _describe(spec: Any) -> dict[str, Any]:
    """What a card is, in the response.

    The pane shows this under each option, and it is what lets someone say "the
    blue one, left aligned" rather than "the third one". It is also the whole
    spec minus the images, which makes a card reproducible from its own record.
    """
    return {
        "template": spec.template,
        "headline": spec.headline,
        "subline": spec.subline,
        "accent": spec.accent,
        "background": {"kind": spec.background.kind, "base": spec.background.base},
    }


def _encode(image: Any) -> bytes:
    """A flattened card to JPEG bytes, before the size ladder runs on it."""
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92, optimize=True, progressive=True)
    return buffer.getvalue()


async def _supplied_mark(urls: list[str],
                         attachments: list[dict[str, Any]] | None
                         ) -> tuple[Any, str]:
    """A logo the person supplied, which beats the one we found.

    Somebody who pastes a mark into the box means "use this one", and the
    harvested favicon is a guess by comparison.
    """
    from app.services import og_harvest

    try:
        references = await collect_reference_images(urls, attachments)
    except OgImageError as e:
        return None, str(e)
    if not references:
        return None, ""

    mime, payload = references[0]
    if og_harvest._looks_like_svg(payload, mime):
        image, note = await og_harvest.rasterise_svg(payload)
        return image, note
    try:
        from PIL import Image
        return og_harvest.trim_transparent(
            Image.open(io.BytesIO(payload)).convert("RGBA")), ""
    except Exception:  # noqa: BLE001
        return None, "the image you supplied could not be read, so the card uses the site's own logo"


async def _publish(payload: bytes, app_code: str, client_code: str,
                   page_name: str, headers: dict[str, str]) -> tuple[dict[str, str], str]:
    """Write one card to the app's static space. Returns `({url, rel}, error)`."""
    from app.agents.appbuilder.tools.modlix import visuals

    filename = card_filename(payload)
    out_dir = Path("/tmp/cfa-generated-images")
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / filename
    local.write_bytes(payload)

    rel, absolute, error = await visuals._upload_generated_static(
        local, page_name or "global", "og", filename, app_code, client_code,
        headers or {}, _MIME_JPEG)
    if error:
        return {}, f"one card could not be published ({error})"
    return {"url": absolute, "rel": rel}, ""


async def generate_og_image(
    *,
    prompt: str,
    app_code: str,
    client_code: str,
    page_name: str = "",
    style_notes: str = "",
    image_provider: str = "",
    image_urls: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    site_name: str = "",
    site_description: str = "",
    auth: Any = None,
) -> dict[str, Any]:
    """Render, crop, encode and publish one card. Returns the response body."""
    from app.agents.appbuilder.tools.modlix import visuals

    prompt = (prompt or "").strip()
    if not prompt:
        raise OgImageError("prompt is required")
    if not app_code:
        raise OgImageError("app_code is required")

    warnings: list[str] = []

    from app.services import og_card

    # A URL someone typed INTO the prompt is an instruction, not decoration.
    # "Use the logo here https://.../mainLogo.png" used to go nowhere: reference
    # images came only from the fields, and the brief step was told to keep URLs
    # out of the picture, so a clear instruction was silently dropped.
    supplied = list(image_urls or []) + og_card.urls_in_prompt(prompt)
    references: list[tuple[str, bytes]] = []
    try:
        references = await collect_reference_images(supplied, attachments)
    except OgImageError as e:
        # A logo that cannot be used must not cost the person their card. The
        # reason is carried out instead, and the card is made without it.
        warnings.append(str(e))

    # The renderer, bound so the agent's `make_plate` can call it without
    # knowing anything about provider routing.
    async def render(brief: str):
        provider, _ = pick_provider(references, (image_provider or "").strip().lower())
        profile = visuals._ASPECT_PROFILES[RENDER_ASPECT]
        return await visuals._render(
            provider, brief, style_notes, RENDER_ASPECT, profile, references or None, "")

    instruction = _assembly_instruction(prompt, site_name, site_description, references)
    out_dir = Path("/tmp/cfa-generated-images")
    out_dir.mkdir(parents=True, exist_ok=True)

    card_path, agent_warnings, provider_used, model_used = await og_card.assemble_card(
        instruction=instruction, render=render, logos=[b for _m, b in references],
        work_dir=str(out_dir), auth=auth,
    )
    warnings.extend(agent_warnings)

    if card_path:
        raw = Path(card_path).read_bytes()
        brief = instruction
    else:
        # The assembling stopped without a plate. Fall back to the one-shot
        # path: a plain card is worth more than an error, and the brief step is
        # the same one this used before there was an agent.
        warnings.append("assembled nothing, so the card is a plain generated plate")
        brief, brief_warning = await build_brief(prompt, site_name, site_description)
        if brief_warning:
            warnings.append(brief_warning)
        provider, _ = pick_provider(references, (image_provider or "").strip().lower())
        profile = visuals._ASPECT_PROFILES[RENDER_ASPECT]
        raw, provider_used, model_used, error, note = await visuals._render(
            provider, brief, style_notes, RENDER_ASPECT, profile, references or None, "")
        if error:
            raise OgImageError(error)
        if note:
            warnings.append(note)

    payload, width, height = to_card(raw)

    if provider_used == "gemini":
        # Gemini has no aspect field and comes back square whatever was asked,
        # so the plate above is a centre cut out of it. Said plainly, because
        # the alternative is the caller wondering where their composition went.
        warnings.append(
            f"rendered on gemini, which returns a square image; the card is a centre crop to "
            f"{OG_WIDTH}x{OG_HEIGHT}"
        )
    if len(payload) > OG_MAX_BYTES:
        warnings.append(
            f"the card is {len(payload)} bytes, over the ~{OG_MAX_BYTES} WhatsApp shows large. "
            "It will still render on LinkedIn, Teams and X."
        )

    filename = card_filename(payload)
    out_dir = Path("/tmp/cfa-generated-images")
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / filename
    local.write_bytes(payload)

    rel, absolute, up_err = await visuals._upload_generated_static(
        local, page_name or "global", "og", filename, app_code, client_code,
        headers or {}, _MIME_JPEG,
    )
    if up_err:
        raise OgImageError(f"{up_err}\n(card saved at {local})")

    return {
        "url": absolute,
        "rel": rel,
        "width": width,
        "height": height,
        "bytes": len(payload),
        "type": _MIME_JPEG,
        "provider": provider_used,
        "model": model_used,
        # What was actually asked for, so a card nobody expected is explicable
        # without reading a log. The pane shows it under the Generate button.
        "brief": brief,
        "warnings": warnings,
    }
