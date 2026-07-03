"""A4 tool surface — ``generate_creatives`` exposed to the A1 chat agent.

Resolves the A2 product profile (inline param → session context → J9 read),
runs the CreativeAgent pipeline (strategy → best-of-N copy → image brief →
attribute tag → critic/repair gate), and optionally writes the resulting
creatives + lead form onto the active CampaignPlan (J1 merge patch).

A4 emits and gates; the PREDICT score is Java (J20) and STUBBED (None) in P1.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump2.creative.creative import (
    BEST_OF_N_DEFAULT,
    N_ANGLES_DEFAULT,
    get_creative_agent,
)
from app.agents.adzump2.creative.models import CreativeSet
from app.agents.adzump2.creative.taxonomy import KNOWN_FORMATS
from app.agents.adzump2.tools.plan import _PLANS_PATH, _client
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_PRODUCTS_PATH = "/api/adzump/products"

# Google campaign type → creative format; Meta objectives → visual (IMAGE).
_GOOGLE_TYPE_FORMAT = {
    "SEARCH": "RSA",
    "DSA": "RSA",
    "DISPLAY": "IMAGE",
    "VIDEO": "VIDEO",
    "DEMAND_GEN": "DEMAND_GEN",
    "PMAX": "DEMAND_GEN",
}


def _session_ctx(context: dict[str, Any]) -> dict[str, Any]:
    return context.setdefault("session_context", {})


async def _resolve_profile(
    params: dict[str, Any], context: dict[str, Any], plan: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the A2 product profile: inline param → session context → J9 read.

    Returns ``(profile, source)`` or ``(None, reason)``.
    """
    inline = params.get("product_profile")
    if isinstance(inline, dict) and inline:
        return inline, "param"

    ctx_profile = _session_ctx(context).get("product_profile")
    if isinstance(ctx_profile, dict) and ctx_profile:
        return ctx_profile, "session"

    product_id = (
        str(params.get("product_id") or "").strip()
        or (str(plan.get("productId")) if plan and plan.get("productId") else "")
    )
    if not product_id:
        return None, "no product profile (pass product_profile, or set product_id on the plan)"

    result = await _client().get(
        f"{_PRODUCTS_PATH}/{product_id}/profile", headers=context.get("headers")
    )
    if not result.success or not isinstance(result.data, dict):
        return None, f"could not read product profile for {product_id}: {result.error}"
    return result.data, f"J9:{product_id}"


