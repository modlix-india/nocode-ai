"""Manual targeting-list edits - the LocationAgent's deterministic tools.

``add_location`` appends one user-named area; ``delete_location`` removes one
by its 1-based index. No LLM judgment inside - the agent's loop extracts the
params from the user's message; these executes just mutate ``target_areas``
and end in ``finalize_targets`` (map → persist → re-render), the same single
source of truth the discovery tools finish through.

Tool schemas are GENERATED from the params models in models.py (see
``tool_params_from_model``), so required fields, descriptions, and defaults
reach the LLM from the models; bounds (min_length, ge) are enforced at the
execute's re-parse, not shown to the model.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from app.core.tools.base import ToolDefinition, ToolResult, tool_params_from_model
from app.agents.adzump.agents.location.models import AddLocation, DeleteLocation
from app.agents.adzump.agents.location.tools._shared import finalize_targets

logger = logging.getLogger(__name__)


async def _add_location(params: dict, context: dict) -> ToolResult:
    """Append a targeting area and re-sync platform handles + craft panel."""
    location, invalid = _parse_params(AddLocation, params)
    if invalid is not None:
        return invalid
    target_areas = _target_areas(context)
    if target_areas is None:
        return ToolResult(success=False, error="No session context available.")

    area: dict = {"name": location.name, "distance_km": location.radius}
    where = {"city": location.city, "state": location.state,
             "pincode": location.pincode, "lat": location.lat, "lng": location.lng,
             "scale": location.scale}
    area.update({k: v for k, v in where.items() if v not in (None, "")})

    # The funnel owns the commit (it assigns product["target_areas"] itself),
    # so pass the appended COPY - a save/render failure leaves memory untouched.
    mapped = await finalize_targets([*target_areas, area], context)
    logger.info("location_agent.add_location: name=%r areas=%d", location.name, len(mapped))
    return ToolResult(
        success=True,
        data={"target_areas": mapped},
        summary=f"Added '{location.name}' to targeting - {len(mapped)} area(s) total.",
    )


async def _delete_location(params: dict, context: dict) -> ToolResult:
    """Remove a targeting area by 1-based index and re-sync."""
    deletion, invalid = _parse_params(DeleteLocation, params)
    if invalid is not None:
        return invalid
    target_areas = _target_areas(context)
    if target_areas is None:
        return ToolResult(success=False, error="No session context available.")

    # index >= 1 is enforced by the model; the upper bound needs the live list.
    index = deletion.index
    if index > len(target_areas):
        return ToolResult(
            success=False,
            error=(
                f"Invalid index {index}. There are only {len(target_areas)} "
                "target area(s)."
            ),
        )
    removed_name = target_areas[index - 1].get("name") or "the targeting area"
    # Same contract as add_location: the funnel commits, so pass the survivors
    # as a new list and the deletion only sticks if finalize succeeds.
    survivors = [a for i, a in enumerate(target_areas) if i != index - 1]

    mapped = await finalize_targets(survivors, context)
    logger.info("location_agent.delete_location: index=%s areas=%d", index, len(mapped))
    return ToolResult(
        success=True,
        data={"target_areas": mapped},
        summary=f"Removed '{removed_name}' from targeting - {len(mapped)} area(s) left.",
    )


def _parse_params(model_cls, params: dict):
    """LLM → Python boundary: type the raw tool params, or build the error.

    Returns ``(model, None)`` on success, ``(None, ToolResult)`` on failure -
    so the executes never see an unvalidated dict."""
    try:
        return model_cls(**params), None
    except ValidationError as e:
        first = e.errors()[0]
        logger.warning("edit_location_params_invalid: model=%s error=%s",
                       model_cls.__name__, first)
        return None, ToolResult(
            success=False,
            error=(
                f"Invalid params: {'.'.join(str(p) for p in first['loc'])} - "
                f"{first['msg']}. Use only values the user actually gave."
            ),
        )


def _target_areas(context: dict) -> list[dict] | None:
    """Live target_areas list from session context; None when no session."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return None
    return session_ctx.setdefault("product_data", {}).setdefault("target_areas", [])


add_location_tool = ToolDefinition(
    name="add_location",
    description=(
        "Append ONE specific area the user named to the existing targeting "
        "list. Pass only the fields present in their message - never invent "
        "coordinates, pincodes, or radii they didn't give."
    ),
    display_name="Add Location",
    parameters=tool_params_from_model(AddLocation),
    execute=_add_location,
)

delete_location_tool = ToolDefinition(
    name="delete_location",
    description=(
        "Remove one area from the targeting list by its 1-based position. "
        "'the first/second/Nth', 'remove that one', and 'delete the area I "
        "just added' all map to an index. If the user names an area ('remove "
        "Bangalore'), find that area's index in the current targeting list "
        "you were given."
    ),
    display_name="Delete Location",
    parameters=tool_params_from_model(DeleteLocation),
    execute=_delete_location,
)
