"""Choosing what a card should look like. The model advises; it never draws.

The split this module exists to enforce:

    harvest   ->   ADVISOR   ->   CardSpec   ->   og_templates   ->   pixels
                   (a model)      (data)         (deterministic)

The model is handed the app's real screenshot, its real logo, its real palette
and whatever the person typed, and it answers with a *specification*: which
layout, which background, which colours, what the headline should say. It is
never handed a canvas. Every number it returns is clamped, every colour is
parsed or replaced, and an unknown layout name falls back. Drawing stays in
`og_templates`, where it is a pure function.

This is what takes the image-generation model out of the loop entirely. A
diffusion model cannot place a logo, set type, or hold a grid; what it was
actually contributing was taste about colour and composition, and that is a
judgement a language model can make as data.

**There is always an answer.** `presets` builds proposals from the harvested
palette with no model at all, and it is both the fallback when the model is
unavailable and the baseline the model's suggestions are merged into. A card is
never blocked on an LLM being reachable, in credit, or in the mood to return
valid JSON.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
from typing import Any

from PIL import Image

from app.services import og_paint as P
from app.services import og_templates as T
from app.services.og_harvest import SiteFacts

logger = logging.getLogger(__name__)

#: How many proposals a picker shows. Enough to choose from, few enough to look
#: at in one glance.
DEFAULT_PROPOSALS = 6

#: Ceiling on what one request may ask for, so a caller cannot turn the picker
#: into a render farm.
MAX_PROPOSALS = 12

#: Images sent to the advisor are downscaled first. It is being asked about
#: composition and colour, and neither needs 1440x900.
VISION_W = 760

#: Plates to fall back on when the app's theme has no usable neutral. Dark
#: first: a dark card reads better in every consumer's feed, and a light one
#: has to be chosen deliberately.
_FALLBACK_PLATES = ("#0b0b12", "#101726", "#f6f5f9")
_FALLBACK_ACCENTS = ("#7c3aed", "#2563eb", "#e879f9", "#f59e0b")

_ADVISOR_SYSTEM = """\
You art-direct Open Graph cards: the picture that appears when a link is shared \
in WhatsApp, LinkedIn, Slack or Teams. The card is 1200x630.

You do not draw anything. You choose, and a deterministic renderer does the \
drawing. Answer with JSON only, no prose and no code fence.

You are given the site's real logo, a screenshot of its real page, its brand \
palette taken from its theme, and whatever the person typed. Use them. Do not \
invent a colour that is not in the palette unless the person named one.

Return this shape:

{"proposals": [
  {
    "template": "<one of the template names given below>",
    "headline": "<at most 8 words; the promise, not the company name>",
    "subline": "<at most 14 words, or empty>",
    "background": {
      "kind": "mesh" | "linear" | "flat",
      "base": "#rrggbb",
      "blobs": [{"color": "#rrggbb", "cx": 0.0-1.1, "cy": 0.0-1.1,
                 "r": 0.2-0.9, "alpha": 0.3-1.0}],
      "stops": [[0.0, "#rrggbb"], [1.0, "#rrggbb"]],
      "grid": 0.0-0.1,
      "vignette": 0.0-0.6
    },
    "accent": "#rrggbb",
    "gradient_headline": true | false,
    "why": "<one short sentence>"
  }
]}

