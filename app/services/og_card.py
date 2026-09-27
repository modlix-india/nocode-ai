"""Assemble an Open Graph card: a generated plate, the real logo, real text.

The split here is the whole point, and it is not the obvious one.

**The image model never draws the logo or the words.** Handed a logo it redraws
it, and a redraw is not the mark: `config.py:438` records Gemini embossing a real
brand's logo onto a prompt that asked for an unbranded product, and both backends
garble lettering. So the model produces a BACKGROUND and nothing else, and the
mark and the headline are composited on afterwards with Pillow, pixel for pixel.

**The agent does the assembling, not a fixed template.** It picks the layout,
places the mark, sets the headline, then LOOKS at the result through
`inspect_card` and adjusts. That is why these are tools rather than a function:
a card for a dark photographic plate and a card for a flat pastel one want
different placements and different text colours, and nothing in a fixed template
can tell those apart.

Everything a tool does is deterministic. The agent chooses; Pillow executes.
"""

from __future__ import annotations

import base64
import glob
import io
import logging
import re
from pathlib import Path
from typing import Any

from app.core.agent import BaseAgent as BaseAgentBase
from app.core.streaming import AgentEventStream
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# Where a run keeps its working card. One key on the tool context, so a tool can
# find what the previous tool made without the agent having to carry a path
# around in its own words -- which it would eventually get wrong.
CTX = "og_card"

POSITIONS = ("top-left", "top-center", "top-right",
             "center-left", "center", "center-right",
             "bottom-left", "bottom-center", "bottom-right")

# A font has to come from somewhere, and there is none in this repo and none
# bundled with Pillow. These are the usual places on the two platforms this runs
# on; the container needs one of the Linux packages or the headline falls back
# to Pillow's bitmap default, which is unreadable at card size and says so.
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
)

# Raster only. Pillow cannot open SVG, and rasterising one needs cairo or a
# browser; neither is a dependency this service carries. An SVG logo is REFUSED
# with a sentence naming the fix rather than silently skipped, because a card
# that quietly has no mark on it looks like the feature not working.
_RASTER_MIMES = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif")

# Image URLs typed into the prompt itself. People write "use the logo here
# <url>" in the box, and before this the URL was dropped: the reference fields
# are elsewhere, and the brief step was explicitly told to keep URLs out of the
# picture. So the instruction went nowhere and nothing said so.
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')]+", re.I)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")


def urls_in_prompt(prompt: str) -> list[str]:
    """Image URLs someone wrote into the prompt, in the order they wrote them."""
    out: list[str] = []
    for raw in _URL_IN_TEXT.findall(prompt or ""):
        url = raw.rstrip(".,;:!?)")
        if url.lower().split("?")[0].endswith(_IMAGE_SUFFIXES) and url not in out:
            out.append(url)
    return out


def resolve_font(size: int):
    """A real typeface at `size`, or Pillow's bitmap default as a last resort."""
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        for found in sorted(glob.glob(path)):
            try:
                return ImageFont.truetype(found, size), ""
            except Exception:  # noqa: BLE001 — a font that will not load is not an error
                continue
    return ImageFont.load_default(), (
        "no scalable font found on this host, so the headline is drawn in a small "
        "bitmap face; install DejaVu, Liberation or Noto in the image"
    )


