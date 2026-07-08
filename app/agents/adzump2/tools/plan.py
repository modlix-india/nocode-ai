"""Adzump2 plan tools — CRUD over the server-side CampaignPlan.

The CampaignPlan is the platform-neutral IR owned by the adzump Java
service (nocode-saas/adzump). These tools are the agent's ONLY path to it:

- ``create_plan``       POST  /api/adzump/plans
- ``get_plan``          GET   /api/adzump/plans/{id}
- ``update_plan``       PATCH /api/adzump/plans/{id}  (RFC-7386 merge patch)
- ``get_completeness``  GET   /api/adzump/plans/{id}/completeness
- ``validate_plan``     POST  /api/adzump/plans/{id}/validate

The active plan id lives in ``context["session_context"]["plan_id"]``:
``create_plan`` stores it, every other tool reads it. After each successful
patch (and on explicit reads) the completeness payload is stashed into
``session_context["plan_completeness"]`` so the agent's turn reminder can
render the missing-slots rail without an extra round-trip.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.core.tools.http_client import SaasClient

logger = logging.getLogger(__name__)

_PLANS_PATH = "/api/adzump/plans"

_client_instance: SaasClient | None = None


def _client() -> SaasClient:
    """Shared SaasClient singleton for the adzump gateway endpoints."""
    global _client_instance
    if _client_instance is None:
        _client_instance = SaasClient(settings.GATEWAY_URL)
    return _client_instance


async def close_plan_client() -> None:
    """Close the SaasClient (call on shutdown)."""
    global _client_instance
    if _client_instance is not None:
        await _client_instance.close()
        _client_instance = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _session_ctx(context: dict[str, Any]) -> dict[str, Any]:
    """The persisted session context — plan_id / plan_completeness live here."""
    return context.setdefault("session_context", {})


def _require_plan_id(context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    """Read the active plan id from session context, or an error ToolResult."""
    plan_id = _session_ctx(context).get("plan_id")
    if not plan_id:
        return "", ToolResult(
            success=False,
            error="No active plan in this session. Call create_plan first.",
        )
    return str(plan_id), None


def _plan_summary(plan: Any) -> str:
    """One-line human summary of a CampaignPlan payload."""
    if not isinstance(plan, dict):
        return "plan saved"
    parts = [f"plan #{plan.get('id')}"]
    if plan.get("name"):
        parts.append(f"'{plan['name']}'")
    if plan.get("status"):
        parts.append(f"status={plan['status']}")
    platforms = plan.get("platforms") or []
    if platforms:
        parts.append("platforms=" + ",".join(str(p) for p in platforms))
    return " ".join(parts)


def _completeness_line(completeness: Any) -> str:
    """Render a PlanCompleteness payload as a short human line."""
    if not isinstance(completeness, dict):
        return ""
    if completeness.get("complete"):
        return "plan is complete — all required slots filled"
    missing = completeness.get("missingRequired") or []
    if missing:
        return "remaining: " + ", ".join(str(m) for m in missing)
    return "completeness unknown"


async def _fetch_and_stash_completeness(
    plan_id: str, context: dict[str, Any]
) -> dict[str, Any] | None:
    """GET /completeness and stash the payload into session context.

    Returns the payload, or None on failure — non-fatal for callers whose
    primary call (e.g. the patch) already succeeded.
    """
    result = await _client().get(
        f"{_PLANS_PATH}/{plan_id}/completeness",
        headers=context.get("headers"),
    )
    if not result.success or not isinstance(result.data, dict):
        logger.warning(
            "Completeness fetch failed for plan %s: %s", plan_id, result.error
        )
        return None
    _session_ctx(context)["plan_completeness"] = result.data
    return result.data


# ── execute functions ───────────────────────────────────────────────────────


async def _create_plan(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Create a CampaignPlan and make it the session's active plan."""
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="name is required.")

    body: dict[str, Any] = {"name": name}
    product_id = (str(params.get("product_id") or "")).strip()
    if product_id:
        body["productId"] = product_id

    result = await _client().post(
        _PLANS_PATH, headers=context.get("headers"), json=body
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create plan: {result.error}")

    plan = result.data if isinstance(result.data, dict) else {}
    plan_id = plan.get("id")
    if plan_id is None:
        return ToolResult(
            success=False, error="Plan created but the response carried no id."
        )

    _session_ctx(context)["plan_id"] = str(plan_id)
    logger.info("create_plan: plan_id=%s name=%r", plan_id, name)
    return ToolResult(success=True, data=plan, summary=f"Created {_plan_summary(plan)}")


async def _get_plan(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read the session's active CampaignPlan."""
    plan_id, err = _require_plan_id(context)
    if err:
        return err

    result = await _client().get(
        f"{_PLANS_PATH}/{plan_id}", headers=context.get("headers")
    )
    if not result.success:
        return ToolResult(
            success=False, error=f"Failed to read plan {plan_id}: {result.error}"
        )
    return ToolResult(success=True, data=result.data, summary=_plan_summary(result.data))


async def _update_plan(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Merge-patch the active plan, then refresh the completeness stash."""
    plan_id, err = _require_plan_id(context)
    if err:
        return err

    patch = params.get("patch")
    if not isinstance(patch, dict) or not patch:
        return ToolResult(
            success=False,
            error="patch must be a non-empty object (RFC-7386 merge patch over the plan).",
        )

    result = await _client().patch(
        f"{_PLANS_PATH}/{plan_id}", headers=context.get("headers"), json=patch
    )
    if not result.success:
        return ToolResult(
            success=False, error=f"Failed to patch plan {plan_id}: {result.error}"
        )

    completeness = await _fetch_and_stash_completeness(plan_id, context)
    touched = ", ".join(sorted(patch.keys()))
    line = _completeness_line(completeness)
    summary = f"{touched} set" + (f"; {line}" if line else "")
    logger.info("update_plan: plan_id=%s touched=%s", plan_id, touched)
    return ToolResult(success=True, data=result.data, summary=summary)


async def _get_completeness(
    params: dict[str, Any], context: dict[str, Any]
) -> ToolResult:
    """Read (and stash) the active plan's required-slot completeness."""
    plan_id, err = _require_plan_id(context)
    if err:
        return err

    completeness = await _fetch_and_stash_completeness(plan_id, context)
    if completeness is None:
        return ToolResult(
            success=False,
            error=f"Failed to read completeness for plan {plan_id}.",
        )
    return ToolResult(
        success=True, data=completeness, summary=_completeness_line(completeness)
    )


async def _validate_plan(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Run server-side validation over the active plan."""
    plan_id, err = _require_plan_id(context)
    if err:
        return err

    result = await _client().post(
        f"{_PLANS_PATH}/{plan_id}/validate", headers=context.get("headers")
    )
    if not result.success:
        return ToolResult(
            success=False, error=f"Failed to validate plan {plan_id}: {result.error}"
        )

    data = result.data if isinstance(result.data, dict) else {}
    if data.get("valid"):
        summary = "plan is valid — no issues"
    else:
        issues = data.get("issues") or []
        summary = f"plan has {len(issues)} validation issue{'s' if len(issues) != 1 else ''}"
    return ToolResult(success=True, data=data, summary=summary)


# ── tool definitions ────────────────────────────────────────────────────────


create_plan = ToolDefinition(
    name="create_plan",
    display_name="Create Plan",
    description=(
        "Create a new CampaignPlan on the adzump service and make it this "
        "session's active plan. Call it ONCE at the start of a build; make "
        "every subsequent edit via update_plan."
    ),
    parameters=[
        ToolParameter(
            name="name",
            type="string",
            description="Human-readable name for the campaign plan.",
            required=True,
        ),
        ToolParameter(
            name="product_id",
            type="string",
            description=(
                "Id of the product this campaign advertises. Must come from a "
                "fetcher tool or verbatim from the user — never invented."
            ),
            required=False,
        ),
    ],
    execute=_create_plan,
)

get_plan = ToolDefinition(
    name="get_plan",
    display_name="Get Plan",
    description=(
        "Read the session's active CampaignPlan from the adzump service — the "
        "authoritative current state. Use it instead of recalling plan values "
        "from earlier in the conversation."
    ),
    parameters=[],
    execute=_get_plan,
)

update_plan = ToolDefinition(
    name="update_plan",
    display_name="Update Plan",
    description=(
        "Apply an RFC-7386 merge patch to the active CampaignPlan — the ONLY "
        "way to edit the plan. Pass just the fields to change (set a field to "
        "null to remove it). Returns the updated plan and reports which "
        "required slots are still missing."
    ),
    parameters=[
        ToolParameter(
            name="patch",
            type="object",
            description=(
                "RFC-7386 merge patch over the plan body, e.g. "
                '{"body": {"budget": {"total": {"amount": 50000, "currency": "INR"}}}}. '
                "Only include the fields being changed; null removes a field."
            ),
            required=True,
        ),
    ],
    execute=_update_plan,
)

get_completeness = ToolDefinition(
    name="get_completeness",
    display_name="Get Completeness",
    description=(
        "Check which required slots of the active CampaignPlan are still "
        "missing. update_plan already reports this after each edit — call this "
        "only when you need a fresh snapshot without editing."
    ),
    parameters=[],
    execute=_get_completeness,
)

validate_plan = ToolDefinition(
    name="validate_plan",
    display_name="Validate Plan",
    description=(
        "Run server-side validation over the active CampaignPlan (deep checks "
        "beyond slot completeness). Call it once all required slots are "
        "filled, before reviewing the plan with the user."
    ),
    parameters=[],
    execute=_validate_plan,
)


PLAN_TOOLS = [create_plan, get_plan, update_plan, get_completeness, validate_plan]
