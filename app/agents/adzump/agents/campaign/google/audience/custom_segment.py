"""Custom segments — describing an audience Google's catalogue has no segment for.

Draft and submit are deliberately split so the write is gated:

  draft_custom_segment    real search terms + volume. Read-only.
  submit_custom_segment   records a blueprint and targets it. publish.py creates the
                          CustomAudience at launch, so it stays editable until then.
  edit_custom_segment     members by the listful, the same path the panel clicks.

submit refuses without a draft, so the user's confirmation sits between them structurally
rather than depending on the model asking first.

Terms come from the autosuggest and Keyword Planner adapters, not the keyword agent's tools -
those read a keyword run's kw_* state (including get_theme(kw_type)) and reusing them would
mean pretending to be one.
"""

from __future__ import annotations

import itertools
import logging
import uuid

from pydantic import ValidationError

from app.agents.adzump.adapters import autosuggest
from app.agents.adzump.adapters.google import keyword_planner
from app.agents.adzump.agents.campaign.google.audience import constants
from app.agents.adzump.agents.campaign.google.audience.constants import (
    BLUEPRINTS_KEY,
    pending_ref,
)
from app.agents.adzump.agents.campaign.google.audience.models import (
    AudienceSignal,
    CustomSegmentApp,
    CustomSegmentTerm,
    CustomSegmentUrl,
    SignalKind,
    SignalSource,
)
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

# Stamped on every draft so the parent session can tell a redraft from the list it has
# already offered - see carry_draft, which expires one and not the other.
DRAFT_ID_KEY = "aud_custom_draft_id"


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
        logger.warning(
            "custom segment expansion failed: %s",
            str(exc)[: constants.LOG_ERROR_MAX_CHARS],
        )
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
        logger.warning(
            "custom segment ideas failed: %s", str(exc)[: constants.LOG_ERROR_MAX_CHARS]
        )
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
    state[DRAFT_ID_KEY] = uuid.uuid4().hex
    listing = ", ".join(
        f"{t['keyword']} ({t['volume']})"
        for t in scored[: constants.DRAFT_TERMS_DISPLAY_MAX]
    )
    return ToolResult(
        success=True,
        summary=(
            f"{len(scored)} searched terms for {', '.join(themes)}: {listing}. "
            "Expansion drifts wider than the themes, so most of these will be about the "
            "category rather than the request - take only the ones that mean what the user "
            "ASKED FOR, however small their volume, and drop the rest. Two differing by "
            "word order, a plural or a preposition are one intent twice. At most "
            f"{constants.CUSTOM_SEGMENT_KEYWORD_TARGET_MAX}, and as few as that leaves - "
            "never pad to a number. Show them and ASK before creating anything. Nothing "
            "has been created yet."
        ),
        data={"terms": scored},
    )


