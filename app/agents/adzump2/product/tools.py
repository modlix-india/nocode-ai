"""A2 tools exposed to A1 (the Adzump2 chat agent).

- ``analyze_product`` — run the product study (A2), stash the artifact, and, when
  there are open items, raise the right human-in-loop ask:
    * low-confidence vertical → a **tagged** ``present_options`` confirm
      (reused; field="vertical") so the answer maps cleanly and the vertical
      selects the whole J5 playbook downstream (A2 §5.3–5.4);
    * missing assets → a multi-message upload elicitation.
  It only ever ELICITS conditionally, so it stays ``kind="tool"`` and signals at
  runtime via ``ToolResult.data["elicited"]`` (BaseAgent honors this).
- ``confirm_product_profile`` — on the user's confirm, write the (optionally
  edited) profile back via **J9** ``PATCH /api/adzump/products/{id}/profile``
  and return the discovered competitors for **J19**.

Boundary: these tools do LLM reasoning + orchestration only; the profile write
goes through the shared adzump ``SaasClient`` singleton (reused from ``plan.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

# Reuse (import, don't edit): the tagged-choice elicitation + the shared client.
from app.agents.adzump.tools.suggestions import present_options
from app.agents.adzump2.tools.plan import _client as _plan_client

from app.agents.adzump2.product.models import ProductProfile
from app.agents.adzump2.product.study import get_product_study_agent
from app.agents.adzump2.product.vertical import SPECIFIC_VERTICAL_CODES

logger = logging.getLogger(__name__)

_PRODUCTS_PATH = "/api/adzump/products"

# Human labels for the vertical confirm chips.
_VERTICAL_LABELS: dict[str, str] = {
    "real_estate": "Real estate (property / project)",
    "generic": "Something else / general business",
}


def _session_ctx(context: dict[str, Any]) -> dict[str, Any]:
    """The persisted session context — the study artifact is stashed here."""
    return context.setdefault("session_context", {})


# ── analyze_product ──────────────────────────────────────────────────────────


async def _analyze_product(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Study a product from url / name / product_id → ProductStudyResult."""
    url = (params.get("url") or "").strip() or None
    name = (params.get("name") or "").strip() or None
    product_id = (params.get("product_id") or "").strip() or None
    if not (url or name or product_id):
        return ToolResult(
            success=False,
            error="Provide at least one of url, name, or product_id to study.",
        )

    session_ctx = _session_ctx(context)
    try:
        result = await get_product_study_agent().study(
            url=url,
            name=name,
            product_id=product_id,
            event_stream=context.get("event_stream"),
            auth=context.get("auth"),
            tool_use_id=context.get("tool_use_id", ""),
            session_context=session_ctx,
        )
    except Exception as e:  # study should be resilient; surface a clean error
        logger.exception("analyze_product study failed")
        return ToolResult(success=False, error=f"Product study failed: {type(e).__name__}: {e}")

    # Stash the artifact + the id it belongs to (if the seed carried one) + the
    # competitor list, so confirm_product_profile can write it and J19 can read it.
    study_dict = result.to_dict()
    session_ctx["_product_study"] = study_dict
    if product_id:
        session_ctx["_product_study_product_id"] = product_id
    session_ctx["_product_competitors"] = [c.to_dict() for c in result.competitors]

    data: dict[str, Any] = {
        "study": study_dict,
        "needs_vertical_confirm": result.needs_vertical_confirm,
        "asset_gaps": result.asset_gaps.to_dict(),
        "competitor_count": len(result.competitors),
    }

    # ── human-in-loop: raise the right ask ──
    if result.needs_vertical_confirm:
        po = await _raise_vertical_confirm(result.vertical.code, context)
        # Propagate present_options' tag so the run loop opens the elicitation
        # against this tool call (one ask per turn).
        po_data = po.data if isinstance(po.data, dict) else {}
        data["elicited"] = True
        data["elicit_expects"] = "single"
        data["elicit_field"] = po_data.get("elicit_field", "vertical")
        data["elicit_answers"] = po_data.get("elicit_answers")
        return ToolResult(
            success=True,
            data=data,
            summary=(
                f"Studied {result.profile.name or 'the product'} but the vertical is "
                f"uncertain (best guess '{result.vertical.code}', "
                f"{result.vertical.confidence:.0%}). Asked the user to confirm the "
                "vertical via chips — wait for their reply, then call "
                "confirm_product_profile."
            ),
        )

    if result.asset_gaps.any_open():
        missing = list(result.asset_gaps.missing_categories)
        if result.asset_gaps.logo_missing:
            missing = ["logo", *missing]
        need = ", ".join(missing) or "creative assets"
        data["elicited"] = True
        data["elicit_expects"] = "multi"
        data["elicit_payload"] = result.asset_gaps.to_dict()
        return ToolResult(
            success=True,
            data=data,
            audience="user",
            summary=(
                f"I've studied {result.profile.name or 'the product'} and drafted its "
                f"profile (vertical: {result.vertical.code}). To build strong creatives "
                f"I still need: {need}. Please upload them, or say to proceed without."
            ),
            model_summary=(
                f"Study complete; profile drafted; asset gaps still open ({need}). "
                "Deferred for uploads — resume when the user uploads or says to proceed, "
                "then call confirm_product_profile."
            ),
        )

    # Clean study — no blocking ask. A1 reviews with the user, then confirms.
    return ToolResult(
        success=True,
        data=data,
        summary=(
            f"{result.summary_line()}. Profile drafted and editable; review it with the "
            "user, then call confirm_product_profile to save it (J9). Competitors are "
            "kept for market analysis (J19)."
        ),
    )


