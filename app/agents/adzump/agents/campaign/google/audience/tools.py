"""Tools the audience agent calls during its loop.

Thin wrappers over real I/O or a deterministic validation gate; all judgment stays in the
agent's reasoning:

  fetch_audience_segments   Google's taxonomies -> the tree the agent picks from
  search_audience_segments  narrow that tree by a phrase
  submit_segments           validate refs against what was fetched, record
  submit_demographics       validate against Google's enums, record

Per-run state lives in ``session.context`` under ``aud_*`` keys (plain dicts, so it survives
JSON persistence). Submit re-checks every reference against what was fetched.
"""

from __future__ import annotations

import logging
from collections import Counter

from pydantic import ValidationError

from app.agents.adzump.adapters.google import audience_taxonomy as taxonomy
from app.agents.adzump.agents.campaign.google.audience import catalogue, constants
from app.agents.adzump.agents.campaign.google.audience.models import (
    DIMENSION_FIELDS,
    AudienceSignal,
    AudienceTargetingResult,
    DemographicSpec,
    SignalKind,
    SignalSource,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


def _state(context: dict) -> dict | None:
    """The agent's per-run state bag. None rather than an empty dict when absent: tools
    write results here, and writes into a throwaway would be lost while still reporting
    success."""
    return context.get("session_context")


_TREE_MAX_CHARS = 120_000  # the whole catalogue in one result; the 4000 default cuts ~93%


async def _fetch_audience_segments(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    if state is None:
        return ToolResult(success=False, error="No session context available.")

    # Every live run so far has called this twice. The catalogue is 24h-cached and cannot
    # change mid-run, so a second tree in the history is a pure duplicate of the first.
    if loaded := state.get("aud_candidates"):
        return ToolResult(
            success=True,
            summary=f"{len(loaded)} targetable segments available.",
            model_summary=(
                f"Already loaded - the full tree is in the earlier "
                f"fetch_audience_segments result ({len(loaded)} segments). Scroll back to "
                "it, or call search_audience_segments to narrow it."
            ),
        )

    customer_id = state.get("aud_customer_id")
    if not customer_id:
        return ToolResult(success=False, error="No ad account set for this run.")

    candidates = await catalogue.load(
        customer_id=customer_id,
        channel_type=state.get("aud_channel_type", ""),
        country_code=state.get("aud_country", ""),
        login_customer_id=state.get("aud_login_customer_id", ""),
        client_code=context.get("client_code", ""),
        auth_headers=context.get("headers", {}),
    )
    if not candidates:
        return ToolResult(
            success=False,
            error="No targetable audience segments came back - check the ad account and country.",
        )

    state["aud_candidates"] = candidates
    tree = catalogue.as_tree(candidates)
    # The tree is ~90% of this agent's context and is resent every turn. Logged by depth so
    # a decision to prune deep leaves (leaving them to search) rests on real numbers.
    depths = Counter(len(c["path"]) for c in candidates)
    logger.info(
        "audience catalogue: %d segments, %d chars, depths %s",
        len(candidates),
        len(tree),
        dict(sorted(depths.items())),
    )
    return ToolResult(
        success=True,
        # summary is the chat line; model_summary is what the LLM reads. `data` does NOT
        # reach it - to_tool_result_content falls back to data only when both are empty.
        summary=f"{len(candidates)} targetable segments available.",
        model_summary=tree,
        MAX_RESULT_CHARS=_TREE_MAX_CHARS,
    )


async def _search_audience_segments(params: dict, context: dict) -> ToolResult:
    query = str(params.get("query") or "").strip()
    if not query:
        return ToolResult(success=False, error="No query provided.")
    state = _state(context)
    candidates = (state or {}).get("aud_candidates") or []
    if not candidates:
        return ToolResult(
            success=False, error="Fetch the segments first - nothing to search."
        )
    hits = taxonomy.rank_by_name(candidates, query, lambda c: c["label"])
    if not hits:
        return ToolResult(
            success=True,
            summary=f"Nothing matches {query!r}. Describe the audience with a custom segment instead.",
        )
    shown = hits[: constants.MAX_SEARCH_RESULTS]
    return ToolResult(
        success=True,
        summary=f"{len(hits)} match {query!r}.",
        model_summary=catalogue.as_lines(shown),
    )


async def _submit_segments(params: dict, context: dict) -> ToolResult:
    items = params.get("segments") or []
    if not items:
        return ToolResult(success=False, error="No segments provided.")
    state = _state(context)
    if state is None:
        return ToolResult(success=False, error="No session context available.")
    by_key = catalogue.by_key(state.get("aud_candidates") or [])
    if not by_key:
        return ToolResult(success=False, error="Fetch the segments first.")

    kept: list[dict] = []
    seen: set[str] = set()
    rejected: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("ref") or item.get("id") or "").strip()
        cand = by_key.get(key)
        if not key:
            continue
        if cand is None:
            # The one gate that matters: an id the agent invented would target the wrong
            # people, or nobody, and nothing downstream would notice.
            rejected.append(key)
            continue
        if cand["ref"] in seen:
            continue
        try:
            signal = AudienceSignal(
                kind=SignalKind(cand["kind"]),
                ref=cand["ref"],
                label=cand["label"],
                source=SignalSource.TAXONOMY,
                rationale=str(item.get("rationale") or "").strip(),
                path=cand["path"],
            )
        except ValidationError as exc:
            rejected.append(f"{key} ({exc.error_count()} invalid)")
            continue
        seen.add(cand["ref"])
        kept.append(signal.model_dump(mode="json"))

    if not kept:
        return ToolResult(
            success=False,
            error=f"No valid segments. Unknown refs: {', '.join(rejected[:constants.ERROR_ITEMS_DISPLAY_MAX])}",
        )

    state["aud_segments"] = kept
    result = AudienceTargetingResult(signals=[AudienceSignal(**s) for s in kept])
    over = result.over_cap()
    note = (
        " Over the cap for "
        + ", ".join(f"{k.value} ({n})" for k, n in over.items())
        + " - trim to the strongest."
        if over
        else ""
    )
    if rejected:
        note += f" Dropped {len(rejected)} unknown ref(s)."
    return ToolResult(
        success=True,
        summary=f"{len(kept)} segments recorded.{note}",
        data={"count": len(kept)},
    )


async def _submit_demographics(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    if state is None:
        return ToolResult(success=False, error="No session context available.")
    try:
        spec = DemographicSpec.model_validate(
            {
                "age_ranges": params.get("age_ranges") or [],
                "genders": params.get("genders") or [],
                "income_ranges": params.get("income_ranges") or [],
                "parental_statuses": params.get("parental_statuses") or [],
                "include_undetermined": params.get("include_undetermined") or {},
                "rationales": params.get("rationales") or {},
            }
        )
    except ValidationError as exc:
        return ToolResult(
            success=False,
            error=f"Invalid demographics: {exc.errors()[0].get('msg', 'check the allowed values')}",
        )
    # Refused rather than stored: the panel prints this beside every dimension, and an open
    # one with no reason reads as a step that was skipped, not a decision that was taken.
    if bare := [
        f for f in DIMENSION_FIELDS if not (spec.rationales.get(f) or "").strip()
    ]:
        return ToolResult(
            success=False,
            error=(
                f"Missing `rationales` for {', '.join(bare)}. Send one per dimension, the "
                "ones left open included - there the reason is why narrowing would cost "
                "reach. Resend the whole call."
            ),
        )
    state["aud_demographics"] = spec.model_dump(mode="json")
    if spec.is_empty:
        return ToolResult(
            success=True,
            summary="No demographic narrowing - the campaign reaches every age and gender.",
        )
    return ToolResult(success=True, summary="Demographics recorded.")


FETCH_AUDIENCE_SEGMENTS = ToolDefinition(
    name="fetch_audience_segments",
    description=(
        "Load every audience segment that can serve on this campaign's channel and country, "
        "as a tree. Call this first - segments can only be chosen from what it returns."
    ),
    display_name="Load Audience Segments",
    parameters=[],
    execute=_fetch_audience_segments,
)

SEARCH_AUDIENCE_SEGMENTS = ToolDefinition(
    name="search_audience_segments",
    description=(
        "Find loaded segments matching a phrase. Use when looking for a specific idea; "
        "an empty result means Google has no segment for it."
    ),
    display_name="Search Audience Segments",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="What to look for, in the words the business would use.",
            required=True,
        )
    ],
    execute=_search_audience_segments,
)