async def _load_plan(context: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort read of the active plan (for vertical/formats + write-back)."""
    plan_id = _session_ctx(context).get("plan_id")
    if not plan_id:
        return None
    result = await _client().get(f"{_PLANS_PATH}/{plan_id}", headers=context.get("headers"))
    if result.success and isinstance(result.data, dict):
        return result.data
    return None


def _derive_formats(params: dict[str, Any], plan: dict[str, Any] | None) -> list[str]:
    explicit = params.get("formats")
    if isinstance(explicit, list) and explicit:
        fmts = [str(f).strip().upper() for f in explicit if str(f).strip().upper() in KNOWN_FORMATS]
        if fmts:
            return list(dict.fromkeys(fmts))

    fmts: list[str] = []
    ctypes = (plan or {}).get("campaignTypes") or {}
    if isinstance(ctypes, dict):
        gtype = str(ctypes.get("GOOGLE") or "").upper()
        if gtype:
            fmts.append(_GOOGLE_TYPE_FORMAT.get(gtype, "RSA"))
        if ctypes.get("META"):
            fmts.append("IMAGE")
    return list(dict.fromkeys(fmts)) or ["RSA", "IMAGE"]


async def _write_back(
    context: dict[str, Any], result: CreativeSet
) -> tuple[bool, str]:
    """Merge-patch launchable creatives + lead form onto the active plan (J1)."""
    plan_id = _session_ctx(context).get("plan_id")
    if not plan_id:
        return False, "no active plan"
    body: dict[str, Any] = {
        "creatives": [c.to_plan_creative() for c in result.launchable],
    }
    if result.lead_form is not None:
        body["leadForm"] = result.lead_form.to_plan_lead_form()
    patch = {"body": body}
    res = await _client().patch(
        f"{_PLANS_PATH}/{plan_id}", headers=context.get("headers"), json=patch
    )
    if not res.success:
        return False, f"plan write-back failed: {res.error}"
    return True, f"wrote {len(result.launchable)} creative(s) to plan {plan_id}"


async def _generate_creatives(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    plan = await _load_plan(context)

    profile, source = await _resolve_profile(params, context, plan)
    if profile is None:
        return ToolResult(
            success=False,
            error=f"Cannot generate creatives: {source}. A studied product profile "
            "is required (A2 gates creative generation).",
        )

    vertical = (
        str(params.get("vertical") or "").strip()
        or str(profile.get("vertical") or "")
        or (str(plan.get("vertical")) if plan and plan.get("vertical") else "")
        or None
    )
    formats = _derive_formats(params, plan)
    n_angles = params.get("n_angles") or N_ANGLES_DEFAULT
    best_of_n = params.get("best_of_n") or BEST_OF_N_DEFAULT
    write_to_plan = params.get("write_to_plan", True)

    agent = get_creative_agent()
    try:
        result = await agent.generate(
            profile=profile,
            vertical=vertical,
            formats=formats,
            n_angles=n_angles,
            best_of_n=best_of_n,
            reference_url=(str(params.get("reference_url") or "").strip() or None),
            auth=context.get("auth"),
            event_stream=context.get("event_stream"),
        )
    except Exception as e:  # noqa: BLE001 — surface as a clean tool error, never raise
        logger.exception("generate_creatives failed")
        return ToolResult(success=False, error=f"Creative generation failed: {type(e).__name__}: {e}")

    data = result.to_dict()
    wrote = False
    write_note = ""
    if write_to_plan:
        wrote, write_note = await _write_back(context, result)
    data["writtenToPlan"] = wrote
    if write_note:
        data["writeNote"] = write_note

    n_launch = len(result.launchable)
    n_explore = len(result.explore)
    summary = (
        f"Generated {len(result.creatives)} creative(s) "
        f"({n_launch} launchable, {n_explore} explore) across {', '.join(formats)} "
        f"for vertical '{result.vertical}'. Lead form: "
        f"{len(result.lead_form.fields) if result.lead_form else 0} fields "
        f"({result.lead_form.source if result.lead_form else 'none'}). "
        f"predict_score is stubbed (None) — J20 scores pre-spend later."
    )
    if write_note:
        summary += f" {write_note}."
    return ToolResult(success=True, data=data, summary=summary)


generate_creatives = ToolDefinition(
    name="generate_creatives",
    display_name="Generate Creatives",
    description=(
        "Generate ad creatives (copy + image briefs + taxonomy attributes) and a "
        "lead form for the studied product, grounded on the A2 profile. Produces "
        "best-of-N copy per angle as slot POOLS (RSA up to 15 headlines / 4 "
        "descriptions; Meta primary text / headline / description) so the "
        "compiler can map pools to platform slots. A pre-spend critic gates weak "
        "copy with bounded repair. predict_score is scored later by Java (J20), "
        "not here. Requires a studied product profile. By default writes the "
        "launchable creatives + lead form onto the active CampaignPlan."
    ),
    parameters=[
        ToolParameter(
            name="product_profile",
            type="object",
            description=(
                "The A2 ProductProfile (name, pitch, value_props, offerings, geo, "
                "price_band, tone, assets, vertical). If omitted, the tool reads it "
                "from session context or the plan's product (J9)."
            ),
            required=False,
        ),
        ToolParameter(
            name="product_id",
            type="string",
            description="Product id to read the profile for (J9), if not passed inline.",
            required=False,
        ),
        ToolParameter(
            name="vertical",
            type="string",
            description="Vertical code override (e.g. real_estate); defaults to the profile/plan vertical.",
            required=False,
        ),
        ToolParameter(
            name="formats",
            type="array",
            description="Creative formats to produce (RSA, IMAGE, VIDEO, CAROUSEL, DEMAND_GEN). Defaults from the plan's campaign types.",
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="n_angles",
            type="integer",
            description="How many creative angles to explore (1-6, default 3).",
            required=False,
        ),
        ToolParameter(
            name="best_of_n",
            type="integer",
            description="Copy variants generated per angle before the critic picks the best (1-5, default 3).",
            required=False,
        ),
        ToolParameter(
            name="reference_url",
            type="string",
            description="Optional competitor/reference URL to inform angle strategy (read via web_fetch).",
            required=False,
        ),
        ToolParameter(
            name="write_to_plan",
            type="boolean",
            description="Write the launchable creatives + lead form onto the active plan (default true).",
            required=False,
        ),
    ],
    execute=_generate_creatives,
)


CREATIVE_TOOLS = [generate_creatives]
