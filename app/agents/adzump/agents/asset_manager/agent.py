"""AssetManagerAgent — tool-loop agent that judges user-uploaded images.

The orchestrator hands over via the `manage_assets` tool (see
tools/asset_manage.py). This agent SEES each pasted image, decides per image
whether it belongs (relevance gate), what it is (role), and what to call it
(name), then ACTS through `store_asset` / `reject_asset`. A completion oracle
in the launcher reconciles dispositions against the image count and re-pokes
once on a shortfall — so a skipped image can't slip through.

Distinct from AssetPickerAgent (single-shot scrape-candidate judge). Shares
the upload transport (`_uploads.py`) and the receipts emitter. Model + tier
reuse the picker's vision config — see implementation-notes for the
Sonnet-vs-gpt-4o-mini decision.
"""

from __future__ import annotations

import base64
import logging
from hashlib import md5
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from app.agents.adzump._uploads import upload_and_analyze
from app.agents.adzump.agents.asset_manager.context import build_asset_manager_context
from app.agents.adzump.agents.product.tools.scrape.receipts import _emit_asset_receipts

logger = logging.getLogger(__name__)

# Vision config reuse from the picker (gpt-4o-mini). The plan floated
# Sonnet-class "then eval"; reusing the picker's known-working vision model
# is the lower-risk choice for now — tier is a Kiran/eval decision.
_PROVIDER = "openai"
_MODEL_OVERRIDE = "openai:gpt-4o-mini"
_MODEL_TIER = "fast"
_MAX_TOKENS = 800
# Headroom: one tool call per image + a little slack, ×2 for the re-poke pass.
_MAX_TURNS = 12

_ROLES = ("logo", "hero", "amenity", "floor_plan")


# ── product_data writers (the deterministic "HOW" behind the tools) ────────
# Ported from the old save_uploaded_assets so the write shape (the four
# index-aligned lists + back-compat scalars + the _shift3 decline signal)
# stays byte-identical. T-014 will normalise this state model later.

def _store_logo(product_data: dict, res: dict, name: str, sctx: dict) -> None:
    # First logo store of THIS run clears any auto-detected logo — the user
    # is correcting the picker ("user upload wins"). Later stores in the same
    # run append (multi-logo: developer + project).
    if not sctx.get("_asset_logo_cleared"):
        product_data["logo_urls"] = []
        product_data["logo_displays"] = []
        product_data["logo_meta"] = []
        sctx["_asset_logo_cleared"] = True
    product_data["logo_urls"].append(res["url"])
    product_data["logo_displays"].append({k: v for k, v in res.items() if k != "url"})
    product_data["logo_meta"].append({
        "source_url": "user_upload", "source": "user_upload", "role": "main",
        "reasoning": f"User-uploaded ({name})", "format": res.get("format", ""),
    })
    product_data["logo_url"] = product_data["logo_urls"][0]
    product_data["logo_display"] = product_data["logo_displays"][0]
    product_data["logo_source_url"] = "user_upload"
    product_data["logo_source"] = "user_upload"
    product_data["logo_reasoning"] = "User-uploaded logo"
    product_data["logo_confidence"] = 1.0
    sig = product_data.get("_shift3_signal")
    if isinstance(sig, dict):
        sig["logo_missing"] = False


def _store_creative(product_data: dict, res: dict, role: str, name: str) -> bool:
    urls = product_data.setdefault("creative_images", [])
    displays = product_data.setdefault("creative_displays", [])
    if res["url"] in set(urls):
        return False
    urls.append(res["url"])
    displays.append({k: v for k, v in res.items() if k != "url"})
    sig = product_data.get("_shift3_signal")
    if isinstance(sig, dict):
        sig["creative_missing_categories"] = [
            c for c in (sig.get("creative_missing_categories") or []) if c != role
        ]
    return True


async def _emit_receipts(context: dict, sctx: dict, product_data: dict) -> None:
    stream = context.get("event_stream")
    craft_id = sctx.get("craft_id", "")
    if not (stream and craft_id):
        return
    try:
        await _emit_asset_receipts(stream, craft_id, sctx.get("primary_url", ""), product_data)
    except Exception:
        logger.exception("asset_manager: receipts emit failed")