async def _raise_vertical_confirm(best_code: str, context: dict[str, Any]) -> ToolResult:
    """Raise a tagged present_options confirm for the vertical (reused tool).

    Offers the specific verticals + generic; ``field="vertical"`` + per-option
    ``answer`` so the harness captures the reply as the confirmed code.
    """
    options: list[dict[str, str]] = []
    for code in list(SPECIFIC_VERTICAL_CODES) + ["generic"]:
        options.append({
            "label": _VERTICAL_LABELS.get(code, code),
            "value": code,
            "answer": code,
        })
    question = (
        "I couldn't confidently tell which category this product is in. "
        "Which fits best?"
    )
    return await present_options.execute(
        {"question": question, "options": options, "mode": "single", "field": "vertical"},
        context,
    )


analyze_product = ToolDefinition(
    name="analyze_product",
    display_name="Analyze Product",
    description=(
        "Study a product before building a campaign: scrape + profile the "
        "business, deduce its vertical, and discover direct competitors. Provide "
        "at least one of url (preferred), name, or product_id. Returns a "
        "structured product study (profile, vertical + confidence, competitors, "
        "asset gaps). Call this ONCE up front — no campaign can be built without "
        "a studied product. If the vertical is uncertain it will ask the user to "
        "confirm; if brand assets are missing it will ask them to upload. After "
        "the user is happy, call confirm_product_profile to save the profile."
    ),
    parameters=[
        ToolParameter(
            name="url",
            type="string",
            description="The product / business website URL (https://…). Preferred seed.",
            required=False,
        ),
        ToolParameter(
            name="name",
            type="string",
            description="The product / business name, when no URL is known.",
            required=False,
        ),
        ToolParameter(
            name="product_id",
            type="string",
            description=(
                "Existing adzump product id, if the campaign already names one. "
                "Must come from the user or a fetcher — never invented."
            ),
            required=False,
        ),
    ],
    execute=_analyze_product,
)


# ── confirm_product_profile (J9 write-back) ──────────────────────────────────


async def _confirm_product_profile(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Write the studied (optionally edited) profile back via J9 + return J19 feed."""
    session_ctx = _session_ctx(context)
    study = session_ctx.get("_product_study")
    if not isinstance(study, dict):
        return ToolResult(
            success=False,
            error="No studied product in this session. Call analyze_product first.",
        )

    product_id = (
        (params.get("product_id") or session_ctx.get("_product_study_product_id") or "")
    ).strip()
    if not product_id:
        return ToolResult(
            success=False,
            error=(
                "product_id is required to save the profile — it must come from the "
                "user or a fetcher, not be invented."
            ),
        )

    profile = ProductProfile.from_dict(study.get("profile")).merge_edits(params.get("edits"))
    vertical_code = (
        (params.get("vertical") or (study.get("vertical") or {}).get("code") or "generic")
    ).strip()

    body = {"profile": profile.to_dict(), "vertical": vertical_code}
    result = await _plan_client().patch(
        f"{_PRODUCTS_PATH}/{product_id}/profile",
        headers=context.get("headers"),
        json=body,
    )
    if not result.success:
        return ToolResult(
            success=False,
            error=f"Failed to save profile for {product_id}: {result.error}",
        )

    # Persist the confirmed state so downstream (A3/A4) reads the final vertical.
    study["profile"] = profile.to_dict()
    study["vertical"] = {**(study.get("vertical") or {}), "code": vertical_code}
    study["needs_vertical_confirm"] = False
    session_ctx["_product_study"] = study
    session_ctx["_product_study_product_id"] = product_id
    session_ctx["_product_confirmed_vertical"] = vertical_code

    competitors = session_ctx.get("_product_competitors") or []
    logger.info("confirm_product_profile: product_id=%s vertical=%s competitors=%d",
                product_id, vertical_code, len(competitors))
    return ToolResult(
        success=True,
        data={
            "productId": product_id,
            "vertical": vertical_code,
            "profile": profile.to_dict(),
            "competitors": competitors,
        },
        summary=(
            f"Saved the profile for product {product_id} (vertical: {vertical_code}). "
            f"{len(competitors)} competitor(s) kept for market analysis. Ready to build "
            "the campaign plan."
        ),
    )


confirm_product_profile = ToolDefinition(
    name="confirm_product_profile",
    display_name="Confirm Product Profile",
    description=(
        "Save the studied product profile back to the adzump service once the "
        "user is happy with it (writes via the product-enhance endpoint). Call "
        "AFTER analyze_product and after the user has confirmed the vertical / "
        "reviewed the profile. Pass product_id (from the user or a fetcher; falls "
        "back to the id analyze_product was seeded with), the confirmed vertical "
        "code if the user picked one, and any field edits the user asked for."
    ),
    parameters=[
        ToolParameter(
            name="product_id",
            type="string",
            description=(
                "Id of the product to write the profile to. Optional if "
                "analyze_product was seeded with one. Never invented."
            ),
            required=False,
        ),
        ToolParameter(
            name="vertical",
            type="string",
            description=(
                "The confirmed vertical code (e.g. the user's chip answer). "
                "Defaults to the deduced vertical if omitted."
            ),
            required=False,
        ),
        ToolParameter(
            name="edits",
            type="object",
            description=(
                "Optional profile field overrides the user asked for, e.g. "
                '{"pitch": "...", "value_props": ["..."], "attributes": {"tone": "premium"}}. '
                "Present keys replace; attributes merge key-wise."
            ),
            required=False,
        ),
    ],
    execute=_confirm_product_profile,
)


PRODUCT_STUDY_TOOLS = [analyze_product, confirm_product_profile]