def resolve_name(existing: list[dict], label: str) -> str:
    """A name no live segment already holds - it must be unique per customer."""
    base = label[:_NAME_MAX]
    taken = {e["name"] for e in existing}
    if base not in taken:
        return base
    for n in itertools.count(2):
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

    # Keep each term's drafted volume - the panel shows it, and rebuilding from the keyword
    # alone would record every approved term as zero demand.
    allowed = {c["keyword"]: int(c.get("volume") or 0) for c in candidates}
    unknown = [t for t in chosen if t not in allowed]
    if unknown:
        return ToolResult(
            success=False,
            error=(
                f"These were not in the draft: {', '.join(unknown[: constants.ERROR_ITEMS_DISPLAY_MAX])}. "
                "Choose only from the drafted terms - invented ones reach nobody."
            ),
        )

    try:
        terms = [
            CustomSegmentTerm(keyword=t, volume=allowed[t])
            for t in dict.fromkeys(chosen)
        ]
    except ValidationError as exc:
        return ToolResult(
            success=False, error=str(exc.errors()[0].get("msg", "invalid term"))
        )

    # `url` singular is still accepted: it is what the tool took before, and a model that
    # learned the old shape should not silently lose the value.
    raw_urls = params.get("urls") or ([params["url"]] if params.get("url") else [])
    # Their own site, seeded rather than left to the model to remember.
    own = str(state.get("aud_business_url") or "").strip()
    if own and not any(str(u).strip() == own for u in raw_urls):
        raw_urls = [*raw_urls, own]
    try:
        urls = [CustomSegmentUrl(url=str(u)).url for u in raw_urls if str(u).strip()]
        apps = [
            CustomSegmentApp(app=str(a)).app
            for a in params.get("apps") or []
            if str(a).strip()
        ]
    except ValidationError as exc:
        return ToolResult(
            success=False, error=str(exc.errors()[0].get("msg", "invalid member"))
        )

    label = str(params.get("label") or state.get("aud_custom_theme") or "").strip()
    if not label:
        return ToolResult(success=False, error="A `label` is required.")

    # Creates nothing: publish materialises this at launch, so an abandoned campaign and a
    # dry run both leave the account untouched.
    ref = pending_ref(label)
    blueprints = dict(state.get(BLUEPRINTS_KEY) or {})
    blueprints[ref] = {
        "label": label,
        "terms": [t.model_dump() for t in terms],
        "urls": urls,
        "apps": apps,
    }

    from app.agents.adzump.agents.campaign.tools.google.audience_update import (
        add_signal,
        emit_panel,
    )

    ok, message = add_signal(
        state,
        AudienceSignal(
            kind=SignalKind.CUSTOM_AUDIENCE,
            ref=ref,
            label=label,
            source=SignalSource.GENERATED,
            rationale=f"people searching: {', '.join(t.keyword for t in terms[: constants.ERROR_ITEMS_DISPLAY_MAX])}",
            owned=True,
        ),
    )
    if not ok:
        return ToolResult(success=False, error=message)

    state[BLUEPRINTS_KEY] = blueprints
    state.pop("aud_custom_candidates", None)  # spent - a new request drafts again
    state.pop(DRAFT_ID_KEY, None)
    await emit_panel(context, state)
    logger.info("custom segment blueprint %s (%d terms)", ref, len(terms))
    return ToolResult(
        success=True,
        summary=(
            f"{message} Targeting people who search those terms. It is created in the "
            "account when the campaign launches, not now."
        ),
        data={"ref": ref, "terms": len(terms)},
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
                "What the audience is after — the user's own words FIRST, then other ways a "
                "real person would type THAT request. Three to six is plenty. Each theme is "
                "expanded into dozens of terms, so one drawn from the business's other lines "
                "floods the result with those instead of what was asked for."
            ),
            required=True,
        )
    ],
    execute=_draft_custom_segment,
)