def _anchor(position: str, box: tuple[int, int], canvas: tuple[int, int],
            margin: int) -> tuple[int, int]:
    """Top-left pixel for `box` placed at `position` within `canvas`."""
    bw, bh = box
    cw, ch = canvas
    vert, _, horiz = position.partition("-")
    x = {"left": margin, "center": (cw - bw) // 2, "right": cw - bw - margin}.get(
        horiz or "center", (cw - bw) // 2)
    y = {"top": margin, "center": (ch - bh) // 2, "bottom": ch - bh - margin}.get(
        vert, (ch - bh) // 2)
    return x, y


def _state(context: dict[str, Any]) -> dict[str, Any]:
    return context.setdefault(CTX, {})


# ── the tools ────────────────────────────────────────────────────────────────

async def _execute_make_plate(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.services import og_image_ai as O

    brief = (params.get("brief") or "").strip()
    if not brief:
        return ToolResult(success=False, error="`brief` is required")

    st = _state(context)
    render = st.get("render")
    if not render:
        return ToolResult(success=False, error="no renderer wired for this run")

    raw, provider, model, err, note = await render(brief)
    if err:
        return ToolResult(success=False, error=err)

    payload, w, h = O.to_card(raw)
    path = Path(st["work_dir"]) / "plate.png"
    # PNG while the card is being worked on. Re-encoding JPEG on every composite
    # would stack generation loss; the single JPEG happens once, at finish.
    from PIL import Image
    Image.open(io.BytesIO(payload)).convert("RGB").save(path, format="PNG")
    st["card"] = str(path)
    st["provider"] = provider
    st["model"] = model
    if note:
        st.setdefault("warnings", []).append(note)

    return ToolResult(
        success=True,
        data={"width": w, "height": h, "provider": provider},
        summary=f"Plate rendered at {w}x{h} on {provider}. Nothing is on it yet.",
    )


async def _execute_place_logo(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from PIL import Image

    st = _state(context)
    if not st.get("card"):
        return ToolResult(success=False, error="make_plate first: there is nothing to place a logo on")

    logos = st.get("logos") or []
    if not logos:
        return ToolResult(success=False, error="no usable logo was supplied with this request")

    index = int(params.get("index") or 0)
    if index >= len(logos):
        return ToolResult(success=False, error=f"logo {index} does not exist; {len(logos)} supplied")

    position = (params.get("position") or "bottom-left").strip()
    if position not in POSITIONS:
        return ToolResult(success=False, error=f"position must be one of {list(POSITIONS)}")
    width_pct = max(5, min(60, int(params.get("width_pct") or 22)))
    margin = max(0, min(200, int(params.get("margin_px") or 48)))

    card = Image.open(st["card"]).convert("RGBA")
    logo = Image.open(io.BytesIO(logos[index])).convert("RGBA")

    target_w = int(card.width * width_pct / 100)
    scale = target_w / logo.width
    logo = logo.resize((target_w, max(1, int(logo.height * scale))), Image.LANCZOS)

    x, y = _anchor(position, logo.size, card.size, margin)
    # `logo` as its own mask, so transparency stays transparent. Pasting without
    # one fills the alpha with black and the mark arrives in a box.
    card.alpha_composite(logo, (x, y))
    card.convert("RGB").save(st["card"], format="PNG")

    return ToolResult(
        success=True,
        data={"placed_at": [x, y], "size": list(logo.size)},
        summary=f"Logo placed {position} at {logo.width}x{logo.height}px.",
    )


async def _execute_draw_headline(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from PIL import Image, ImageDraw

    st = _state(context)
    if not st.get("card"):
        return ToolResult(success=False, error="make_plate first: there is nothing to draw on")

    text = (params.get("text") or "").strip()
    if not text:
        return ToolResult(success=False, error="`text` is required")

    position = (params.get("position") or "center-left").strip()
    if position not in POSITIONS:
        return ToolResult(success=False, error=f"position must be one of {list(POSITIONS)}")
    size_px = max(18, min(120, int(params.get("size_px") or 64)))
    colour = (params.get("colour") or "#FFFFFF").strip()
    margin = max(0, min(200, int(params.get("margin_px") or 56)))
    width_pct = max(20, min(100, int(params.get("width_pct") or 62)))

    card = Image.open(st["card"]).convert("RGB")
    font, font_warning = resolve_font(size_px)
    draw = ImageDraw.Draw(card)

    # Wrap by measuring, not by counting characters: a proportional face makes
    # "Illinois" and "WWWWWWWW" the same length in characters and nothing like
    # it in pixels.
    max_w = int(card.width * width_pct / 100)
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)

    leading = int(size_px * 1.22)
    block = (max((draw.textlength(ln, font=font) for ln in lines), default=0), leading * len(lines))
    x, y = _anchor(position, (int(block[0]), block[1]), card.size, margin)

    for i, ln in enumerate(lines):
        # A soft dark offset under the glyphs. A headline sits on a picture
        # nobody chose for contrast, and one light patch behind a light word is
        # the difference between a card and an empty rectangle.
        draw.text((x + 2, y + i * leading + 2), ln, font=font, fill="#00000055")
        draw.text((x, y + i * leading), ln, font=font, fill=colour)

    card.save(st["card"], format="PNG")
    if font_warning:
        st.setdefault("warnings", []).append(font_warning)

    return ToolResult(
        success=True,
        data={"lines": lines, "at": [x, y]},
        summary=f"Headline drawn {position} on {len(lines)} line(s) at {size_px}px.",
    )


async def _execute_inspect_card(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    st = _state(context)
    if not st.get("card"):
        return ToolResult(success=False, error="nothing has been made yet")

    raw = Path(st["card"]).read_bytes()
    # Returned as `image_base64`, which `extract_anthropic_image_blocks` turns
    # into a real image block. This is the whole reason the agent can correct
    # itself: without it, it is placing things it cannot see.
    return ToolResult(
        success=True,
        data={"image_base64": base64.b64encode(raw).decode(), "mime_type": "image/png"},
        summary="The card as it stands. Check the mark is clear of the edges and the "
                "headline is readable against what is behind it.",
    )


async def _execute_finish_card(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    st = _state(context)
    if not st.get("card"):
        return ToolResult(success=False, error="nothing has been made yet")
    st["done"] = True
    return ToolResult(success=True, data={"done": True},
                      summary="Card accepted. Stop here.")


def _pos_desc() -> str:
    return "One of: " + ", ".join(POSITIONS)


make_plate_tool = ToolDefinition(
    name="make_plate",
    description="Render the BACKGROUND of the card from a visual brief, already cropped to "
                "1200x630. No text and no logo: those are placed afterwards. Call this first.",
    parameters=[ToolParameter(name="brief", type="string",
                              description="A description of the background picture. Scene, "
                                          "style, palette, lighting. Leave room where the "
                                          "mark and the headline will go.")],
    execute=_execute_make_plate,
)

place_logo_tool = ToolDefinition(
    name="place_logo",
    description="Composite a supplied logo onto the card at its real pixels. Use this rather "
                "than asking for a logo in the brief: a generated logo is a redraw and is "
                "always subtly wrong.",
    parameters=[
        ToolParameter(name="position", type="string", required=False,
                      description=_pos_desc() + ". Default bottom-left."),
        ToolParameter(name="width_pct", type="integer", required=False,
                      description="Logo width as a percent of the card, 5-60. Default 22."),
        ToolParameter(name="margin_px", type="integer", required=False,
                      description="Distance from the edge. Default 48."),
        ToolParameter(name="index", type="integer", required=False,
                      description="Which supplied logo, when more than one. Default 0."),
    ],
    execute=_execute_place_logo,
)

draw_headline_tool = ToolDefinition(
    name="draw_headline",
    description="Draw real text on the card in a real typeface. Wraps by measuring. Use this "
                "for anything the reader must be able to READ; generators garble lettering.",
    parameters=[
        ToolParameter(name="text", type="string", description="The words to draw. Keep it short."),
        ToolParameter(name="position", type="string", required=False,
                      description=_pos_desc() + ". Default center-left."),
        ToolParameter(name="size_px", type="integer", required=False,
                      description="Cap height in pixels, 18-120. Default 64."),
        ToolParameter(name="colour", type="string", required=False,
                      description="Hex like #FFFFFF. Pick for contrast against the plate."),
        ToolParameter(name="width_pct", type="integer", required=False,
                      description="Wrap width as a percent of the card. Default 62."),
        ToolParameter(name="margin_px", type="integer", required=False,
                      description="Distance from the edge. Default 56."),
    ],
    execute=_execute_draw_headline,
)

inspect_card_tool = ToolDefinition(
    name="inspect_card",
    description="Look at the card as it stands. Returns the image. Call it after placing "
                "things, and fix anything unreadable or crowded before finishing.",
    parameters=[],
    execute=_execute_inspect_card,
)

finish_card_tool = ToolDefinition(
    name="finish_card",
    description="Accept the card and stop. Call this once it reads well.",
    parameters=[],
    execute=_execute_finish_card,
)

OG_CARD_TOOLS = [make_plate_tool, place_logo_tool, draw_headline_tool,
                 inspect_card_tool, finish_card_tool]


AGENT_PERSONA = """\
You assemble one Open Graph social card: the picture someone sees when a link to \
a product is shared in WhatsApp, LinkedIn, Slack or Teams. The card is 1200x630.

You have a background renderer, a logo compositor and a text drawer, and you can \
look at what you have made.

How to work:
1. `make_plate` with a brief for the BACKGROUND only. Never ask the renderer for \
words or for a logo: it redraws a logo wrongly and garbles lettering. Leave calm \
space where you intend to put things.
2. `place_logo` if a logo was supplied. It is composited at its real pixels.
3. `draw_headline` if the person asked for any words on the card. Keep it to a \
few words; this is a card, not a paragraph.
4. `inspect_card` and look. Is the mark clear of the edges? Is the text readable \
against what is actually behind it? Fix it if not, then look again.
5. `finish_card`.

Judgement:
- Consumers crop this card. Keep everything important away from the edges.
- Choose the text colour against the plate you actually rendered, not the one \
you imagined.
- If no logo was supplied, do not invent one. Say so plainly at the end rather than quietly producing a card without the mark they asked for.
- Asking for "context about the app", "what it does", a tagline, a headline or a message IS asking for words. Draw them with `draw_headline`, taken from what the site does, kept to a handful of words. Only a person who asked for no text, or who described a picture and nothing else, gets a card with no words on it.
- Two rounds of correction is plenty. Finish."""


def build_og_card_context():
    from app.core.context import BaseContext

    ctx = BaseContext(doc_paths=[], static_prefix=AGENT_PERSONA)
    ctx._cached_static_text = ctx._static_prefix
    return ctx


def _appbuilder_provider() -> str | None:
    from app.config import settings

    return getattr(settings, "APPBUILDER_PROVIDER", None) or None


class _QuietCardStream(AgentEventStream):
    """Swallows the assembling chatter. This runs inside one HTTP request, and
    nobody is watching a stream for it."""

    def __init__(self) -> None:
        # `super().__init__()` IS called, unlike the sub-agent stream in
        # `creative.py` that this otherwise mirrors. The loop reaches into
        # `drain_steers` at every turn boundary, and that reads `_steers`, so
        # skipping the constructor makes the FIRST turn die on an AttributeError
        # and the whole assembly silently fall back to a plain plate.
        super().__init__()
        self.errors: list[str] = []

    @property
    def is_cancelled(self) -> bool:
        return False

    def cancel(self) -> None:
        return

    async def emit_text(self, *a, **kw) -> None: return
    async def emit_thinking(self, *a, **kw) -> None: return
    async def emit_tool_start(self, *a, **kw) -> None: return
    async def emit_tool_update(self, *a, **kw) -> None: return
    async def emit_tool_result(self, *a, **kw) -> None: return
    async def emit_data(self, *a, **kw) -> None: return
    async def emit_complete(self, *a, **kw) -> None: return
    async def emit_error(self, message: str) -> None:
        # WARNING, not debug. The agent loop catches its own failures and emits
        # them here rather than raising, so a stream that swallows this at debug
        # turns every agent fault into "assembled nothing" with no reason
        # anywhere. That is how a 401 on the provider key presented.
        logger.warning("og card agent error: %s", str(message)[:400])
        self.errors.append(str(message)[:400])
    async def emit_done(self, *a, **kw) -> None: return
    async def emit_keepalive(self) -> None: return
    async def emit_suggestions(self, *a, **kw) -> None: return
    async def emit_craft(self, *a, **kw) -> None: return
    async def emit_craft_text(self, *a, **kw) -> None: return
    async def emit_feedback_request(self, *a, **kw) -> None: return
    async def emit_agent_started(self, *a, **kw) -> None: return
    async def emit_agent_finished(self, *a, **kw) -> None: return


class OgCardAgent(BaseAgentBase):
    """A bounded assembler. Few turns, deterministic tools, no writes anywhere.

    `build_tool_context` is the whole reason this is a class: every tool needs
    the same working directory, the same card path and the same fetched logos,
    and threading those through the model's own words would mean the model could
    get them wrong.
    """

    def __init__(self, run: dict[str, Any]) -> None:
        super().__init__(
            name="ogcard",
            tools=OG_CARD_TOOLS,
            context_builder=build_og_card_context(),
            model_tier="balanced",
            # The appbuilder's provider, not the global default. This is the
            # appbuilder assembling a card for an app it is editing, and the two
            # are configured separately -- the same reason `version_diff` binds
            # to `APPBUILDER_PROVIDER`. It also has to SEE: `inspect_card` hands
            # back an image, and a text-only backend makes that tool useless.
            provider=_appbuilder_provider(),
            # Plate, logo, headline, look, one correction, finish. A card that
            # needs more than this is one the person should describe better, and
            # an unbounded loop here spends real money on image renders.
            max_turns=12,
            max_tokens=4096,
        )
        self._run = run

    def build_tool_context(self, session) -> dict:
        ctx = super().build_tool_context(session)
        ctx[CTX] = self._run
        return ctx


async def assemble_card(
    *,
    instruction: str,
    render,
    logos: list[bytes],
    work_dir: str,
    auth,
) -> tuple[str | None, list[str], str, str]:
    """Run the agent. Returns (card_path, warnings, provider, model).

    Never raises for a model problem: a card is worth having even when the
    assembling went sideways, and the caller can still publish whatever the
    plate ended up as. Only a missing plate is a real failure.
    """
    from app.core.session import BaseSession

    run: dict[str, Any] = {"work_dir": work_dir, "render": render, "logos": logos,
                           "warnings": []}
    agent = OgCardAgent(run)
    session = BaseSession(agent_name="ogcard")
    await session.get_or_create(None, auth)
    stream = _QuietCardStream()

    try:
        await agent.run(user_message=instruction, session=session, event_stream=stream)
    except Exception as e:  # noqa: BLE001
        logger.warning("og card assembly failed: %s: %s", type(e).__name__, str(e)[:200])
        run.setdefault("warnings", []).append(
            f"the assembling step stopped early ({type(e).__name__}: {str(e)[:120]})")

    for err in stream.errors:
        run.setdefault("warnings", []).append(f"the assembling step failed: {err}")

    if not run.get("card"):
        # The agent loop swallows its own errors into the event stream, so an
        # empty result arrives here with nothing said. Without this the caller
        # reports "assembled nothing" and no reason, which is what a 401 on the
        # provider key looked like from the outside.
        logger.warning("og card assembly produced no plate for: %s", instruction[:120])

    return (run.get("card"), run.get("warnings") or [],
            run.get("provider", ""), run.get("model", ""))