SUBMIT_SEGMENTS = ToolDefinition(
    name="submit_segments",
    description=(
        "Record the chosen segments. Send the COMPLETE set every time - this replaces any "
        "previous submission rather than adding to it. Every ref must come from "
        "fetch_audience_segments; invented ids are rejected."
    ),
    display_name="Submit Segments",
    parameters=[
        ToolParameter(
            name="segments",
            type="array",
            description='Each: {"ref": "<from the tree>", "rationale": "why this audience"}.',
            required=True,
        )
    ],
    execute=_submit_segments,
)

SUBMIT_DEMOGRAPHICS = ToolDefinition(
    name="submit_demographics",
    description=(
        "Record age, gender, household income and parental status. Narrow only where the "
        "product genuinely excludes people - every filter shrinks reach."
    ),
    display_name="Submit Demographics",
    parameters=[
        ToolParameter(
            name="age_ranges",
            type="array",
            description='Each: {"min_age": 18|25|35|45|55|65, "max_age": 24|34|44|54|64 or omitted}.',
            required=False,
        ),
        ToolParameter(
            name="genders",
            type="array",
            description="MALE and/or FEMALE. Omit unless the product genuinely excludes one.",
            required=False,
        ),
        ToolParameter(
            name="income_ranges",
            type="array",
            description=(
                "Percentile bands, not amounts: INCOME_RANGE_90_UP (top 10%), _80_90, "
                "_70_80, _60_70, _50_60, _0_50. Must be one unbroken span - Google's own "
                "picker is a top band and a bottom band, so a gap in the middle is refused."
            ),
            required=False,
        ),
        ToolParameter(
            name="parental_statuses",
            type="array",
            description="PARENT and/or NOT_A_PARENT.",
            required=False,
        ),
        ToolParameter(
            name="include_undetermined",
            type="object",
            description=(
                "Per dimension, keyed age_ranges / genders / income_ranges / "
                "parental_statuses. Google cannot classify a large share of users on each; "
                "true (the default) keeps them, false drops them and narrows again on top "
                "of the bands you chose. Send false only where reaching an unclassified "
                "user is genuinely wrong - for income it removes most of the world, since "
                "Google reports it in select countries only."
            ),
            required=False,
        ),
        ToolParameter(
            name="rationales",
            type="object",
            description=(
                "One short reason per dimension, keyed age_ranges / genders / "
                "income_ranges / parental_statuses. Give one for EVERY dimension, the ones "
                "you leave open included - the user is shown why it was not narrowed, and "
                "'Everyone' with no reason reads as a step you skipped."
            ),
            required=True,
        ),
    ],
    execute=_submit_demographics,
)

ALL_TOOLS = [
    FETCH_AUDIENCE_SEGMENTS,
    SEARCH_AUDIENCE_SEGMENTS,
    SUBMIT_SEGMENTS,
    SUBMIT_DEMOGRAPHICS,
]