SUBMIT_CUSTOM_SEGMENT = ToolDefinition(
    name="submit_custom_segment",
    description=(
        "Add the custom segment to the campaign. Call ONLY after draft_custom_segment and "
        "after the user has agreed. Nothing is created in their account yet - it is built "
        "at launch, so the terms stay editable until then. Terms must come from the draft."
    ),
    display_name="Create Custom Segment",
    parameters=[
        ToolParameter(
            name="terms",
            type="array",
            description=(
                "The chosen search terms, verbatim from the draft - the ones that mean what "
                "the user asked for, however small their volume. Take as few as that leaves; "
                "the next request adds to the segment."
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
            name="urls",
            type="array",
            description=(
                "Optional. Sites whose visitors describe this audience - the business's "
                "own, or competitors'. Each must include https://."
            ),
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="apps",
            type="array",
            description=(
                "Optional. ANDROID package names of apps this audience uses, e.g. "
                "'com.nobroker.app'. Only ones the user gave you - never guess a package "
                "name, and iOS apps cannot be targeted this way."
            ),
            required=False,
            items={"type": "string"},
        ),
    ],
    execute=_submit_custom_segment,
)


async def _volumes(keywords: list[str], state: dict, context: dict) -> dict[str, int]:
    """Real search volume per keyword, in ONE Planner call - a segment whose added terms all
    read zero looks broken beside the drafted ones. Unknown on failure, not fatal."""
    try:
        metrics = await keyword_planner.fetch_keyword_historical_metrics(
            keywords, **_planner_args(state, context)
        )
    except Exception as exc:
        logger.warning(
            "custom segment volume lookup failed: %s",
            str(exc)[: constants.LOG_ERROR_MAX_CHARS],
        )
        return {}
    return {
        str(m.get("keyword") or ""): int(m.get("volume") or 0) for m in (metrics or [])
    }


async def _edit_custom_segment(params: dict, context: dict) -> ToolResult:
    """Add or remove members by conversation, through the SAME path the panel clicks.

    Takes a list because people ask in lists ("add these five"). One member per call cost a
    turn each against MAX_TURNS, so a long list used to stop half-applied with no sign which
    half landed.
    """
    from app.agents.adzump.agents.campaign.tools.google.audience_update import (
        MEMBERS,
        apply_member_edit,
        emit_panel,
    )

    state = _state(context)
    blueprints = state.get(BLUEPRINTS_KEY) or {}
    if not blueprints:
        return ToolResult(
            success=False,
            error="No custom segment to edit - build one with draft_custom_segment first.",
        )

    named = str(params.get("segment") or "").strip().casefold()
    refs = [
        ref
        for ref, plan in blueprints.items()
        if not named or named in str(plan.get("label", "")).casefold()
    ]
    if len(refs) != 1:
        labels = ", ".join(p.get("label", "") for p in blueprints.values())
        return ToolResult(
            success=False, error=f"Say which segment you mean - there are: {labels}."
        )

    action = str(params.get("action") or "")
    _, _, kind = action.partition("_")
    if kind not in MEMBERS:
        return ToolResult(success=False, error=f"Invalid action '{action}'.")
    field = MEMBERS[kind].field
    values = [str(v).strip() for v in (params.get(field) or []) if str(v).strip()]
    if not values:
        return ToolResult(success=False, error=f"No {field} given for '{action}'.")

    volumes = await _volumes(values, state, context) if action == "add_term" else {}

    applied, refused = [], []
    for value in values:
        edit = {"action": action, "ref": refs[0], MEMBERS[kind].param: value}
        if action == "add_term":
            edit["volume"] = volumes.get(value, 0)
        ok, message = await apply_member_edit(edit, context)
        (applied if ok else refused).append((value, message))

    # Emitted once: the panel redraws per call, and a ten-item list would flash ten times.
    if applied:
        await emit_panel(context, state)
    label = blueprints[refs[0]].get("label", "")
    done = f"{len(applied)} of {len(values)} applied to '{label}'."
    detail = "".join(f" '{v}': {m}" for v, m in refused)
    if not applied:
        return ToolResult(success=False, error=f"None applied.{detail}")
    return ToolResult(
        success=True,
        summary=done + detail,
        data={"applied": len(applied), "refused": len(refused)},
    )


EDIT_CUSTOM_SEGMENT = ToolDefinition(
    name="edit_custom_segment",
    description=(
        "Add or remove search terms, websites or apps on a custom segment the user has "
        "already approved but that has not launched yet. Use this when they want to change "
        "an existing segment - drafting a new one instead would leave them with two. Pass "
        "every value they named in ONE call; one call per value runs the turn budget out "
        "and applies only part of their list."
    ),
    display_name="Edit Custom Segment",
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description="What to do. One action per call, applied to every value given.",
            required=True,
            enum=[
                "add_term",
                "delete_term",
                "add_url",
                "delete_url",
                "add_app",
                "delete_app",
            ],
        ),
        ToolParameter(
            name="segment",
            type="string",
            description="Which segment, by label. Omit when there is only one.",
            required=False,
        ),
        ToolParameter(
            name="terms",
            type="array",
            description="For *_term. Search phrases; their volumes are looked up for you.",
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="urls",
            type="array",
            description="For *_url. Each must include https://.",
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="apps",
            type="array",
            description=(
                "For *_app. ANDROID package names the user gave you, e.g. "
                "'com.example.app' - never invent one."
            ),
            required=False,
            items={"type": "string"},
        ),
    ],
    execute=_edit_custom_segment,
)

CUSTOM_SEGMENT_TOOLS = [
    DRAFT_CUSTOM_SEGMENT,
    SUBMIT_CUSTOM_SEGMENT,
    EDIT_CUSTOM_SEGMENT,
]
