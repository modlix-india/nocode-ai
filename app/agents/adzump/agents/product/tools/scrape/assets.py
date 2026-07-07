"""Asset selection, upload, and craft-panel receipts for the ProductAgent.

`_emit_asset_receipts` replaces the `assets_label` and `assets_row`
placeholders that `scrape_profile._generate_business_profile` seeds in the
initial layout. The layout emit MUST happen before the first receipt emit,
or the id-based merge targets non-existent blocks and receipts silently
drop. Ordering is guaranteed by `_scrape_url`'s call sequence in scrape.py.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.product.models import AssetRequirements, ProductAssets
from app.agents.adzump.agents.product.product_assets import select_product_assets
from app.agents.adzump.models import Image, Logo
from .receipts import _emit_asset_receipts
from app.agents.adzump.agents.product.scrape_stages import ScrapeStage, stage_emit
from app.agents.adzump._uploads import rehost_image, upload_and_analyze

logger = logging.getLogger(__name__)


async def _select_and_persist_primary_assets(
    page,
    business_profile: str,
    product_data: dict,
    context: dict,
    stream,
    craft_id: str,
    url: str,
) -> None:
    """LLM-pick logo + creatives using business_profile as context, upload,
    emit receipts. The DISCOVER stage emit fires inside select_product_assets
    once candidates are fetched."""
    logger.info(
        "product_assets_select_start: url=%s candidates=%d",
        url, len(page.images),
    )
    try:
        selected_assets, prefetched = await select_product_assets(
            page, business_profile, context
        )
    except Exception as e:
        logger.warning(
            "product_assets_call_failed: url=%s err=%s",
            url, str(e)[:200],
        )
        selected_assets, prefetched = ProductAssets(), {}
    logger.info(
        "product_assets_select_done: url=%s logo=%s conf=%.2f creatives=%d source=%s",
        url, bool(selected_assets.logo_url), selected_assets.confidence,
        len(selected_assets.creative_image_urls), selected_assets.logo_source,
    )
    await _persist_product_assets(
        selected_assets, prefetched, product_data, context,
        stream=stream, craft_id=craft_id, primary_url=url,
    )
    # The asset-upload chat-prompt is emitted at the AdPilot orchestrator layer
    # (tools/product.py · _analyze_product), not here - _PassthroughEventStream
    # drops emit_text from the sub-agent's stream. _persist_product_assets
    # stashed the requirements for the return; the parent reads
    # AnalysisOutput.asset_requirements and emits to the user-visible chat.
    # See plans/asset-gaps-refactor.html.


async def _update_assets_from_extra_page(
    page,
    product_data: dict,
    context: dict,
    url: str,
) -> None:
    """Same-host non-primary scrape: silently refine assets using the stored
    summary. No craft re-emit - panel is already populated from the primary."""
    session_ctx = context.get("session_context") or {}
    existing_summary = (session_ctx.get("product_profile") or {}).get("summary") or ""
    try:
        selected_assets, prefetched = await select_product_assets(
            page, existing_summary, context
        )
        await _persist_product_assets(
            selected_assets, prefetched, product_data, context
        )
    except Exception as e:
        logger.warning(
            "product_assets_refine_failed: url=%s err=%s",
            url, str(e)[:200],
        )


async def _persist_product_assets(
    assets: ProductAssets,
    prefetched: dict[str, dict],
    product_data: dict,
    context: dict,
    stream=None,
    craft_id: str = "",
    primary_url: str = "",
) -> None:
    """Persist LLM-picked logos + creatives.

    Logos: higher-confidence later turn replaces the prior set wholesale.
    Creatives: accumulate, deduped by URL. Reuses the selector's prefetched
    bytes when available (skips refetch) - see `_upload_picked_image`.
    """
    logo_outcome = await _persist_logos(
        assets, prefetched, product_data, context, stream, craft_id, primary_url,
    )
    added, skipped_dup, skipped_fail, rehosted_filenames = await _persist_creatives(
        assets, prefetched, product_data, context, stream, craft_id, primary_url,
    )
    logger.info(
        "assets_stage:persisted logo=%s creatives_added=%d skipped_dup=%d skipped_fail=%d total_images=%d rehosted=[%s]",
        logo_outcome, added, skipped_dup, skipped_fail,
        len((product_data.get("assets") or {}).get("images") or []),
        ",".join(rehosted_filenames),
    )
    # Popped by _parse_result onto AnalysisOutput.asset_requirements. Stored as
    # a dict: save_context json.dumps the context before _parse_result runs.
    cc = getattr(assets, "creative_completeness", None)
    (context.get("session_context") or {})["_asset_requirements"] = AssetRequirements(
        logo_missing=not assets.logos,
        missing_categories=list(getattr(cc, "missing_categories", []) or []),
        verdict=getattr(cc, "verdict", ""),
    ).to_dict()


async def _persist_logos(
    assets: ProductAssets,
    prefetched: dict[str, dict],
    product_data: dict,
    context: dict,
    stream,
    craft_id: str,
    primary_url: str,
) -> str:
    """Upload picked logos and write assets.logos. Returns log outcome."""
    if not assets.logos:
        return "skip_no_pick"
    assets_state = product_data.setdefault("assets", {})
    existing = assets_state.get("logos") or []
    existing_conf = float(existing[0].get("confidence") or 0.0) if existing else 0.0
    if assets.confidence < existing_conf:
        return f"skip_lower_conf(new={assets.confidence:.2f}<existing={existing_conf:.2f})"

    new_logos: list[dict] = []
    for pick in assets.logos:
        await stage_emit(context, ScrapeStage.SAVE_LOGO)
        # Hints sourced from the vision LLM that picked this logo (background
        # only); fit is constant per-kind - logos are almost always wider than
        # tall and read best contained inside the tile, not cropped.
        hints = {"fit": "contain"}
        if pick.background in ("light", "dark"):
            hints["background"] = pick.background
        # name from the vision pick's role: developer/project → <role>-logo,
        # singular/unknown → plain "logo".
        name = f"{pick.role}-logo" if pick.role in ("developer", "project") else "logo"
        rehosted = await _upload_picked_image(pick.url, "logo", prefetched, context, hints=hints, name=name)
        if not rehosted:
            continue
        # `format` from the upload's actual content-type beats LogoPick.format's
        # URL-extension guess. Fall back to the pick when the upload omits it.
        upload_format = rehosted.get("format") or pick.format or ""
        new_logos.append(Logo(
            url=rehosted["url"],
            display={k: v for k, v in rehosted.items() if k != "url"},
            source=pick.source,
            source_url=pick.url,
            role=pick.role,
            reasoning=pick.reasoning,
            format=upload_format,
            # Batch-level: one vision call picked all of this run's logos.
            confidence=assets.confidence,
        ).model_dump())
        logger.info(
            "stage=scrape.select scrape_id=%s logo_role=%s logo_format=%s logo_url=%s reasoning=%r",
            context.get("scrape_id", ""),
            pick.role or "main",
            upload_format or "unknown",
            rehosted["url"],
            pick.reasoning[:120],
        )
        await _emit_asset_receipts(
            stream, craft_id, primary_url,
            product_data | {"assets": assets_state | {"logos": new_logos}},
        )

    if not new_logos:
        return "rehost_failed"

    assets_state["logos"] = new_logos
    return f"persisted({len(new_logos)})"


async def _persist_creatives(
    assets: ProductAssets,
    prefetched: dict[str, dict],
    product_data: dict,
    context: dict,
    stream,
    craft_id: str,
    primary_url: str,
) -> tuple[int, int, int, list[str]]:
    """Upload picked creatives, dedup by URL. Returns (added, dup, fail, names)."""
    added = 0
    skipped_dup = 0
    skipped_fail = 0
    filenames: list[str] = []
    if not assets.creative_image_urls:
        return added, skipped_dup, skipped_fail, filenames

    images: list[dict] = product_data.setdefault("assets", {}).setdefault("images", [])
    seen = {img.get("url") for img in images}
    total = len(assets.creative_image_urls)
    # Creatives are full-color product photos - no background-tile contrast
    # issue. `cover` fills the tile cleanly without letterboxing.
    creative_hints = {"fit": "cover"}
    # name from the vision role map: image-<role>-<nth of its role>, e.g.
    # image-hero-1, image-amenity-2; plain image-<i> when role is unknown.
    role_by_url = {c.url: c.role for c in (assets.creatives_with_role or [])}
    role_counts: dict[str, int] = {}
    for i, src in enumerate(assets.creative_image_urls, start=1):
        await stage_emit(context, ScrapeStage.SAVE_IMG, i=i, n=total)
        role = role_by_url.get(src, "")
        if role:
            role_counts[role] = role_counts.get(role, 0) + 1
            name = f"image-{role.replace('_', '-')}-{role_counts[role]}"
        else:
            name = f"image-{i}"
        rehosted = await _upload_picked_image(src, "creative", prefetched, context, hints=creative_hints, name=name)
        if not rehosted:
            skipped_fail += 1
            continue
        url = rehosted["url"]
        if url in seen:
            skipped_dup += 1
            continue
        images.append(Image(
            url=url,
            display={k: v for k, v in rehosted.items() if k != "url"},
            role=role,
            source="site_pick",
        ).model_dump())
        seen.add(url)
        added += 1
        filenames.append(url.rsplit("/", 1)[-1][:50])
        await _emit_asset_receipts(stream, craft_id, primary_url, product_data)
    return added, skipped_dup, skipped_fail, filenames


async def _upload_picked_image(
    source_url: str, kind: str, prefetched: dict[str, dict], context: dict,
    hints: dict | None = None, name: str = "",
) -> dict | None:
    """Upload a LLM-picked candidate. Reuses bytes the selector already
    fetched when available, falls back to a fresh network fetch otherwise.

    `hints` (`background`, `fit`) come from the caller - the vision LLM that
    picked this asset is the source of truth, not pixel sampling here. They
    flow through to the stored asset's `display` dict.
    `name` (e.g. "project-logo", "image-hero-1") is the semantic filename the
    caller derived from the pick's role; threaded to both branches so cache
    hit / miss name identically.

    Also uploads a 256px JPEG thumbnail variant (when the selector cached
    one) and attaches its URL as ``thumb_url`` so the receipts row can
    render a small image instead of pulling multi-MB originals into a tile."""
    cached = prefetched.get(source_url)
    if cached:
        result = await upload_and_analyze(
            cached["bytes"], cached["content_type"], source_url, kind, context,
            hints=hints, name=name,
        )
    else:
        result = await rehost_image(source_url, kind, context, hints=hints, name=name)
    if not result:
        return None
    thumb_bytes = (cached or {}).get("thumb_bytes")
    if thumb_bytes:
        # Thumb is a 256px JPEG used as <img src>. Reuse the main asset's hints
        # so the thumb render matches the full asset's tile contrast.
        thumb = await upload_and_analyze(
            thumb_bytes, "image/jpeg", source_url, f"{kind}_thumb", context,
            hints=hints, name=f"{name}-thumb" if name else "",
        )
        if thumb and thumb.get("url"):
            result["thumb_url"] = thumb["url"]
    return result


# ── Shift 3 Stage 1 · upload-request chat prompts ─────────────────────────
#
# The picker may legitimately decline (Shift 4/7 contract: missing > wrong).
# When it does, the receipts panel shows what we DID pick, and this helper
# posts a non-blocking chat message asking the user to upload what we missed.
# Two-stage ask: Stage 2 (the creative agent's hard gate) lives in a future
# cycle - see plans/agent-tracing/asset-picker-fixes-v2.html · "Shift 3 design".

_CREATIVE_KIND_LABEL = {
    "hero": "a hero shot",
    "amenity": "an amenity / lifestyle photo",
    "floor_plan": "a floor plan",
}


def _compose_asset_request_text(missing_logo: bool, missing_creatives: list[str]) -> str:
    """Build the chat prompt body. One combined message - never two prompts
    for one decline. Per memory feedback_adzump_receipts_pattern.md: receipts
    are display-only, corrections via chat prompt. This is the prompt half."""
    parts: list[str] = []
    if missing_logo:
        parts.append(
            "I couldn't auto-detect your **brand logo** from the website. "
            "Share an image of your logo if you have one handy - it'll go "
            "straight to your ad creatives."
        )
    if missing_creatives:
        readable = [_CREATIVE_KIND_LABEL.get(k, k) for k in missing_creatives]
        if len(readable) == 1:
            cats = readable[0]
        elif len(readable) == 2:
            cats = f"{readable[0]} and {readable[1]}"
        else:
            cats = ", ".join(readable[:-1]) + f", and {readable[-1]}"
        verb = "I'm also missing" if missing_logo else "I picked some ad images, but I'm missing"
        parts.append(
            f"{verb} {cats}. Upload one if you'd like - the ad agent will "
            "ask again if it needs one I don't have."
        )
    return "\n\n".join(parts)


# _emit_asset_request_prompt was here, removed in v9 live-test fix 2:
# the sub-agent's _PassthroughEventStream drops emit_text, so this function's
# chat-text never reached the user-visible chat. The emit now lives at the
# AdPilot orchestrator layer in tools/product.py · _analyze_product, which
# has access to the parent stream. _compose_asset_request_text above is
# reused from there. _persist_product_assets stashes the requirements for the
# sub-agent's return (AnalysisOutput.asset_requirements), which the parent
# reads - no re-walking the ProductAssets object.
