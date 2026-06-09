"""LLM-driven, content-based selection of product logo + ad-creative images.

The HTML parser + Playwright network capture together produce a candidate
list of image URLs. This module:

1. Pre-filters the list by source quality and caps to TOP_N.
2. Fetches each candidate in parallel and downscales to a 256px thumbnail.
3. Sends the product summary + the thumbnails (as vision input) to an LLM,
   which picks logo + up to 5 creatives based on what each image *depicts*,
   not what its filename suggests.
4. Returns ProductAssets (URLs the LLM picked) plus the pre-fetched bytes
   for those picks — so the persister can upload them directly without
   re-downloading.

This is content-grounded selection: the LLM sees actual image content +
the business summary, so it rejects testimonial cards / awards / icons
even when their filenames sound product-like.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from io import BytesIO
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.adzump.agents.product.models import (
    CreativeCompleteness,
    CreativeRole,
    LogoPick,
    PageContent,
    ProductAssets,
    SiteImage,
)
from app.config import settings

logger = logging.getLogger(__name__)

# Cap candidates sent to the LLM. Each one is ~1.5K input tokens at low detail.
TOP_N_CANDIDATES = 25

# Hard limits for candidate fetching.
FETCH_TIMEOUT_S = 8.0
MAX_BYTES_PER_IMAGE = 5 * 1024 * 1024
MIN_USEFUL_BYTES = 2 * 1024  # below this is almost always a decorative icon

# LLM-input thumbnail size — small enough to keep token cost low but big
# enough that a vision model can read the gist (testimonial vs villa vs award).
THUMB_LONG_EDGE = 256
THUMB_JPEG_QUALITY = 75

# Source-quality priority for pre-filtering when we have more candidates than TOP_N.
_SOURCE_PRIORITY = {
    "jsonld": 0,   # explicit Organization.logo / Product.image — strongest signal
    "og": 1,       # author-curated share image / logo
    "img": 2,      # DOM image with alt/class context
    "picture": 3,  # responsive image (largest source)
    "link": 4,     # icon / apple-touch-icon — useful as logo fallback
    "network": 5,  # captured at browser network layer — high recall, low metadata
}


class _LogoChoice(BaseModel):
    """One brand-logo selection in the LLM's JSON output. Carries the
    candidate INDEX, not the URL — `_resolve` maps idx → URL and builds the
    public `models.LogoPick` from this. Don't confuse the two: this is the
    wire shape the model fills, `LogoPick` is the resolved domain shape."""
    idx: int = Field(description="Index into the candidate list.")
    role: str = Field(
        default="",
        description="Short label for this brand mark — 'developer', 'project', 'cobrand', or 'main' if singular. Leave blank when unsure.",
    )
    reasoning: str = Field(
        default="",
        description="One short sentence: why this image is the named brand's logo.",
    )
    background_hint: str = Field(
        default="",
        description=(
            "UI tile contrast hint based on the thumbnail you're looking at. "
            "'dark' if the logo is mostly light/white (wordmark designed for a dark header — "
            "needs a dark tile to read). 'light' if the logo is mostly dark (designed for a "
            "white header — needs a light tile). Empty string ('') if the logo has its own "
            "non-transparent background, or if the color is mid-tone, or if you have no "
            "thumbnail (SVG-only candidates). Only the thumbnail tells you this — never guess "
            "from the filename."
        ),
    )


class _AssetSelection(BaseModel):
    """Schema the LLM fills. Indices into the candidate list."""
    logos: list[_LogoChoice] = Field(
        default_factory=list,
        max_length=3,
        description="Brand logos visible in the candidates. Most sites have one; some (real estate, franchise, parent+sub-brand) display two co-equal marks — a developer/parent brand and a project/product brand. Return both when both are clearly present, developer first. Default to one. Return [] if no brand logo qualifies — never pick a hero photo, payment badge, or partner mark as a logo.",
    )
    creative_idxs: list[int] = Field(
        default_factory=list,
        description="Indices of images that genuinely depict the product/service the business sells, ranked best first. Return as many as are good — don't pad with weak picks, don't cap at an arbitrary number. A site with rich photography may yield 8–10; a sparse site may yield 2–3.",
    )
    confidence: float = Field(
        default=0.0,
        description="Self-assessed precision on the logo picks, 0.0 to 1.0.",
    )
    note: str = Field(
        default="",
        description="When logos=[] OR a candidate that LOOKS like a brand logo was deliberately rejected, write ONE short sentence explaining why. Leave empty otherwise. Helps debug why a logo wasn't picked.",
    )


_PROMPT_PATH = Path(__file__).parent / "prompts" / "product_assets.txt"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


# Header-region threshold: an image whose rendered top is within this many pixels
# of the viewport top, with logo-sized dimensions, is treated as a header logo
# even when the DOM has no <header>/<nav> tag (Wix/Webflow/custom framework case).
_HEADER_REGION_TOP_PX = 150
# Logo-shaped bounding box: wide enough to be a wordmark, narrow enough to not
# be a hero banner. Tuned for typical header brand marks on real-estate sites.
_LOGO_MIN_W, _LOGO_MAX_W = 30, 500
_LOGO_MIN_H, _LOGO_MAX_H = 15, 200


def _is_header_visual(img: SiteImage) -> bool:
    """Image is rendered in the header region with logo-shaped dimensions —
    the visual definition of a header brand mark. Works on framework sites
    that don't use semantic <header>/<nav> tags."""
    t = img.rendered_top
    w = img.rendered_width
    h = img.rendered_height
    if t is None or w is None or h is None:
        return False
    if t > _HEADER_REGION_TOP_PX:
        return False
    if not (_LOGO_MIN_W <= w <= _LOGO_MAX_W and _LOGO_MIN_H <= h <= _LOGO_MAX_H):
        return False
    return True