# ── the agent's tools ──────────────────────────────────────────────────────

async def _store_asset(params: dict, context: dict) -> ToolResult:
    sctx = context.get("session_context") or {}
    image_id = (params.get("image_id") or "").strip()
    role = (params.get("role") or "").strip().lower()
    name = (params.get("name") or "").strip()
    background = (params.get("background") or "").strip().lower()
    pending = sctx.get("_asset_pending_by_id") or {}
    disp = sctx.setdefault("_asset_dispositions", [])

    if image_id not in pending:
        return ToolResult(success=False, error=(
            f"No pending image with id {image_id}. Valid ids: {', '.join(pending) or '(none)'}."
        ))
    if role not in _ROLES:
        return ToolResult(success=False, error=f"role must be one of: {', '.join(_ROLES)}.")
    if any(d["id"] == image_id for d in disp):
        return ToolResult(success=False, error=f"Image {image_id} was already handled this run.")

    try:
        raw = base64.b64decode(pending[image_id].get("data") or "")
    except Exception:
        raw = b""
    if not raw:
        return ToolResult(success=False, error=f"Image {image_id} bytes are unreadable.")

    kind = "logo" if role == "logo" else "creative"
    hints: dict[str, str] = {"fit": "contain" if kind == "logo" else "cover"}
    if kind == "logo" and background in ("light", "dark"):
        hints["background"] = background

    res = await upload_and_analyze(
        raw, pending[image_id].get("mime") or "image/png",
        f"user_upload:{name}", kind, context, hints=hints, name=name,
    )
    if not res:
        return ToolResult(success=False, error=(
            f"Upload failed for image {image_id}; ask the user to retry."
        ))

    product_data = sctx.setdefault("product_data", {})
    if role == "logo":
        _store_logo(product_data, res, name, sctx)
    else:
        _store_creative(product_data, res, role, name)
    disp.append({"id": image_id, "action": "store", "role": role, "name": name, "url": res["url"]})
    await _emit_receipts(context, sctx, product_data)
    return ToolResult(success=True, summary=f"Stored image {image_id} as {role} ({name}).")


async def _reject_asset(params: dict, context: dict) -> ToolResult:
    sctx = context.get("session_context") or {}
    image_id = (params.get("image_id") or "").strip()
    reason = (params.get("reason") or "").strip() or "not relevant to the product"
    pending = sctx.get("_asset_pending_by_id") or {}
    disp = sctx.setdefault("_asset_dispositions", [])

    if image_id not in pending:
        return ToolResult(success=False, error=f"No pending image with id {image_id}.")
    if any(d["id"] == image_id for d in disp):
        return ToolResult(success=False, error=f"Image {image_id} was already handled this run.")
    disp.append({"id": image_id, "action": "reject", "reason": reason})
    return ToolResult(success=True, summary=f"Rejected image {image_id}: {reason}")


store_asset = ToolDefinition(
    name="store_asset",
    description=(
        "Save one uploaded image as a campaign asset. Call once per relevant "
        "image, referencing it by the id shown in the prompt (Image N (id: XXXXXX))."
    ),
    display_name="Store Asset",
    parameters=[
        ToolParameter(name="image_id", type="string", description="The id of the image to store."),
        ToolParameter(name="role", type="string",
                      description="Slot the image fills.", enum=list(_ROLES)),
        ToolParameter(name="name", type="string",
                      description="2-4 word descriptive name, e.g. 'logo-dark', 'floor-plan-3bhk'."),
        ToolParameter(name="background", type="string", required=False,
                      description="Logos only: 'light' if the mark is light (needs a dark tile), "
                                  "'dark' if dark, '' otherwise.",
                      enum=["light", "dark", ""]),
    ],
    execute=_store_asset,
)

reject_asset = ToolDefinition(
    name="reject_asset",
    description=(
        "Decline one uploaded image that does NOT belong in this campaign "
        "(unrelated photo, meme, wrong product). Reference it by its id."
    ),
    display_name="Reject Asset",
    parameters=[
        ToolParameter(name="image_id", type="string", description="The id of the image to reject."),
        ToolParameter(name="reason", type="string", description="One-line reason it doesn't belong."),
    ],
    execute=_reject_asset,
)


