"""save_uploaded_assets — ingest user-uploaded images as campaign assets.

v9 I-0 fix. Before this, the asset-upload prompt invited a logo/creative
upload but nothing persisted it: the image only reached the LLM as a vision
block and was dropped. The chat /chat handler now stashes raw uploads on
``session.context["_pending_uploads"]``; this tool reuses the picker's
``upload_and_analyze`` to push the bytes to the CDN and writes the same
``product_data`` keys the picker + launch read (logo_url/creative_images),
so the asset shows in the craft panel and reaches launch_campaign.

The LLM picks the ``role`` (it sees the image and knows what's missing) —
code gathers the bytes, the model decides the slot.
"""

from __future__ import annotations

import base64
import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import upload_and_analyze
from app.agents.adzump.agents.product.tools.scrape.receipts import _emit_asset_receipts

logger = logging.getLogger(__name__)

_CREATIVE_ROLES = ("hero", "amenity", "floor_plan")


async def _save_uploaded_assets(params: dict, context: dict) -> ToolResult:
    role = (params.get("role") or "").strip().lower()
    if role not in ("logo",) + _CREATIVE_ROLES:
        return ToolResult(success=False, error=(
            "role is required — one of: logo, hero, amenity, floor_plan. "
            "Look at the uploaded image and pick the slot it fills."
        ))

    session = context.get("_session")
    sctx = session.context if session else (context.get("session_context") or {})
    pending = sctx.get("_pending_uploads") or []
    if not pending:
        return ToolResult(success=False, error=(
            "No uploaded image is pending. Ask the user to attach the image first."
        ))

    product_data = sctx.setdefault("product_data", {})
    kind = "logo" if role == "logo" else "creative"
    hints = {"fit": "contain"} if kind == "logo" else {"fit": "cover"}

    uploaded: list[dict] = []
    for up in pending:
        try:
            raw = base64.b64decode(up.get("data") or "")
        except Exception:
            continue
        if not raw:
            continue
        res = await upload_and_analyze(
            raw, up.get("mime") or "image/png",
            f"user_upload:{up.get('name', '')}", kind, context, hints=hints,
        )
        if res:
            uploaded.append(res)

    sctx["_pending_uploads"] = []  # consumed — don't re-ingest on the next turn
    if not uploaded:
        return ToolResult(success=False, error=(
            "Upload failed (couldn't store the image). Ask the user to try again."
        ))

    if role == "logo":
        urls = [r["url"] for r in uploaded]
        displays = [{k: v for k, v in r.items() if k != "url"} for r in uploaded]
        # User upload wins — they're explicitly correcting the picker.
        product_data["logo_urls"] = urls
        product_data["logo_displays"] = displays
        product_data["logo_meta"] = [{
            "source_url": "user_upload", "source": "user_upload",
            "role": "main", "reasoning": "User-uploaded logo",
            "format": r.get("format", ""),
        } for r in uploaded]
        product_data["logo_url"] = urls[0]
        product_data["logo_display"] = displays[0]
        product_data["logo_source_url"] = "user_upload"
        product_data["logo_source"] = "user_upload"
        product_data["logo_reasoning"] = "User-uploaded logo"
        product_data["logo_confidence"] = 1.0  # user-provided = max confidence
        # The logo gap is now filled — drop it from the decline signal so the
        # asset prompt doesn't re-ask for a logo on a later turn.
        sig = product_data.get("_shift3_signal")
        if isinstance(sig, dict):
            sig["logo_missing"] = False
        summary = f"Saved {len(urls)} uploaded logo(s) as the brand logo."
    else:
        creative_urls = product_data.setdefault("creative_images", [])
        creative_displays = product_data.setdefault("creative_displays", [])
        seen = set(creative_urls)
        added = 0
        for r in uploaded:
            if r["url"] in seen:
                continue
            creative_urls.append(r["url"])
            creative_displays.append({k: v for k, v in r.items() if k != "url"})
            seen.add(r["url"])
            added += 1
        # Drop this creative category from the decline signal.
        sig = product_data.get("_shift3_signal")
        if isinstance(sig, dict):
            sig["creative_missing_categories"] = [
                c for c in (sig.get("creative_missing_categories") or []) if c != role
            ]
        summary = f"Saved {added} uploaded {role} image(s)."

    stream = context.get("event_stream")
    craft_id = sctx.get("craft_id", "")
    primary_url = (sctx.get("product_profile") or {}).get("url") or product_data.get("url", "")
    if stream and craft_id:
        try:
            await _emit_asset_receipts(stream, craft_id, primary_url, product_data)
        except Exception:
            logger.exception("asset_receipts_emit_failed after upload role=%s", role)

    logger.info("save_uploaded_assets: role=%s count=%d", role, len(uploaded))
    return ToolResult(success=True, data={"role": role, "count": len(uploaded)}, summary=summary)


save_uploaded_assets = ToolDefinition(
    name="save_uploaded_assets",
    description=(
        "Persist an image the user just uploaded as a campaign asset. Call this "
        "whenever the user attaches an image in response to the asset-upload "
        "request (or any time they share a logo/photo for the ad). Pick `role` "
        "by looking at the image and what's missing: a brand mark → 'logo'; a "
        "main building/render shot → 'hero'; a lifestyle/amenity photo → "
        "'amenity'; a floor plan → 'floor_plan'. The uploaded image is read from "
        "the pending-upload stash; you don't pass the file. A 'logo' upload "
        "replaces any auto-detected logo (the user is correcting the picker)."
    ),
    display_name="Save Uploaded Asset",
    parameters=[
        ToolParameter(
            name="role", type="string",
            description="Which slot the uploaded image fills.",
            enum=["logo", "hero", "amenity", "floor_plan"],
        ),
    ],
    execute=_save_uploaded_assets,
)

ASSET_UPLOAD_TOOLS = [save_uploaded_assets]