def _prefilter_candidates(images: list[SiteImage], top_n: int) -> list[SiteImage]:
    """Cap candidate count for the vision LLM. Sorts by source-quality
    priority first (strong-signal sources like jsonld/og/link first), then
    by DOM/insertion order within each priority tier — so the LLM sees
    candidates in roughly the order they appear on the page.

    v9 (2026-05-22, Shift 6): SVG-penalty branch retired. SVGs are filtered
    upstream in html_parser; no SVG candidates reach the prefilter."""
    if len(images) <= top_n:
        return list(images)

    def sort_key(pair: tuple[int, SiteImage]) -> tuple[int, int]:
        idx, img = pair
        return (_SOURCE_PRIORITY.get(img.source, 99), idx)

    indexed = sorted(enumerate(images), key=sort_key)
    kept_idxs = sorted(idx for idx, _ in indexed[:top_n])
    return [images[i] for i in kept_idxs]


def _filenames(urls: list[str], limit: int = 12) -> str:
    """Compact URL list for log lines — strips to filenames, truncates if
    many, so a stage line stays under a screen width."""
    files = [u.rsplit("/", 1)[-1][:50] for u in urls if u]
    if len(files) > limit:
        head = ",".join(files[:limit])
        return f"[{head} +{len(files)-limit}]"
    return f"[{','.join(files)}]"


