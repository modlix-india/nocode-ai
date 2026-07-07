"""manage_assets - the ONE asset front door (replaces save_uploaded_assets).

Design C: the orchestrator hands over; this tool gathers the pending uploads,
runs the VisionAnalyst ONCE (review-each), then CODE dispositions each verdict -
store the confident-relevant, reject the off-product, and escalate the unsure
back to the orchestrator to ask the user. No tool-loop, no completion oracle.
The reviewer decides per image (relevant? role? name? unsure?); code executes.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress

logger = logging.getLogger(__name__)


def _build_brief(sctx: dict, note: str = "") -> str:
    """Context the VisionAnalyst reviews relevance against: what the product is,
    what's already on file, and what the user said about THIS upload (note) - the
    user's own claim is the strongest identity signal we have (PR1)."""
    pd = sctx.get("product_data") or {}
    name = pd.get("product_name") or "(unknown product)"
    summary = (pd.get("summary") or "").strip()
    assets = pd.get("assets") or {}
    logos = len(assets.get("logos") or [])
    images = len(assets.get("images") or [])
    lines = [
        f"Product (these uploads are claimed to be for THIS product): {name}",
        f"Summary: {summary[:1500]}" if summary else "Summary: (none yet)",
        f"Already on file: {logos} logo(s), {images} product image(s).",
    ]
    note = (note or "").strip()
    if note:
        lines.append(f'The user said about these image(s): "{note[:300]}"')
    return "\n".join(lines)


def _saved_summary(stored: list[dict]) -> list[str]:
    """The 'Saved …' receipt line(s) for stored assets + a non-blocking hedge on
    brand-defining assets (hero/logo) the model can't verify are THIS project's
    (PR1a). Pure → unit-tested below the model."""
    if not stored:
        return []

    def _label(d: dict) -> str:  # PR4: drop redundant "(role)" when name == role / empty
        r, n = d.get("role", ""), (d.get("name") or "").strip()
        return f"your {r}" if (not n or n.lower() == r) else f"{n} ({r})"

    parts = ["Saved " + ", ".join(_label(d) for d in stored) + "."]
    brand = sorted({d.get("role") for d in stored if d.get("role") in ("hero", "logo")})
    if brand:
        parts.append(f"If the {' or '.join(brand)} isn't from this project, tell me and I'll swap it.")
    return parts