# ── the agent ────────────────────────────────────────────────────────────

def _build_message(pending_by_id: dict[str, dict], brief: str) -> tuple[str, list[dict]]:
    """Vision user-message: brief + one labelled line per image, plus the
    image blocks in the same order. Ids are content hashes (also the filename
    hash) so the model's tool args bind unambiguously to bytes."""
    lines = [brief, "", "Uploaded images — store or reject EVERY one:"]
    image_blocks: list[dict] = []
    for n, (iid, up) in enumerate(pending_by_id.items(), start=1):
        lines.append(f"- Image {n} (id: {iid})")
        data = up.get("data") or ""
        mime = up.get("mime") or "image/png"
        if data:
            image_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
            })
    return "\n".join(lines), image_blocks


class AssetManagerAgent(BaseAgent):
    """Tool-loop vision agent: judge + store/reject each uploaded image."""

    display_name = "Asset Manager"
    _instance: "AssetManagerAgent | None" = None

    def __init__(self) -> None:
        super().__init__(
            name="asset_manager",
            tools=[store_asset, reject_asset],
            context_builder=build_asset_manager_context(),
            model_tier=_MODEL_TIER,
            max_turns=_MAX_TURNS,
            max_tokens=_MAX_TOKENS,
            provider=_PROVIDER,
            context_management=None,
        )

    @classmethod
    def get_instance(cls) -> "AssetManagerAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("AssetManagerAgent created (tool-loop, store/reject)")
        return cls._instance

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        # Expose the sub-session context so store/reject reach the pending
        # bytes, the (parent-referenced) product_data, and the dispositions
        # list; and so upload_and_analyze's _asset_filename can read
        # product_data for the filename prefix.
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        return ctx

    async def handle(
        self,
        pending: list[dict],
        brief: str,
        parent_sctx: dict,
        auth: AuthContext,
        parent_event_stream: AgentEventStream | None,
    ) -> dict:
        """Run the judge loop over `pending`; return the dispositions + any
        ids the model never handled (after one re-poke)."""
        pending_by_id: dict[str, dict] = {}
        for up in pending:
            try:
                raw = base64.b64decode(up.get("data") or "")
            except Exception:
                raw = b""
            iid = md5(raw).hexdigest()[:6] if raw else md5(repr(up).encode()).hexdigest()[:6]
            pending_by_id[iid] = up

        sub_session = BaseSession(agent_name="asset_manager")
        await sub_session.get_or_create(None, auth)
        sub_session.context = {
            "_asset_pending_by_id": pending_by_id,
            "product_data": parent_sctx.setdefault("product_data", {}),  # parent ref — writes propagate
            "_asset_dispositions": [],
            "craft_id": parent_sctx.get("craft_id", ""),
            "primary_url": (parent_sctx.get("product_profile") or {}).get("url")
                           or parent_sctx.get("product_data", {}).get("url", ""),
        }

        message, image_blocks = _build_message(pending_by_id, brief)
        missing = list(pending_by_id)
        # One initial pass + one re-poke (completion oracle). The model
        # demonstrably skips items; the oracle is the deterministic backstop.
        for attempt in range(2):
            try:
                await self.run(
                    user_message=message,
                    session=sub_session,
                    event_stream=parent_event_stream,
                    image_blocks=image_blocks if attempt == 0 else None,
                    model_override=_MODEL_OVERRIDE,
                )
            except Exception as e:
                logger.warning("asset_manager_run_failed (attempt %d): %s: %s",
                               attempt, type(e).__name__, str(e)[:200])
                break
            handled = {d["id"] for d in sub_session.context["_asset_dispositions"]}
            missing = [i for i in pending_by_id if i not in handled]
            if not missing:
                break
            message = (
                "You have NOT handled these images yet: "
                + ", ".join(missing)
                + ". Call store_asset or reject_asset for each one now."
            )

        return {
            "dispositions": sub_session.context["_asset_dispositions"],
            "missing": missing,
            "pending_ids": list(pending_by_id),
            "usage": sub_session.total_usage or {},
        }


def get_asset_manager_agent() -> AssetManagerAgent:
    return AssetManagerAgent.get_instance()
