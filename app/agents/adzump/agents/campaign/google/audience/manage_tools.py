"""Tools the AudienceAgent uses AFTER the build - to answer and to edit.

  lookup_segment   what we recorded about one targeted segment, or that it is not targeted
  edit_audience    add or drop segments, change demographics - batched

General capabilities, not one handler per question: "why this one?", "add something for young
families", "drop the finance ones" all compose from these plus ``search_audience_segments``.

edit_audience goes through the SAME ``apply_edit`` the panel's click path uses, so an edit
made in words cannot break an invariant a click couldn't.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.campaign.google.audience import constants
from app.agents.adzump.agents.campaign.models import audience
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_MAX_EDITS = 15  # one conversational turn's worth


def _signals(context: dict) -> list[dict]:
    state = context.get("session_context") or {}
    return (audience(state) or {}).get("signals") or []


def _find(signals: list[dict], needle: str) -> dict | None:
    """The targeted segment a user named, by label, resource name or id.

    Label first and case-insensitively, because a user types what the panel shows them,
    not a resource name.
    """
    folded = needle.casefold()
    for s in signals:
        if s.get("label", "").casefold() == folded:
            return s
    for s in signals:
        if s["ref"] == needle or s["ref"].rsplit("/", 1)[-1] == needle:
            return s
    # Last resort: a substring of the label, so "finance" reaches "Finance & Banking".
    return next((s for s in signals if folded in s.get("label", "").casefold()), None)


async def _lookup_segment(params: dict, context: dict) -> ToolResult:
    needle = str(params.get("segment", "")).strip()
    if not needle:
        return ToolResult(success=False, error="A `segment` is required.")

    signals = _signals(context)
    if not signals:
        return ToolResult(
            success=False, error="No audience has been built for this campaign yet."
        )

    hit = _find(signals, needle)
    if hit is None:
        # Never invent a past reason. The catalogue is large and "we did not pick it" is
        # usually "it was not the sharpest fit", not a recorded judgement.
        return ToolResult(
            success=True,
            summary=(
                f"'{needle}' is NOT targeted by this campaign, and there is no record of it "
                "being considered — say that plainly rather than inventing a reason. "
                "Currently targeted: "
                + ", ".join(s.get("label", "") for s in signals)
                + ". If the user wants it added, find it with search_audience_segments first."
            ),
        )

    facts = [f"'{hit.get('label')}' IS targeted ({hit.get('kind')})."]
    # Ancestry is the answer to "who does this actually reach" - the same words sit under a
    # product category and under Employment, meaning opposite audiences.
    if (path := hit.get("path")) and len(path) > 1:
        facts.append(f"- sits under: {' > '.join(path[:-1])}")
    if hit.get("rationale"):
        facts.append(f"- why it was picked: {hit['rationale']}")
    if hit.get("negative"):
        facts.append("- this one is EXCLUDED, not targeted.")
    return ToolResult(success=True, summary="\n".join(facts))


async def _edit_audience(params: dict, context: dict) -> ToolResult:
    edits = params.get("edits") or []
    if not isinstance(edits, list) or not edits:
        return ToolResult(success=False, error="`edits` must be a non-empty list.")
    if len(edits) > _MAX_EDITS:
        return ToolResult(
            success=False,
            error=f"Too many edits in one call ({len(edits)}); {_MAX_EDITS} max.",
        )

    # Imported here: audience_update lives in the campaign tools layer, which imports the
    # audience package — a module-level import would close the cycle.
    from app.agents.adzump.agents.campaign.tools.google.audience_update import (
        apply_edit,
        emit_panel,
    )

    done: list[str] = []
    failed: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        ok, message = await apply_edit(edit, context)
        (done if ok else failed).append(message)

    if not done:
        return ToolResult(
            success=False,
            error="No edit applied. "
            + " ".join(failed[: constants.ERROR_ITEMS_DISPLAY_MAX]),
        )
    # Re-emit the audience block so the panel matches the mutated set — the SAME keyed
    # upsert the panel's own click path does. Without this the panel silently shows the old
    # audience after a spoken edit.
    await emit_panel(context, context.get("session_context") or {})
    logger.info("aud_edit applied=%d rejected=%d", len(done), len(failed))
    summary = " ".join(done)
    if failed:
        summary += f" ({len(failed)} rejected: {failed[0]})"
    return ToolResult(success=True, summary=summary, data={"applied": len(done)})


LOOKUP_SEGMENT = ToolDefinition(
    name="lookup_segment",
    description=(
        "What we recorded about ONE segment: whether the campaign targets it, why it was "
        "picked, and where it sits in Google's tree. Use this FIRST for any 'why is X "
        "targeted' / 'who does X reach' question — answer from the record, not a guess."
    ),
    display_name="Look Up Segment",
    parameters=[
        ToolParameter(
            name="segment",
            type="string",
            description="What the user called it — the panel label, or a resource name.",
            required=True,
        )
    ],
    execute=_lookup_segment,
)

EDIT_AUDIENCE = ToolDefinition(
    name="edit_audience",
    description=(
        "Change the saved audience: add or drop segments, or replace the demographic "
        "narrowing. Batch every change from one request into a single call. This is the "
        "ONLY way to change a saved audience — never re-submit the whole set. Every ref "
        "must come from search_audience_segments; invented ones are rejected."
    ),
    display_name="Edit Audience",
    parameters=[
        ToolParameter(
            name="edits",
            type="array",
            description="The changes to apply, in order.",
            required=True,
            items={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "delete", "set_demographics"],
                    },
                    "ref": {
                        "type": "string",
                        "description": "add/delete — the segment, from search_audience_segments.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "add only — why this audience fits.",
                    },
                    "age_ranges": {
                        "type": "array",
                        "description": (
                            "set_demographics only. Each: "
                            '{"min_age": 18|25|35|45|55|65, "max_age": 24|34|44|54|64 or omitted}. '
                            "Sending the complete set replaces the previous narrowing; "
                            "send none of the demographic fields to clear it."
                        ),
                    },
                    "genders": {
                        "type": "array",
                        "description": "set_demographics only. MALE and/or FEMALE.",
                    },
                    "income_ranges": {
                        "type": "array",
                        "description": (
                            "set_demographics only. Percentile bands, not amounts: "
                            "INCOME_RANGE_90_UP (top 10%), _80_90, _70_80, _60_70, _50_60, _0_50."
                        ),
                    },
                    "parental_statuses": {
                        "type": "array",
                        "description": "set_demographics only. PARENT and/or NOT_A_PARENT.",
                    },
                },
                "required": ["action"],
            },
        )
    ],
    execute=_edit_audience,
)

MANAGE_TOOLS = [LOOKUP_SEGMENT, EDIT_AUDIENCE]
