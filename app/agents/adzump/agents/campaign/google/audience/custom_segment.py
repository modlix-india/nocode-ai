"""Custom segments — describing an audience Google's catalogue has no segment for.

Two tools, deliberately split so the write is gated:

  draft_custom_segment    real search terms + volume. Read-only, creates nothing.
  submit_custom_segment   creates the CustomAudience and targets it.

submit refuses without a draft, so the user's confirmation sits between them structurally
rather than depending on the model asking first.

Terms come from the autosuggest and Keyword Planner adapters, not the keyword agent's tools -
those read a keyword run's kw_* state (including get_theme(kw_type)) and reusing them would
mean pretending to be one.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from app.agents.adzump.adapters import autosuggest
from app.agents.adzump.adapters.google import custom_audience, keyword_planner
from app.agents.adzump.adapters.google.client import GoogleAdsApiError
from app.agents.adzump.agents.campaign.google.audience import constants
from app.agents.adzump.agents.campaign.google.audience.models import (
    AudienceSignal,
    CustomSegmentTerm,
    CustomSegmentUrl,
    SignalKind,
    SignalSource,
)
from app.agents.adzump.agents.campaign.google.emitter import parse_mutate_errors
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# Seeds come from the agent, not a separate generation phase: it already holds the business
# brief and the user's request, so asking it for phrasings costs nothing. Bounded so a
# runaway list cannot fan out across three autosuggest sources.
_MAX_SEEDS = 8
_MAX_PLANNER_SEEDS = 150  # autosuggest phrasings sent TO the Planner, capping API calls
# Kept FROM the Planner after its own sort by volume. It expands well beyond the seeds, so
# without a cap the agent gets hundreds of terms to choose ten from. Far smaller than the
# keyword agent's 600: that builds whole ad groups, this needs 10-15.
_MAX_CANDIDATES = 60
_NAME_MAX = 80


def _state(context: dict) -> dict:
    return context.get("session_context") or {}


def _planner_args(state: dict, context: dict) -> dict:
    # geo rides the campaign's own country. Without it the Planner falls back to its India
    # default, and a US segment would be filtered and ranked on Indian search volume.
    geo = state.get("aud_geo") or None
    return keyword_planner.planner_call_args(
        customer_id=state.get("aud_customer_id", ""),
        login_customer_id=state.get("aud_login_customer_id", ""),
        client_code=context.get("client_code", ""),
        auth_headers=context.get("headers", {}),
        geo_target_constants=geo,
    )


async def _draft_custom_segment(params: dict, context: dict) -> ToolResult:
    raw = params.get("themes") or []
    if isinstance(raw, str):  # a model that sent one string rather than a list
        raw = [raw]
    themes = list(dict.fromkeys(str(t).strip() for t in raw if str(t).strip()))
    if not themes:
        return ToolResult(success=False, error="At least one theme is required.")
    state = _state(context)
    if not state.get("aud_customer_id"):
        return ToolResult(success=False, error="No ad account set for this campaign.")

    # The product-qualified variant of the first theme finds brand-adjacent phrasings the
    # generic ones miss; it is one seed, so it cannot crowd out the agent's own.
    seeds = list(themes)
    if product := str(state.get("aud_product_name") or "").strip():
        seeds.append(f"{product} {themes[0]}")

    try:
        suggestions = await autosuggest.fetch_suggestions(
            seeds[:_MAX_SEEDS], hl="en", gl=state.get("aud_country") or "US"
        )
    except Exception as exc:
        logger.warning("custom segment expansion failed: %s", str(exc)[:200])
        suggestions = []

    # The agent's own themes lead: autosuggest may return nothing for a niche phrase, and
    # those words are still the truest statement of what the user asked for.
    planner_seeds = list(dict.fromkeys([*themes, *(s.keyword for s in suggestions)]))[
        :_MAX_PLANNER_SEEDS
    ]
    try:
        # Ideas, not historical metrics: the Planner EXPANDS beyond the seeds, and that
        # expansion is where the terms a user would not have thought of come from. Scoring
        # the exact seed list instead would throw all of it away. The business URL rides
        # along as a keywordAndUrlSeed so Google reads the landing page too.
        ideas = await keyword_planner.fetch_keyword_ideas(
            planner_seeds,
            url=state.get("aud_business_url") or None,
            **_planner_args(state, context),
        )
    except keyword_planner.PlannerUnavailable:
        # The breaker is open. Say so - "no demand" would be a different, wrong answer.
        return ToolResult(
            success=False,
            error="Keyword data is temporarily unavailable - ask the user to retry shortly.",
        )
    except Exception as exc:
        logger.warning("custom segment ideas failed: %s", str(exc)[:200])
        return ToolResult(
            success=False,
            error="Could not find search terms just now - try again in a moment.",
        )

    # A term nobody searches reaches nobody, so volume is the filter. Keep the shape valid
    # here too: a candidate the model cannot legally submit should never be offered.
    # fetch_keyword_ideas already returns highest volume first, so the cap keeps the top.
    scored: list[dict] = []
    for idea in ideas:
        if int(idea.get("volume") or 0) <= 0:
            continue
        try:
            term = CustomSegmentTerm(
                keyword=idea["keyword"], volume=int(idea["volume"])
            )
        except ValidationError:
            continue
        scored.append(term.model_dump())
        if len(scored) >= _MAX_CANDIDATES:
            break

    if not scored:
        return ToolResult(
            success=True,
            summary=(
                f"No searched terms found for: {', '.join(themes)}. Tell the user plainly "
                "that Google has neither a segment nor search demand for it - do not "
                "invent terms."
            ),
            data={"terms": []},
        )

    state["aud_custom_theme"] = themes[0]
    state["aud_custom_candidates"] = scored
    listing = ", ".join(f"{t['keyword']} ({t['volume']})" for t in scored[:30])
    return ToolResult(
        success=True,
        summary=(
            f"{len(scored)} searched terms for {', '.join(themes)}: {listing}. "
            f"Show the user the {constants.CUSTOM_SEGMENT_KEYWORD_TARGET_MIN}-"
            f"{constants.CUSTOM_SEGMENT_KEYWORD_TARGET_MAX} strongest and ASK before "
            "creating anything. Nothing has been created yet."
        ),
        data={"terms": scored},
    )


def _resolve_name(existing: list[dict], label: str) -> str:
    """A name no live segment already holds - it must be unique per customer."""
    base = label[:_NAME_MAX]
    taken = {e["name"] for e in existing}
    if base not in taken:
        return base
    for n in range(2, 1000):
        suffix = f" ({n})"
        candidate = base[: _NAME_MAX - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise ValueError(f"no free name for {label!r}")


async def _submit_custom_segment(params: dict, context: dict) -> ToolResult:
    state = _state(context)
    candidates = state.get("aud_custom_candidates") or []
    if not candidates:
        # The gate: turn one drafts, turn two submits. Without it the model could create a
        # segment on the turn it was asked about, before the user has seen the terms.
        return ToolResult(
            success=False,
            error="Draft the terms with draft_custom_segment first and let the user confirm.",
        )

    chosen = [str(t).strip() for t in (params.get("terms") or []) if str(t).strip()]
    if not chosen:
        return ToolResult(success=False, error="No terms provided.")

    allowed = {c["keyword"] for c in candidates}
    unknown = [t for t in chosen if t not in allowed]
    if unknown:
        return ToolResult(
            success=False,
            error=(
                f"These were not in the draft: {', '.join(unknown[:5])}. "
                "Choose only from the drafted terms - invented ones reach nobody."
            ),
        )

    try:
        terms = [CustomSegmentTerm(keyword=t) for t in dict.fromkeys(chosen)]
    except ValidationError as exc:
        return ToolResult(
            success=False, error=str(exc.errors()[0].get("msg", "invalid term"))
        )

    urls: list[str] = []
    if raw_url := str(params.get("url") or "").strip():
        try:
            urls.append(CustomSegmentUrl(url=raw_url).url)
        except ValidationError as exc:
            return ToolResult(
                success=False, error=str(exc.errors()[0].get("msg", "invalid url"))
            )

    label = str(params.get("label") or state.get("aud_custom_theme") or "").strip()
    if not label:
        return ToolResult(success=False, error="A `label` is required.")

    account = {
        "customer_id": state.get("aud_customer_id", ""),
        "login_customer_id": state.get("aud_login_customer_id", ""),
        "client_code": context.get("client_code", ""),
        "auth_headers": context.get("headers", {}),
    }
    try:
        existing = await custom_audience.list_enabled(**account)
        # A colliding name only gets a suffix - the segment still has to carry the terms the
        # user just approved, so reusing an older one and reporting the new terms is wrong.
        name = _resolve_name(existing, label)
        resource_name = await custom_audience.create(
            name=name,
            description=f"{constants.OWNED_MARKER}{state.get('aud_product_name', '')}",
            keywords=[t.keyword for t in terms],
            urls=urls,
            **account,
        )
    except GoogleAdsApiError as exc:
        detail = "; ".join(parse_mutate_errors(exc.payload)) or str(exc)[:200]
        logger.warning("custom segment create failed: %s", detail)
        hint = (
            " Custom segments cannot be created on a manager account - use the client account."
            if "ACTION_NOT_PERMITTED" in detail or "OPERATION_NOT_PERMITTED" in detail
            else ""
        )
        return ToolResult(
            success=False, error=f"Google refused to create the segment: {detail}{hint}"
        )
    except Exception as exc:
        logger.warning("custom segment create failed: %s", str(exc)[:300])
        return ToolResult(
            success=False, error="Could not create the segment - try again in a moment."
        )

    from app.agents.adzump.agents.campaign.tools.google.audience_update import (
        add_signal,
        emit_panel,
    )

    ok, message = add_signal(
        state,
        AudienceSignal(
            kind=SignalKind.CUSTOM_AUDIENCE,
            ref=resource_name,
            label=label,
            source=SignalSource.GENERATED,
            rationale=f"people searching: {', '.join(t.keyword for t in terms[:5])}",
            owned=True,
        ),
    )
    if not ok:
        return ToolResult(success=False, error=message)

    state.pop("aud_custom_candidates", None)  # spent - a new request drafts again
    await emit_panel(context, state)
    logger.info("custom segment %s (%d terms)", resource_name, len(terms))
    return ToolResult(
        success=True,
        summary=f"{message} Targeting people who search those terms.",
        data={"resource_name": resource_name, "terms": len(terms)},
    )


DRAFT_CUSTOM_SEGMENT = ToolDefinition(
    name="draft_custom_segment",
    description=(
        "Find real search terms for an audience Google's catalogue has no segment for. "
        "Read-only - it creates NOTHING, it only shows what people actually search. Use it "
        "when search_audience_segments came back empty, or when the user asks to target "
        "people searching for something. Show the user the terms and ask before submitting."
    ),
    display_name="Draft Custom Segment",
    parameters=[
        ToolParameter(
            name="themes",
            type="array",
            description=(
                "What the audience is after — the user's own words FIRST, then phrasings "
                "real people would type for it. Three to six is plenty. These seed the "
                "search-term expansion, so breadth here is what makes the segment good; one "
                "phrase only ever explores one direction."
            ),
            required=True,
        )
    ],
    execute=_draft_custom_segment,
)

SUBMIT_CUSTOM_SEGMENT = ToolDefinition(
    name="submit_custom_segment",
    description=(
        "Create the custom segment and target it. Call ONLY after draft_custom_segment and "
        "after the user has agreed - this creates a real resource in their account. Terms "
        "must come from the draft."
    ),
    display_name="Create Custom Segment",
    parameters=[
        ToolParameter(
            name="terms",
            type="array",
            description=(
                "The chosen search terms, verbatim from the draft. Aim for "
                "10-15 - the strongest intent, not the highest volume."
            ),
            required=True,
        ),
        ToolParameter(
            name="label",
            type="string",
            description="Short name for this audience, e.g. 'Home loan seekers'.",
            required=True,
        ),
        ToolParameter(
            name="url",
            type="string",
            description=(
                "Optional. A site whose visitors describe this audience - the business's "
                "own URL, or a competitor's. Must include https://."
            ),
            required=False,
        ),
    ],
    execute=_submit_custom_segment,
)

CUSTOM_SEGMENT_TOOLS = [DRAFT_CUSTOM_SEGMENT, SUBMIT_CUSTOM_SEGMENT]