Rules:
- `blobs` is for kind `mesh`, `stops` for kind `linear`. Give two or three \
blobs, each a wide soft wash. `cx`/`cy` may sit slightly past an edge so the \
wash bleeds off the card.
- `base` carries the card. On a dark base use dark, saturated washes; a pale \
wash on a dark base turns to mud.
- Vary the proposals. Different layouts, different palettes, not one idea \
recoloured.
- The headline is the product's promise. Never the URL, never "Welcome to".
- If the person named colours or a mood, follow them over the palette.
- Judge the screenshot: if the page looks empty or unfinished, prefer a \
template that does not show it."""


# ── building a spec safely ───────────────────────────────────────────────────

def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _colour(value: Any, fallback: str) -> str:
    """A model-supplied colour, or the fallback if it is not really one.

    `parse_color` never raises, so a bad value would otherwise become opaque
    black and quietly ruin a card. Round-tripping it is what tells the two
    apart.
    """
    if not isinstance(value, str) or not value.strip():
        return fallback
    parsed = P.parse_color(value, (0, 0, 0, 0))
    return P.to_hex(parsed) if parsed[3] else fallback


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:limit].strip()


def background_from(raw: Any, facts: SiteFacts) -> T.Background:
    """A `Background` from whatever the model returned, with every field clamped."""
    plate = facts.neutrals[0] if facts.neutrals else _FALLBACK_PLATES[0]
    if not isinstance(raw, dict):
        return T.Background("mesh", plate, _default_blobs(facts))

    kind = str(raw.get("kind") or "mesh").lower()
    if kind not in ("mesh", "linear", "flat"):
        kind = "mesh"

    background = T.Background(
        kind=kind,
        base=_colour(raw.get("base"), plate),
        grid=_clamp(raw.get("grid"), 0.0, 0.12, 0.0),
        vignette=_clamp(raw.get("vignette"), 0.0, 0.7, 0.0),
        noise=0.03,
    )

    if kind == "mesh":
        background.blobs = _blobs_from(raw.get("blobs"), facts)
    elif kind == "linear":
        background.stops = _stops_from(raw.get("stops"), facts, background.base)
        background.angle = _clamp(raw.get("angle"), 0.0, 360.0, 120.0)
    return background


def _blobs_from(raw: Any, facts: SiteFacts) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        return _default_blobs(facts)
    out: list[dict[str, Any]] = []
    for entry in raw[:4]:
        if not isinstance(entry, dict):
            continue
        out.append({
            "color": _colour(entry.get("color"), facts.accent),
            # Slightly past an edge is allowed on purpose: that is how a wash
            # bleeds off the card rather than sitting in it as a disc.
            "cx": _clamp(entry.get("cx"), -0.2, 1.2, 0.5),
            "cy": _clamp(entry.get("cy"), -0.2, 1.2, 0.5),
            "r": _clamp(entry.get("r"), 0.12, 1.1, 0.6),
            "alpha": _clamp(entry.get("alpha"), 0.1, 1.0, 0.9),
        })
    return out or _default_blobs(facts)


def _stops_from(raw: Any, facts: SiteFacts, base: str) -> list[tuple[float, str]]:
    if not isinstance(raw, list) or len(raw) < 2:
        return [(0.0, base), (1.0, facts.accent)]
    out: list[tuple[float, str]] = []
    for entry in raw[:5]:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            out.append((_clamp(entry[0], 0.0, 1.0, 0.0), _colour(entry[1], base)))
        elif isinstance(entry, dict):
            out.append((_clamp(entry.get("at", entry.get("pos")), 0.0, 1.0, 0.0),
                        _colour(entry.get("color"), base)))
    return out if len(out) >= 2 else [(0.0, base), (1.0, facts.accent)]


def _default_blobs(facts: SiteFacts) -> list[dict[str, Any]]:
    accents = facts.accents or list(_FALLBACK_ACCENTS)
    return [
        {"color": accents[0], "cx": 0.82, "cy": 0.18, "r": 0.66, "alpha": 0.92},
        {"color": accents[1 % len(accents)], "cx": 0.18, "cy": 0.88, "r": 0.5, "alpha": 0.6},
    ]


def spec_from(raw: Any, facts: SiteFacts, *, allowed: tuple[str, ...]) -> T.CardSpec:
    """One proposal to a `CardSpec`. Never raises, never trusts a field."""
    raw = raw if isinstance(raw, dict) else {}

    template = str(raw.get("template") or "")
    if template not in allowed:
        template = allowed[0]

    return T.CardSpec(
        template=template,
        headline=_text(raw.get("headline"), 90) or facts.title,
        subline=_text(raw.get("subline"), 130) or facts.description,
        domain=facts.domain,
        background=background_from(raw.get("background"), facts),
        accent=_colour(raw.get("accent"), facts.accent),
        gradient_headline=bool(raw.get("gradient_headline")),
        logo=facts.logo,
        shot=facts.shot,
    )


# ── the deterministic baseline ───────────────────────────────────────────────

def presets(facts: SiteFacts, count: int = DEFAULT_PROPOSALS,
            seed: int | None = None) -> list[T.CardSpec]:
    """Proposals built from the harvested palette, with no model involved.

    Both the fallback and the floor. Seeded from the app code so the same site
    gets the same set every time: a picker that reshuffles on each visit makes
    it impossible to go back for the one you liked.
    """
    rng = random.Random(seed if seed is not None else f"og:{facts.app_code}")
    accents = list(facts.accents) or list(_FALLBACK_ACCENTS)
    darks = [c for c in facts.neutrals if P.relative_luminance(P.parse_color(c)) < 0.25]
    lights = [c for c in facts.neutrals if P.relative_luminance(P.parse_color(c)) > 0.8]
    plates = darks or [_FALLBACK_PLATES[0]]

    usable = T.usable_templates(has_logo=facts.logo is not None,
                                has_shot=facts.shot is not None,
                                has_stats=False)
    names = [t.name for t in usable] or [T.TEMPLATE_NAMES[0]]

    out: list[T.CardSpec] = []
    for i in range(max(1, min(count, MAX_PROPOSALS))):
        accent = accents[i % len(accents)]
        second = accents[(i + 1) % len(accents)]
        # Every third card is light, so the set is not six shades of one idea.
        light = bool(lights) and i % 3 == 2
        plate = lights[0] if light else plates[i % len(plates)]

        if light:
            background = T.Background("linear", plate,
                                      stops=[(0.0, "#ffffff"), (1.0, plate)],
                                      angle=150.0, noise=0.02)
        elif i % 2 == 0:
            background = T.Background("mesh", plate, [
                {"color": accent, "cx": 0.82, "cy": 0.16, "r": 0.66, "alpha": 0.92},
                {"color": second, "cx": 0.14, "cy": 0.9, "r": 0.52, "alpha": 0.62},
            ], grid=0.045)
        else:
            background = T.Background("mesh", plate, [
                {"color": accent, "cx": 0.5, "cy": 1.02, "r": 0.78, "alpha": 0.85},
                {"color": second, "cx": 1.04, "cy": 0.12, "r": 0.42, "alpha": 0.55},
            ], vignette=0.25)

        out.append(T.CardSpec(
            template=names[i % len(names)],
            headline=facts.title,
            subline=facts.description,
            domain=facts.domain,
            background=background,
            accent=accent,
            gradient_headline=(i % 4 == 3),
            logo=facts.logo,
            shot=facts.shot,
        ))

    rng.shuffle(out)
    return out


# ── asking the model ─────────────────────────────────────────────────────────

def _image_block(image: Image.Image, label: str) -> dict[str, Any]:
    """One Pillow image as a provider-neutral content block."""
    small = image.convert("RGB")
    if small.width > VISION_W:
        ratio = VISION_W / small.width
        small = small.resize((VISION_W, max(1, int(small.height * ratio))), Image.LANCZOS)
    buffer = io.BytesIO()
    small.save(buffer, format="JPEG", quality=78, optimize=True)
    logger.debug("og advisor sending %s at %s", label, small.size)
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.b64encode(buffer.getvalue()).decode("ascii")}}


def _brief(facts: SiteFacts, prompt: str, allowed: tuple[str, ...], count: int) -> str:
    lines = [
        f"Give me {count} different proposals.",
        "",
        f"Templates you may choose from: {', '.join(allowed)}.",
        f"The site is called: {facts.title or facts.app_code}",
    ]
    if facts.description:
        lines.append(f"What it does: {facts.description}")
    if facts.domain:
        lines.append(f"Its address: {facts.domain}")
    if facts.accents:
        lines.append(f"Brand colours from its theme: {', '.join(facts.accents[:6])}")
    if facts.neutrals:
        lines.append(f"Neutrals from its theme: {', '.join(facts.neutrals[:4])}")
    if facts.font_family:
        lines.append(f"Its typeface: {facts.font_family}")
    lines.append(f"A logo {'is' if facts.logo is not None else 'is not'} available.")
    if facts.shot is not None:
        lines.append("A screenshot of its page is attached; judge whether it is "
                     "worth showing.")
    else:
        lines.append("No usable screenshot, so do not choose a template that needs one.")
    if prompt:
        lines += ["", f"What the person typed: {prompt}"]
    return "\n".join(lines)


#: Starting output budget. Generous because the balanced tier here is a
#: REASONING model: its thinking is charged against the same budget as its
#: answer, so a limit sized for the JSON alone buys nothing but thought.
#: Measured at 2000: `stop_reason=length`, zero characters of content.
ADVISOR_MAX_TOKENS = 6000


async def suggest(facts: SiteFacts, prompt: str = "",
                  count: int = DEFAULT_PROPOSALS,
                  provider_name: str | None = None,
                  auth: Any = None) -> tuple[list[T.CardSpec], list[str]]:
    """Ask the model for proposals. Returns `(specs, warnings)`.

    Falls back to `presets` on every failure, and tops up from `presets` when
    the model returns fewer than asked, so the picker is always full.

    `auth` carries a `CallMeter`. This talks to a provider directly rather than
    through the agent loop, so nothing else in the path meters it, and an
    unmetered call runs against a suspended wallet and is billed for none of it.
    """
    count = max(1, min(count, MAX_PROPOSALS))
    warnings: list[str] = []
    allowed = tuple(t.name for t in T.usable_templates(
        has_logo=facts.logo is not None, has_shot=facts.shot is not None,
        has_stats=False)) or (T.TEMPLATE_NAMES[0],)

    blocks: list[dict[str, Any]] = [{"type": "text",
                                     "text": _brief(facts, prompt, allowed, count)}]
    if facts.shot is not None:
        blocks.append(_image_block(facts.shot, "screenshot"))
    if facts.logo is not None:
        blocks.append(_image_block(facts.logo, "logo"))

    content, failure = await _complete(blocks, provider_name, auth)
    if failure:
        return presets(facts, count), [failure]

    from app.services.blueprint.service import parse_json_object

    parsed, reason = parse_json_object(content)
    if parsed is None:
        warnings.append(f"the design suggestions came back unreadable ({reason}), so "
                        "these are built from the site's own colours")
        return presets(facts, count), warnings

    raw = parsed.get("proposals")
    if not isinstance(raw, list) or not raw:
        warnings.append("no design suggestions came back, so these are built from "
                        "the site's own colours")
        return presets(facts, count), warnings

    specs = [spec_from(entry, facts, allowed=allowed) for entry in raw[:count]]
    if len(specs) < count:
        # Topped up rather than left short: a half-empty picker looks broken,
        # and the presets are perfectly good cards.
        specs += presets(facts, count - len(specs))[: count - len(specs)]
    return specs, warnings


async def _complete(blocks: list[dict[str, Any]], provider_name: str | None,
                    auth: Any) -> tuple[str, str]:
    """One JSON-returning call, retried once at double the budget.

    Returns `(content, failure_message)`. The retry is not politeness about
    flaky models: a reasoning model that runs out of budget while thinking
    returns `stop_reason=length` with **zero characters of content**, which is
    indistinguishable at the call site from "the model had nothing to say".
    Doubling the budget is the only thing that fixes it.

    Both attempts are charged. A retry is a second real call and the tokens are
    spent whether or not the answer parsed; billing only the successful one
    would make the failing path the free one.
    """
    from app.services.llm_provider import get_llm_provider

    meter = _meter(auth)
    provider = get_llm_provider(provider_name or _advisor_provider())
    budget = ADVISOR_MAX_TOKENS

    for attempt in (1, 2):
        if meter is not None and not await meter.allowed():
            from app.services.billing import OUT_OF_TOKENS
            return "", OUT_OF_TOKENS

        try:
            result = await provider.create_completion(
                system_prompt=_ADVISOR_SYSTEM,
                messages=[{"role": "user", "content": blocks}],
                model_tier="balanced",
                max_tokens=budget,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("og advisor call failed", exc_info=True)
            return "", (f"could not ask for design suggestions ({type(e).__name__}), "
                        "so these are built from the site's own colours")

        if meter is not None:
            await meter.charge(result)

        content = ((result or {}).get("content") or "").strip()
        stop = str((result or {}).get("stop_reason")
                   or (result or {}).get("finish_reason") or "")
        if content:
            return content, ""
        if stop != "length" or attempt == 2:
            return "", ("the design suggestions came back empty, so these are built "
                        "from the site's own colours")

        logger.info("og advisor: empty answer at %d tokens (stop=length), retrying "
                    "at %d", budget, budget * 2)
        budget *= 2

    return "", "the design suggestions came back empty"


def _meter(auth: Any):
    """A `CallMeter` for `auth`, or None when there is nothing to bill."""
    if auth is None:
        return None
    try:
        from app.services.billing import CallMeter
        return CallMeter(auth, session_id="og-advisor")
    except Exception:  # noqa: BLE001
        logger.warning("og advisor could not build a call meter", exc_info=True)
        return None


def _advisor_provider() -> str | None:
    """Bill the advisor to the same provider the builder uses.

    Left to the global default it picks up whatever `LLM_PROVIDER` happens to
    be, which on this deployment is a provider with no key configured; the call
    then fails with an authentication error that reads like a bug in this
    module.
    """
    from app.config import settings
    return getattr(settings, "APPBUILDER_PROVIDER", None) or None
