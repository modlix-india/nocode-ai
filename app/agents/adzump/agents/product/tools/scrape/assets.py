"""Asset selection, upload, and craft-panel receipts for the ProductAgent.

`_emit_asset_receipts` replaces the `assets_label` and `assets_row`
placeholders that `scrape_profile._generate_business_profile` seeds in the
initial layout. The layout emit MUST happen before the first receipt emit,
or the id-based merge targets non-existent blocks and receipts silently
drop. Ordering is guaranteed by `_scrape_url`'s call sequence in scrape.py.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.product.models import ProductAssets
from app.agents.adzump.agents.product.product_assets import select_product_assets
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
    # Shift 3 Stage 1 chat-prompt is emitted at the AdPilot orchestrator layer
    # (tools/product.py · _analyze_product), not here — _PassthroughEventStream
    # drops emit_text from the sub-agent's stream. _persist_product_assets
    # already stashed the decline signal on product_data["_shift3_signal"];
    # the parent reads it and emits to the user-visible chat. See
    # plans/agent-tracing/v9-live-test-fixes.html · FIX 2.


async def _update_assets_from_extra_page(
    page,
    product_data: dict,
    context: dict,
    url: str,
) -> None:
    """Same-host non-primary scrape: silently refine assets using the stored
    summary. No craft re-emit — panel is already populated from the primary."""
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
    bytes when available (skips refetch) — see `_upload_picked_image`.
    """
    logo_outcome = await _persist_logos(
        assets, prefetched, product_data, context, stream, craft_id, primary_url,
    )
    added, skipped_dup, skipped_fail, rehosted_filenames = await _persist_creatives(
        assets, prefetched, product_data, context, stream, craft_id, primary_url,
    )
    logger.info(
        "assets_stage:persisted logo=%s creatives_added=%d skipped_dup=%d skipped_fail=%d total_creatives=%d rehosted=[%s]",
        logo_outcome, added, skipped_dup, skipped_fail,
        len(product_data.get("creative_images") or []),
        ",".join(rehosted_filenames),
    )
    # v9 live-test fix 2 (2026-05-22): persist the Shift 3 signal so the
    # top-level AdPilot tool wrapper (tools/product.py · _analyze_product) can
    # emit the chat-prompt on the PARENT stream. The sub-agent's
    # _PassthroughEventStream drops emit_text, so we can't fire the prompt
    # from inside the picker layer.
    cc = getattr(assets, "creative_completeness", None)
    product_data["_shift3_signal"] = {
        "logo_missing": not assets.logos,
        "creative_missing_categories": list(getattr(cc, "missing_categories", []) or []),
        "verdict": getattr(cc, "verdict", ""),
    }


async def _persist_logos(
    assets: ProductAssets,
    prefetched: dict[str, dict],
    product_data: dict,
    context: dict,
    stream,
    craft_id: str,
    primary_url: str,
) -> str:
    """Upload picked logos and write the logo_* fields. Returns log outcome."""
    if not assets.logos:
        return "skip_no_pick"
    existing_conf = float(product_data.get("logo_confidence") or 0.0)
    if assets.confidence < existing_conf:
        return f"skip_lower_conf(new={assets.confidence:.2f}<existing={existing_conf:.2f})"

    new_urls: list[str] = []
    new_displays: list[dict] = []
    new_meta: list[dict] = []
    for pick in assets.logos:
        await stage_emit(context, ScrapeStage.SAVE_LOGO)
        # Hints sourced from the vision LLM that picked this logo (background
        # only); fit is constant per-kind — logos are almost always wider than
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
        new_urls.append(rehosted["url"])
        new_displays.append({k: v for k, v in rehosted.items() if k != "url"})
        # `format` from the upload's actual content-type beats LogoPick.format's
        # URL-extension guess. Fall back to the pick when the upload omits it.
        upload_format = rehosted.get("format") or pick.format or ""
        new_meta.append({
            "source_url": pick.url,
            "source": pick.source,
            "role": pick.role,
            "reasoning": pick.reasoning,
            "format": upload_format,
        })
        logger.info(
            "stage=scrape.select scrape_id=%s logo_role=%s logo_format=%s logo_url=%s reasoning=%r",
            context.get("scrape_id", ""),
            pick.role or "main",
            upload_format or "unknown",
            rehosted["url"],
            pick.reasoning[:120],
        )
        await _emit_asset_receipts(stream, craft_id, primary_url, product_data | {
            "logo_urls": new_urls,
            "logo_displays": new_displays,
        })

    if not new_urls:
        return "rehost_failed"

    product_data["logo_urls"] = new_urls
    product_data["logo_displays"] = new_displays
    product_data["logo_meta"] = new_meta
    # Back-compat scalars — primary = first pick.
    product_data["logo_url"] = new_urls[0]
    product_data["logo_display"] = new_displays[0]
    product_data["logo_source_url"] = new_meta[0]["source_url"]
    product_data["logo_source"] = new_meta[0]["source"]
    product_data["logo_reasoning"] = new_meta[0]["reasoning"]
    product_data["logo_confidence"] = assets.confidence
    return f"persisted({len(new_urls)})"


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

    creative_urls: list[str] = product_data.setdefault("creative_images", [])
    creative_displays: list[dict] = product_data.setdefault("creative_displays", [])
    seen = set(creative_urls)
    total = len(assets.creative_image_urls)
    # Creatives are full-color product photos — no background-tile contrast
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
        creative_urls.append(url)
        creative_displays.append({k: v for k, v in rehosted.items() if k != "url"})
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

    `hints` (`background`, `fit`) come from the caller — the vision LLM that
    picked this asset is the source of truth, not pixel sampling here. They
    flow through to the upload's `logo_displays` / `creative_displays` record.
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
# cycle — see plans/agent-tracing/asset-picker-fixes-v2.html · "Shift 3 design".

_CREATIVE_KIND_LABEL = {
    "hero": "a hero shot",
    "amenity": "an amenity / lifestyle photo",
    "floor_plan": "a floor plan",
}


def _compose_asset_request_text(missing_logo: bool, missing_creatives: list[str]) -> str:
    """Build the chat prompt body. One combined message — never two prompts
    for one decline. Per memory feedback_adzump_receipts_pattern.md: receipts
    are display-only, corrections via chat prompt. This is the prompt half."""
    parts: list[str] = []
    if missing_logo:
        parts.append(
            "I couldn't auto-detect your **brand logo** from the website. "
            "Share an image of your logo if you have one handy — it'll go "
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
            f"{verb} {cats}. Upload one if you'd like — the ad agent will "
            "ask again if it needs one I don't have."
        )
    return "\n\n".join(parts)


# _emit_asset_request_prompt was here, removed in v9 live-test fix 2:
# the sub-agent's _PassthroughEventStream drops emit_text, so this function's
# chat-text never reached the user-visible chat. The emit now lives at the
# AdPilot orchestrator layer in tools/product.py · _analyze_product, which
# has access to the parent stream. _compose_asset_request_text above is
# reused from there. _persist_product_assets stashes the decline signal on
# product_data["_shift3_signal"] so the parent can read it without re-walking
# the ProductAssets object.