async def _manage_assets(params: dict, context: dict) -> ToolResult:
    import base64

    session = context.get("_session")
    sctx = session.context if session else (context.get("session_context") or {})
    pending = sctx.get("_pending_uploads") or []
    if not pending:
        return ToolResult(success=False, error=(
            "No uploaded image is pending. Ask the user to attach the image first."
        ))

    auth = context.get("auth")
    if auth is None:
        return ToolResult(success=False, error=(
            "No auth context - the vision reviewer needs auth to run."
        ))

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")

    from app.agents.adzump.agents.vision.agent import get_reviewer
    from app.agents.adzump._asset_store import (
        classify_verdict, dedup_by_content, store_logo, store_image,
    )
    from app.agents.adzump._uploads import upload_and_analyze
    from app.core.streaming import pre_emit_agent_started

    # Decode pending bytes + drop content-duplicates (same paste twice → once).
    images: list[dict] = []
    for up in pending:
        try:
            raw = base64.b64decode(up.get("data") or "")
        except Exception:
            raw = b""
        if raw:
            images.append({"data": raw, "content_type": up.get("mime") or "image/png"})
    images = dedup_by_content(images)
    sctx["_pending_uploads"] = []  # consumed - never re-ingest next turn
    if not images:
        return ToolResult(success=False, error=(
            "Uploaded image bytes are unreadable; ask the user to retry."
        ))

    await emit_progress(context, "Reviewing your uploaded image(s)…")
    if stream:
        # The reviewer emits its own agent_finished; the launcher only opens the card.
        await pre_emit_agent_started(
            stream, agent_id="vision_review", label="Vision Analyst",
            parent_tool_use_id=tool_use_id, context=context,
        )

    reviewed = await get_reviewer().review(
        images=images, parent_event_stream=stream, auth=auth,
        summary=_build_brief(sctx, params.get("note") or ""),
        parent_session_context={
            "url": sctx.get("primary_url") or sctx.get("url", ""),
            "craft_id": sctx.get("craft_id", ""),
        },
    )

    # CODE disposition over the verdicts (model decides, code executes).
    product_data = sctx.setdefault("product_data", {})
    up_ctx = {**context, "session_context": sctx}  # _asset_filename reads product_data here
    stored: list[dict] = []
    rejected: list[dict] = []
    ambiguous: list[dict] = []   # → orchestrator asks the user (slice 4 surfaces these)

    for v in reviewed.verdicts:
        if not (0 <= v.idx < len(images)):
            continue
        img = images[v.idx]
        role = (v.role or "").strip().lower()
        action = classify_verdict(v)
        if action == "store":
            kind = "logo" if role == "logo" else "creative"
            hints = {"fit": "contain" if kind == "logo" else "cover"}
            res = await upload_and_analyze(
                img["data"], img["content_type"], f"user_upload:{v.name}",
                kind, up_ctx, hints=hints, name=v.name,
            )
            if not res:
                ambiguous.append({"idx": v.idx, "question": (
                    f"Upload failed for image {v.idx + 1} - ask the user to retry."
                )})
                continue
            if role == "logo":
                store_logo(product_data, res, v.name, sctx)
            else:
                store_image(product_data, res, role, v.name, sctx)
            stored.append({"role": role, "name": v.name or role})
        elif action == "reject":
            rejected.append({"idx": v.idx, "reason": v.reasoning or "not relevant to the product"})
        else:  # escalate
            ambiguous.append({"idx": v.idx, "question": v.question or f"What is image {v.idx + 1}?"})

    if stream:
        craft_id = sctx.get("craft_id", "")
        if craft_id:
            try:
                from app.agents.adzump.agents.product.tools.scrape.receipts import _emit_asset_receipts
                await _emit_asset_receipts(stream, craft_id, sctx.get("primary_url", ""), product_data)
            except Exception:
                logger.exception("manage_assets: receipts emit failed")

    # User-facing summary (tool-text contract - the orchestrator only adds a lead-in).
    parts: list[str] = []
    parts += _saved_summary(stored)
    if rejected:
        parts.append("Skipped " + ", ".join(f"image {d['idx'] + 1} ({d['reason']})" for d in rejected) + ".")
    if ambiguous:
        parts.append("Need your help on " + ", ".join(
            f"image {d['idx'] + 1} - {d['question']}" for d in ambiguous) + "")
    summary = " ".join(parts) or "No assets were changed."

    logger.info("manage_assets: stored=%d rejected=%d ambiguous=%d",
                len(stored), len(rejected), len(ambiguous))
    result_data = {"stored": len(stored), "rejected": len(rejected), "needs_input": ambiguous}
    if ambiguous:
        # Unsure image(s): yield the turn so the question is actually ASKED.
        # Without this the loop rolls on to the next missing field and the ask
        # is swallowed (F1 Step 3). One batched question per turn; the reply is
        # handled conversationally (no elicit_field - the re-review round-trip
        # for consumed bytes is a separate follow-up).
        result_data["elicited"] = True
    return ToolResult(
        success=True,
        data=result_data,
        summary=summary,  # the saved/skipped/ask text IS the user-facing message
        audience="user",  # model gets model_summary, not the user prose → no double
        model_summary=(
            f"Stored {len(stored)}, skipped {len(rejected)}"
            + (f", {len(ambiguous)} need a user decision (already asked in chat)" if ambiguous else "")
            + "."
        ),
    )


manage_assets = ToolDefinition(
    name="manage_assets",
    description=(
        "Handle images the user uploaded for the ad campaign. Call this whenever "
        "the user attaches one or more images (a logo, a building/render shot, a "
        "floor plan, lifestyle photos) - for the first upload, a correction, or a "
        "replacement. You do NOT pick what each image is or pass the file: the "
        "vision reviewer looks at each pending image and decides its role; code then "
        "saves the relevant ones, skips off-product ones, and flags anything "
        "unclear for you to ask about. Optionally pass `note` to relay what the "
        "user said about the image(s)."
    ),
    display_name="Manage Assets",
    parameters=[
        ToolParameter(
            name="note", type="string", required=False,
            description="Optional: what the user said about the upload(s), e.g. "
                        "'this is our logo' or 'replace the hero shot'. A hint for "
                        "the VisionAnalyst - it still reviews each image itself.",
        ),
    ],
    execute=_manage_assets,
)

MANAGE_ASSETS_TOOLS = [manage_assets]
