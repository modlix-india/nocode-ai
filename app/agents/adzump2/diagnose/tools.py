"""A5 tool surface — ``diagnose`` (+ ``propose_action``) exposed to the A1 loop.

``diagnose`` reads the three fixed A5 endpoints on the adzump Java service over
the CONTRACT paths (J10 snapshot, J12 ActionSet, J20 attribute map) for the
active plan, runs the ``DiagnoseAgent``, and returns a ``Diagnosis`` — the
recommend-mode narrative + prioritized actions + test proposals + watchlist.

``propose_action`` is the sanctioned path for a genuinely-new A5 action: it POSTs
a caller-proposed candidate to the J12 SignificanceGate (``.../actions/propose``)
so the action goes THROUGH the gates, never around them. Recommend-mode only —
it applies NOTHING (autonomy routing + apply is P4/J13).

Both tools are read/recommend only. A5 moves no money and writes no metrics.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump2.diagnose.diagnose import get_diagnose_agent
from app.agents.adzump2.tools.plan import _PLANS_PATH, _client, _require_plan_id
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW = "30d"

_VALID_ACTION_TYPES = {
    "SHIFT_BUDGET",
    "ADJUST_BID",
    "REFINE_AUDIENCE",
    "ADD_NEGATIVE_KEYWORD",
    "PAUSE_ENTITY",
    "ROTATE_CREATIVE",
    "REQUEST_VARIANT",
}


# ── diagnose ──────────────────────────────────────────────────────────────────


async def _diagnose(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    plan_id, err = _require_plan_id(context)
    if err:
        return err

    window = str(params.get("window") or "").strip() or _DEFAULT_WINDOW
    headers = context.get("headers")

    # J10 snapshot — REQUIRED. Without measured performance there is nothing to diagnose.
    perf = await _client().get(
        f"{_PLANS_PATH}/{plan_id}/performance", headers=headers, params={"window": window}
    )
    if not perf.success or not isinstance(perf.data, dict):
        return ToolResult(
            success=False,
            error=f"Cannot diagnose plan {plan_id}: no performance snapshot "
            f"(window={window}): {perf.error}",
        )
    snapshot = perf.data

    # J12 ActionSet + J20 attribute map — degrade gracefully (diagnose still narrates).
    recs = await _client().get(
        f"{_PLANS_PATH}/{plan_id}/recommendations", headers=headers, params={"window": window}
    )
    action_set = recs.data if recs.success and isinstance(recs.data, dict) else {}

    amap = await _client().get(
        f"{_PLANS_PATH}/{plan_id}/attribute-map", headers=headers
    )
    attribute_map = amap.data if amap.success and isinstance(amap.data, dict) else {}

    vertical = (
        str(params.get("vertical") or "").strip()
        or str(snapshot.get("vertical") or "")
        or None
    )

    agent = get_diagnose_agent()
    try:
        diagnosis = await agent.diagnose(
            snapshot=snapshot,
            action_set=action_set,
            attribute_map=attribute_map,
            vertical=vertical,
            auth=context.get("auth"),
            event_stream=context.get("event_stream"),
        )
    except Exception as e:  # noqa: BLE001 — surface as a clean tool error, never raise
        logger.exception("diagnose failed")
        return ToolResult(success=False, error=f"Diagnosis failed: {type(e).__name__}: {e}")

    data = diagnosis.to_dict()
    if not action_set:
        data.setdefault("warnings", []).append(
            "no J12 ActionSet available — narrated the snapshot without prioritized actions"
        )
    n_act = len(diagnosis.ranked_actions)
    n_test = len(diagnosis.test_proposals)
    n_watch = len(diagnosis.watchlist)
    summary = (
        f"Diagnosed plan {plan_id} (window={window}): {n_act} prioritized action(s), "
        f"{n_test} grounded test proposal(s), {n_watch} grain(s) on the watchlist. "
        "A5 narrates + prioritizes J12 — it recomputes no numbers and applies nothing; "
        "thin/immature grains are watched, not acted on."
    )
    return ToolResult(success=True, data=data, summary=summary)


# ── propose_action (genuinely-new action → through the J12 gates) ─────────────


async def _propose_action(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    plan_id, err = _require_plan_id(context)
    if err:
        return err

    action_type = str(params.get("type") or "").strip().upper()
    if action_type not in _VALID_ACTION_TYPES:
        return ToolResult(
            success=False,
            error=f"type must be one of {sorted(_VALID_ACTION_TYPES)}.",
        )
    target_id = str(params.get("target_id") or params.get("targetId") or "").strip()
    if not target_id:
        return ToolResult(success=False, error="target_id is required.")

    change = params.get("change")
    if change is not None and not isinstance(change, dict):
        return ToolResult(success=False, error="change must be an object (the typed action payload).")

    body: dict[str, Any] = {
        "type": action_type,
        "targetId": target_id,
        "change": change or {},
        "rationale": str(params.get("rationale") or "").strip(),
    }

    res = await _client().post(
        f"{_PLANS_PATH}/{plan_id}/actions/propose", headers=context.get("headers"), json=body
    )
    if not res.success:
        return ToolResult(
            success=False,
            error=f"Failed to propose action on plan {plan_id}: {res.error}",
        )

    data = res.data if isinstance(res.data, dict) else {}
    # The gate either returns a single gated Action or a suppressed result (+ reason).
    suppressed = bool(data.get("suppressed")) or (not data.get("type") and data.get("suppressionReason"))
    if suppressed:
        reason = data.get("suppressionReason") or data.get("reason") or "gate suppressed the candidate"
        summary = f"Proposed {action_type} on {target_id}: SUPPRESSED by the J12 gate — {reason}. Nothing applied."
    else:
        verdict = data.get("significanceVerdict") or data.get("significance") or "?"
        summary = (
            f"Proposed {action_type} on {target_id}: passed the J12 gate "
            f"(significance={verdict}, requiresApproval={data.get('requiresApproval', True)}). "
            "Recommend-mode — nothing applied."
        )
    return ToolResult(success=True, data=data, summary=summary)


# ── tool definitions ──────────────────────────────────────────────────────────


diagnose = ToolDefinition(
    name="diagnose",
    display_name="Diagnose Campaign",
    description=(
        "Diagnose the active campaign plan's performance in plain language. Reads "
        "the performance snapshot (platform + leadzump CRM joined at the ad grain), "
        "the engine's significance-gated ActionSet, and the creative attribute map, "
        "then explains WHY it's performing as it is (which angle wins, where junk "
        "concentrates), PRIORITIZES the engine's actions with business framing, "
        "proposes qualitative creative/audience tests grounded in real attribute "
        "gaps, and lists thin/immature grains to keep watching. It recomputes no "
        "numbers and applies nothing; immature (FAST_ONLY) grains are watched, "
        "never acted on. Use it in recommend-mode to narrate the feed for the user."
    ),
    parameters=[
        ToolParameter(
            name="window",
            type="string",
            description="Performance window to diagnose over (e.g. 7d, 30d). Defaults to 30d.",
            required=False,
        ),
        ToolParameter(
            name="vertical",
            type="string",
            description="Vertical code override (e.g. real_estate); defaults to the snapshot's vertical.",
            required=False,
        ),
    ],
    execute=_diagnose,
)

propose_action = ToolDefinition(
    name="propose_action",
    display_name="Propose Action",
    description=(
        "Run a genuinely-new candidate action (one the engine did NOT already "
        "surface) through the SAME J12 significance gate + objective, so it goes "
        "THROUGH the gates, never around them. Returns the single gated action "
        "(with its significance verdict; approval always required) OR a suppressed "
        "result with the reason. RECOMMEND-MODE ONLY — it applies nothing. Use it "
        "instead of putting an invented action into the diagnosis narrative."
    ),
    parameters=[
        ToolParameter(
            name="type",
            type="string",
            description="Action type.",
            required=True,
            enum=sorted(_VALID_ACTION_TYPES),
        ),
        ToolParameter(
            name="target_id",
            type="string",
            description="The ad-grain id (campaign/adset/ad entityId) the action targets.",
            required=True,
        ),
        ToolParameter(
            name="change",
            type="object",
            description="The typed change payload for this action (shape depends on type).",
            required=False,
        ),
        ToolParameter(
            name="rationale",
            type="string",
            description="Why this action is proposed (business + quantitative reasoning).",
            required=False,
        ),
    ],
    execute=_propose_action,
)


DIAGNOSE_TOOLS = [diagnose, propose_action]