def _stage(name: str, **fields) -> None:
    """Single-line stage marker for the image-selection pipeline.

    Format: `assets_stage:NAME k=v k=v ...` — uniform prefix makes the
    full journey of an image greppable in production logs."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info("assets_stage:%s %s", name, parts)


def _render_candidate_meta(images: list[SiteImage]) -> str:
    """Per-candidate metadata sent to the LLM. The `file` field carries the
    filename + extension — load-bearing for SVG candidates, which have no
    thumbnail and rely on metadata-only judgment (NavLogo.svg, white-logo.svg,
    cricket-icon.svg all encode meaning in the filename)."""
    rows = []
    for idx, img in enumerate(images):
        src = img.src or ""
        # Just the filename: shorter than full URL, but carries the brand signal
        # most extraction prompts depend on. Truncate aggressively if absurdly long.
        filename = src.rsplit("/", 1)[-1].split("?", 1)[0][:80]
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        fmt = ext if ext in {"svg", "png", "jpg", "jpeg", "webp", "gif", "avif", "ico"} else ""
        rows.append({
            "idx": idx,
            "source": img.source,
            "file": filename,
            "format": fmt,
            "alt": (img.alt or "")[:80],
            "title": (img.title or "")[:40],
            "in_header": img.in_header,
            "in_footer": img.in_footer,
            "in_nav": img.in_nav,
            "header_visual": _is_header_visual(img),
            "rendered_top": img.rendered_top,
            "rendered_w": img.rendered_width,
            "rendered_h": img.rendered_height,
            "class": (img.class_attr or "")[:40],
            "id": (img.id_attr or "")[:30],
        })
    return json.dumps(rows, ensure_ascii=False)


async def select_product_assets(
    page: PageContent,
    summary: str,
    context: dict,
) -> tuple[ProductAssets, dict[str, dict]]:
    """Pick logo + ad-creative images, grounded in the product summary and
    in the actual visual content of each candidate.

    Returns (assets, fetched_bytes). `fetched_bytes` maps source-URL ->
    {'bytes': ..., 'content_type': ...} for each *picked* candidate, so
    the caller can upload directly without re-fetching."""
    from app.agents.adzump.agents.product.scrape_stages import ScrapeStage, stage_emit

    if not page.images:
        _stage("candidates", url=page.url, total=0)
        return ProductAssets(), {}

    by_source: dict[str, int] = {}
    for img in page.images:
        by_source[img.source] = by_source.get(img.source, 0) + 1
    _stage(
        "candidates",
        url=page.url,
        total=len(page.images),
        by_source=by_source,
        urls=_filenames([i.src for i in page.images]),
    )

    candidates = _prefilter_candidates(page.images, TOP_N_CANDIDATES)
    _stage(
        "prefilter",
        kept=len(candidates),
        dropped=len(page.images) - len(candidates),
        urls=_filenames([c.src for c in candidates]),
    )

    # v9 (2026-05-22, Shift 6): SVG filter lives in html_parser._collect_image_candidates.add().
    # Parser yields raster-only candidates by contract; no per-call filter needed here.

    # v4 (2026-05-25, I-1): allocate AssetPicker's own tool_use_id so DISCOVER + SELECT
    # stage events attribute to AssetPicker's row rather than collapsing onto the parent
    # scrape tool's row in the UI. SAVE_LOGO + SAVE_IMG (later, in tools/scrape/assets.py)
    # stay on the parent scrape's tool_use_id — they're post-pick filesystem writes by the
    # scrape tool, not picker work (Kiran's panel-review correction).
    import uuid as _uuid
    asset_picker_tuid = _uuid.uuid4().hex[:12]
    # v6 S2 (2026-05-27): pre-emit agent_started BEFORE DISCOVER stage_emit so
    # the UI has an open span for the tool_update to route to. run() never
    # emits agent_started (caller-owned). See asset-picker-fixes-v6.
    _stream = context.get("event_stream")
    if _stream is not None:
        try:
            from app.core.streaming import current_agent_id as _curr_agent_id
            await _stream.emit_agent_started(
                agent_id="asset_picker",
                label="Asset Picker",
                parent_id=_curr_agent_id.get(),
                parent_tool_use_id=context.get("tool_use_id", ""),
                agent_tool_use_id=asset_picker_tuid,
            )
            context.setdefault("_started_tuids", set()).add(asset_picker_tuid)
        except Exception:
            logger.exception("v6_pre_emit_agent_started_failed agent=asset_picker")
    await stage_emit(context, ScrapeStage.DISCOVER, tool_use_id=asset_picker_tuid, n=len(candidates))

    # Parallel fetch + downscale. Anything that fails / is too small / isn't
    # an image is dropped here so the LLM only sees real visual content.
    fetched = await _fetch_candidates(candidates)
    available = [c for c in candidates if c.src in fetched]
    dropped_at_fetch = [c.src for c in candidates if c.src not in fetched]
    _stage(
        "fetched",
        available=len(available),
        dropped=len(dropped_at_fetch),
        kept_urls=_filenames([c.src for c in available]),
        dropped_urls=_filenames(dropped_at_fetch),
    )
    if not available:
        return ProductAssets(), {}

    await stage_emit(context, ScrapeStage.SELECT, tool_use_id=asset_picker_tuid)

    # Build the vision message: prompt text + summary + per-candidate thumbs.
    # SVG candidates have no thumbnail (vector — see _fetch_one); they appear
    # as text-only entries and the LLM judges by metadata signals.
    n_svg = sum(1 for c in available if fetched[c.src].get("is_svg"))
    meta_json = _render_candidate_meta(available)
    intro = (
        "Business summary:\n"
        f"{(summary or '(no summary available)').strip()[:2000]}\n\n"
        f"Candidates ({len(available)} total, {n_svg} SVG with no thumbnail, "
        f"in index order):\n"
        f"{meta_json}"
    )
    # Diagnostic: capture the exact metadata the LLM sees. Truncated for log
    # noise control. When picks are unexpectedly empty, this is the first
    # thing to check — the prompt rules are only useful if the data backs them.
    _stage("llm_input_meta", n=len(available), meta=meta_json[:1200])

    # Vision pick runs through AssetPickerAgent (single-shot BaseAgent that
    # wraps the gpt-4o-mini call). The agent handles message construction,
    # Anthropic→OpenAI image-block conversion, JSON parsing, and resolve
    # internally — the caller still owns the safety net + bytes dict.
    if context.get("auth") is None:
        logger.warning("asset_picker_skip_no_auth url=%s", page.url)
        return ProductAssets(), {}
    try:
        from app.agents.adzump.agents.asset_picker import get_asset_picker_agent
        # Shift 2 (2026-05-21): scrape/tool.py stashes the full-page screenshot
        # bytes (downsampled to ≤2000 px long-edge) under this context key. If
        # the adapter failed to capture / the chain is invoked outside the scrape
        # tool, the picker falls back to the v7 candidate-only payload.
        screenshot_b64 = (context.get("full_page_screenshot_b64") or "") if isinstance(context, dict) else ""
        assets = await get_asset_picker_agent().pick(
            candidates=available,
            fetched=fetched,
            summary=summary or "",
            meta_json=meta_json,
            parent_event_stream=context.get("event_stream"),
            parent_tool_use_id=context.get("tool_use_id", ""),
            auth=context["auth"],
            parent_session_context=context.get("session_context"),
            full_page_screenshot_b64=screenshot_b64 or None,
            # v5 (2026-05-25, I-1): bind AssetPicker's row to the same UUID
            # used by DISCOVER + SELECT stage_emit calls above, so the UI can
            # group tool_updates correctly. See asset-picker-fixes-v5.
            agent_tool_use_id=asset_picker_tuid,
            # v6 S2 (2026-05-27): caller pre-emitted agent_started before
            # DISCOVER/SELECT stage_emits so the UI had a span to route to.
            # Tell BaseAgent.run() not to double-emit.
        )
    except Exception as e:
        logger.warning(
            "product_assets_select_failed: %s: %s",
            type(e).__name__, str(e)[:200],
        )
        return ProductAssets(), {}

    # Return bytes only for picked candidates.
    picked_bytes: dict[str, dict] = {}
    for pick in assets.logos:
        if pick.url and pick.url in fetched:
            picked_bytes[pick.url] = {
                "bytes": fetched[pick.url]["bytes"],
                "content_type": fetched[pick.url]["content_type"],
                "thumb_bytes": fetched[pick.url].get("thumb_bytes"),
                "thumb_content_type": fetched[pick.url].get("thumb_content_type"),
            }
    for url in assets.creative_image_urls:
        if url in fetched:
            picked_bytes[url] = {
                "bytes": fetched[url]["bytes"],
                "content_type": fetched[url]["content_type"],
                "thumb_bytes": fetched[url].get("thumb_bytes"),
                "thumb_content_type": fetched[url].get("thumb_content_type"),
            }
    return assets, picked_bytes


def _resolve(sel: _AssetSelection, candidates: list[SiteImage]) -> ProductAssets:
    n = len(candidates)

    # Resolve logo picks (dedup by URL; cap at 3 — schema also bounds this).
    logo_picks: list[LogoPick] = []
    logo_urls_seen: set[str] = set()
    for pick in (sel.logos or [])[:3]:
        if not (0 <= pick.idx < n):
            continue
        chosen = candidates[pick.idx]
        if chosen.src in logo_urls_seen:
            continue
        logo_urls_seen.add(chosen.src)
        # Format = file extension from URL (cheap, no content-type sniff needed
        # at this layer; persist step has the upload's real content-type).
        ext = chosen.src.lower().rsplit("?", 1)[0].rsplit(".", 1)[-1]
        fmt = ext if ext in {"svg", "png", "jpg", "jpeg", "webp", "gif", "avif"} else ""
        if fmt == "jpeg":
            fmt = "jpg"
        bg = (pick.background_hint or "").strip().lower()
        if bg not in ("light", "dark"):
            bg = ""
        logo_picks.append(LogoPick(
            url=chosen.src,
            source=chosen.source,
            role=(pick.role or "").strip()[:32],
            reasoning=(pick.reasoning or "").strip()[:200],
            format=fmt,
            background=bg,
        ))

    # Apply the safety net (dedup + logo-filename guard) on creative picks.
    # Shift 2 (post-v7): prefer the new per-candidate `creatives` field
    # (list of CreativeChoice with role enum). If absent, fall back to the
    # v6/v7 `creative_idxs` shape. Track per-candidate role through the
    # pipeline so downstream ad-assembly knows which URL is the hero,
    # amenity, floor_plan without re-classifying.
    creative_urls: list[str] = []
    creatives_with_role: list[CreativeRole] = []
    dropped_by_safety: list[str] = []
    seen: set[str] = set(logo_urls_seen)

    use_role_shape = bool(getattr(sel, "creatives", None))
    role_iter = (
        [(c.idx, (c.role or "").strip().lower(), (c.reasoning or "").strip()) for c in sel.creatives]
        if use_role_shape
        else [(i, "", "") for i in (sel.creative_idxs or [])]
    )
    for idx, role, reasoning in role_iter:
        if not (0 <= idx < n):
            continue
        chosen = candidates[idx]
        url = chosen.src
        if not url or url in seen:
            continue
        if _filename_suggests_logo(url):
            dropped_by_safety.append(url)
            continue
        # 'unused' = model considered it but rejected as ad-usable; skip the
        # back-compat URL list but keep the role record so v8 can still see
        # what the model rejected and why (FYI for v9 calibration).
        if role and role not in {"hero", "amenity", "floor_plan", "unused"}:
            # Unknown role label — keep but normalize to '' so the
            # downstream consumer treats it as a generic creative.
            role = ""
        if role != "unused":
            creative_urls.append(url)
        creatives_with_role.append(CreativeRole(
            url=url,
            role=role,
            reasoning=reasoning[:200],
        ))
        seen.add(url)

    # Derive creative_completeness from per-candidate roles (policy in code,
    # perception in model — Kiran's Q3 pick). When the model returned the
    # legacy creative_idxs shape, all picks are role="" and verdict is
    # 'needs_upload' (we don't know what's missing). v8 prompt should
    # always emit roles, so this fallback is defensive.
    hero_found = any(c.role == "hero" for c in creatives_with_role)
    amenities_count = sum(1 for c in creatives_with_role if c.role == "amenity")
    floor_plan_found = any(c.role == "floor_plan" for c in creatives_with_role)
    if hero_found and amenities_count >= 1:
        verdict = "complete"
    elif hero_found or amenities_count >= 1:
        verdict = "partial"
    else:
        verdict = "needs_upload"
    # v9 I-8 fix: missing_categories must agree with the 'complete' bar
    # (complete = hero AND >=1 amenity — see CreativeCompleteness). floor_plan
    # is tracked (floor_plan_found) but is NOT required for launch-readiness, so
    # it must not appear in missing_categories — otherwise a 'complete' campaign
    # still surfaces a "missing floor plan" ask. (Rejected the inverse fix —
    # requiring floor_plan for 'complete' — because floor plans rarely live on
    # marketing sites, so it would leave most real-estate campaigns perpetually
    # 'needs_upload'.)
    missing = []
    if not hero_found: missing.append("hero")
    if amenities_count < 1: missing.append("amenity")
    creative_completeness = CreativeCompleteness(
        hero_found=hero_found,
        amenities_count=amenities_count,
        floor_plan_found=floor_plan_found,
        verdict=verdict,
        missing_categories=missing,
    )

    _stage(
        "llm_pick",
        logos=_filenames([p.url for p in logo_picks]) if logo_picks else "[none]",
        logo_roles=",".join(p.role or "main" for p in logo_picks) or "(none)",
        conf=f"{float(sel.confidence or 0.0):.2f}",
        creative_count=len(creative_urls),
        creatives=_filenames(creative_urls),
        reason=(logo_picks[0].reasoning if logo_picks else "")[:120],
        note=(sel.note or "")[:200],
    )
    if dropped_by_safety:
        _stage(
            "safety_filter",
            dropped=len(dropped_by_safety),
            reason="logo_filename",
            urls=_filenames(dropped_by_safety),
        )

    conf = max(0.0, min(1.0, float(sel.confidence or 0.0)))
    return ProductAssets(
        logos=logo_picks,
        creative_image_urls=creative_urls,
        creatives_with_role=creatives_with_role,
        creative_completeness=creative_completeness,
        confidence=conf,
        note=(sel.note or "").strip()[:300],
    )


_LOGO_FILENAME_TOKENS = ("logo", "wordmark", "brandmark", "monogram")


def _filename_suggests_logo(url: str) -> bool:
    """True if the URL's filename strongly indicates a logo. Used only as a
    post-LLM-pick safety net for the 'creative' bucket — content-based
    selection remains primary; this catches the narrow case where the
    vision pass let a sub-brand wordmark through (e.g. `clublogo.png`)."""
    filename = url.rsplit("/", 1)[-1].lower()
    name_part = filename.rsplit(".", 1)[0]
    return any(tok in name_part for tok in _LOGO_FILENAME_TOKENS)


# ─── Candidate fetch + downscale ─────────────────────────────────────────


async def _fetch_candidates(candidates: list[SiteImage]) -> dict[str, dict]:
    """Fetch + downscale each candidate URL in parallel. Returns a dict
    keyed by source URL of {bytes, content_type, data_url} for every
    candidate that resolved to real, large-enough image content."""
    import httpx

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S, follow_redirects=True
    ) as client:
        tasks = [_fetch_one(client, c.src) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, dict] = {}
    for cand, result in zip(candidates, results):
        if isinstance(result, Exception) or result is None:
            continue
        out[cand.src] = result
    return out


async def _fetch_one(client, url: str) -> dict | None:
    """Download one candidate; downscale to a JPEG thumbnail when possible.

    SVGs are kept as candidates with no thumbnail — PIL can't open them and
    we don't want a Cairo system dependency. The LLM judges SVG candidates
    by their text metadata (in_header / in_nav / alt / class / filename),
    which is enough to discriminate brand logos from decorative icons.

    Recovers from missing content-type by falling back to the URL extension."""
    try:
        resp = await client.get(url)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    from app.agents.adzump._shared import (
        looks_like_image_response, _guess_ctype_from_url,
    )
    raw_ctype = resp.headers.get("content-type") or ""
    if not looks_like_image_response(raw_ctype, url):
        return None
    ctype = (raw_ctype or _guess_ctype_from_url(url)).lower().split(";", 1)[0].strip()
    data = resp.content
    if not data or len(data) > MAX_BYTES_PER_IMAGE:
        return None

    is_svg = "svg" in ctype
    # SVGs are vector — byte size is unrelated to visual size, so the min-bytes
    # filter would drop legitimate logos. Apply the floor only to raster formats.
    if not is_svg and len(data) < MIN_USEFUL_BYTES:
        return None

    if is_svg:
        return {
            "bytes": data,
            "content_type": ctype,
            "data_url": None,
            "thumb_bytes": None,
            "thumb_content_type": None,
            "is_svg": True,
        }

    thumb_bytes = _downscale_to_jpeg_bytes(data, ctype)
    if not thumb_bytes:
        return None
    data_url = f"data:image/jpeg;base64,{base64.b64encode(thumb_bytes).decode('ascii')}"
    return {
        "bytes": data,
        "content_type": ctype,
        "data_url": data_url,
        "thumb_bytes": thumb_bytes,
        "thumb_content_type": "image/jpeg",
        "is_svg": False,
    }


# Shift 2 (2026-05-21): full-page screenshot resampling. Pages can render
# 5000–15000 px tall; we cap at 2000 px long-edge so the vision LLM input
# stays predictable and the storage upload doesn't blow up. Decided via the
# grilling session — Q1: "scaled full-page" (≤ 2000 px) was the user pick.
SCREENSHOT_LONG_EDGE = 2000
SCREENSHOT_JPEG_QUALITY = 75


def _downscale_screenshot_to_jpeg_bytes(image_bytes: bytes) -> bytes | None:
    """Resample a full-page screenshot so its long edge ≤ SCREENSHOT_LONG_EDGE.
    Accepts JPEG/PNG bytes; emits JPEG. No mode coercion needed — Playwright
    emits RGB JPEG with no alpha channel.
    """
    try:
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > SCREENSHOT_LONG_EDGE:
            img.thumbnail((SCREENSHOT_LONG_EDGE, SCREENSHOT_LONG_EDGE))
        out = BytesIO()
        img.save(out, "JPEG", quality=SCREENSHOT_JPEG_QUALITY)
        return out.getvalue()
    except Exception:
        return None


def _downscale_to_jpeg_bytes(image_bytes: bytes, content_type: str) -> bytes | None:
    """Resize the image so its long edge is THUMB_LONG_EDGE; return JPEG bytes.
    Composites transparent backgrounds onto white so the vision LLM (and the
    UI thumbnail tile) see the actual image, not the alpha channel."""
    try:
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert("RGB")

        if max(img.size) > THUMB_LONG_EDGE:
            img.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))

        out = BytesIO()
        img.save(out, "JPEG", quality=THUMB_JPEG_QUALITY)
        return out.getvalue()
    except Exception:
        return None
