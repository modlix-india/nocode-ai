"""manage_assets — the ONE asset front door (replaces save_uploaded_assets).

The orchestrator recognises "this is asset work" and hands over; this tool is
thin — it gathers the pending uploads + a context brief, launches the
AssetManagerAgent (agent-as-tool, same shape as analyze_product → Product
Analyst), runs the completion oracle, and relays the agent's summary. The
Asset Agent decides per image: relevant? role? name? — not the orchestrator.
"""

from __future__ import annotations

import logging
import time

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress

logger = logging.getLogger(__name__)


def _build_brief(sctx: dict) -> str:
    """Context the Asset Agent judges relevance against: what the product is +
    what's already on file."""
    pd = sctx.get("product_data") or {}
    name = pd.get("product_name") or "(unknown product)"
    summary = (pd.get("summary") or "").strip()
    logos = len(pd.get("logo_urls") or ([pd["logo_url"]] if pd.get("logo_url") else []))
    creatives = len(pd.get("creative_images") or [])
    lines = [
        f"Product: {name}",
        f"Summary: {summary[:1500]}" if summary else "Summary: (none yet)",
        f"Already on file: {logos} logo(s), {creatives} product image(s).",
    ]
    return "\n".join(lines)


async def _manage_assets(params: dict, context: dict) -> ToolResult:
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
            "No auth context — the Asset Manager sub-agent needs auth to run."
        ))

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")

    from app.agents.adzump.agents.asset_manager.agent import get_asset_manager_agent
    from app.core.streaming import pre_emit_agent_started

    await emit_progress(context, "Reviewing your uploaded image(s)…")
    if stream:
        await pre_emit_agent_started(
            stream, agent_id="asset_manager", label="Asset Manager",
            parent_tool_use_id=tool_use_id, context=context,
        )

    run_start = time.monotonic()
    result = await get_asset_manager_agent().handle(
        pending=pending,
        brief=_build_brief(sctx),
        parent_sctx=sctx,
        auth=auth,
        parent_event_stream=stream,
    )
    sctx["_pending_uploads"] = []  # consumed — never re-ingest next turn

    dispositions = result["dispositions"]
    stored = [d for d in dispositions if d["action"] == "store"]
    rejected = [d for d in dispositions if d["action"] == "reject"]
    missing = result["missing"]

    if stream:
        try:
            usage = result.get("usage") or {}
            await stream.emit_agent_finished(
                agent_id="asset_manager",
                status="success" if not missing else "error",
                duration_ms=int((time.monotonic() - run_start) * 1000),
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
                summary=f"stored {len(stored)}, rejected {len(rejected)}",
            )
        except Exception:
            logger.exception("asset_manager: agent_finished emit failed")

    # User-facing summary text. The tool owns the wording (tool-text contract);
    # the orchestrator only adds a lead-in.
    parts: list[str] = []
    if stored:
        parts.append("Saved " + ", ".join(f"{d['name']} ({d['role']})" for d in stored) + ".")
    if rejected:
        parts.append("Skipped " + ", ".join(f"image {d['id']} ({d['reason']})" for d in rejected) + ".")
    if missing:
        # Oracle escalation (decided: report, don't auto-reject — see notes).
        parts.append(
            f"Couldn't classify {len(missing)} image(s) — ask the user to describe them."
        )
    summary = " ".join(parts) or "No assets were changed."

    logger.info("manage_assets: stored=%d rejected=%d missing=%d",
                len(stored), len(rejected), len(missing))
    return ToolResult(
        success=not missing,
        data={"stored": len(stored), "rejected": len(rejected), "missing": missing},
        summary=summary,
    )


manage_assets = ToolDefinition(
    name="manage_assets",
    description=(
        "Handle images the user uploaded for the ad campaign. Call this whenever "
        "the user attaches one or more images (a logo, a building/render shot, a "
        "floor plan, lifestyle photos) — for the first upload, a correction, or a "
        "replacement. You do NOT pick what each image is or pass the file: the "
        "Asset Manager looks at each pending image, decides its role and name, "
        "rejects anything off-product, and saves the rest. Optionally pass "
        "`note` to relay what the user said about the image(s)."
    ),
    display_name="Manage Assets",
    parameters=[
        ToolParameter(
            name="note", type="string", required=False,
            description="Optional: what the user said about the upload(s), e.g. "
                        "'this is our logo' or 'replace the hero shot'. A hint for "
                        "the Asset Manager — it still judges each image itself.",
        ),
    ],
    execute=_manage_assets,
)

MANAGE_ASSETS_TOOLS = [manage_assets]
