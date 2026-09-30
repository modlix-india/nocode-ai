"""Tools the KeywordResearchAgent uses AFTER generation — to answer, edit and rebuild.

  lookup_keyword     what we recorded about one keyword (in the set / passed over / unseen)
  edit_keywords      add, remove or change keywords — batched
  research_ad_group  run the full research pipeline for an ad group that has none

Both are general capabilities, not one handler per question: "why this?", "why not that?",
"add location keywords", "drop the low-volume ones" are all composed from these plus the
generation tools (expand_keywords, keyword_metrics) and the theme's own guidance.

edit_keywords goes through the SAME ``_apply_edit`` the panel's click path uses, so an edit
made in words cannot break an invariant a click couldn't — and it mutates the saved set in
place rather than re-submitting it, which would fabricate provenance and clobber a
concurrent panel click.
"""

from __future__ import annotations

import asyncio
import logging

from app.agents.adzump.agents.campaign.google.keyword.models import (
    AdGroupStatus,
    BusinessProfile,
    KeywordSet,
    normalize,
)
from app.agents.adzump.agents.campaign.google.keyword.themes import (
    KEYWORD_THEMES,
    get_theme,
)
from app.agents.adzump.agents.campaign.google.keyword.tools import fill_volumes
from app.agents.adzump.agents.campaign.models import (
    keyword_research,
    set_keyword_research,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_MAX_EDITS = 25  # one conversational turn's worth


def _state(context: dict) -> dict:
    return context.get("session_context") or {}


def _themes(state: dict) -> dict[str, dict]:
    # Through the accessor, not the session key: the first edit migrates the set into the
    # build envelope, and a raw read would find nothing from then on - so every later
    # lookup would answer "no record of that keyword" about a keyword sitting in the panel.
    return (keyword_research(state) or {}).get("themes") or {}


def _find(state: dict, kw: str) -> tuple[str, str, dict] | None:
    """(theme_id, section, row) for a keyword already in a set."""
    for tid, kset in _themes(state).items():
        for section in ("positives", "negatives"):
            for row in kset.get(section) or []:
                if normalize(row.get("keyword", "")) == kw:
                    return tid, section, row
    return None


def _rejection(state: dict, kw: str) -> tuple[str, dict] | None:
    """(theme_id, rejection) for a keyword we scored but did not keep."""
    for tid, kset in _themes(state).items():
        for rej in kset.get("rejections") or []:
            if normalize(rej.get("keyword", "")) == kw:
                return tid, rej
    return None


async def _lookup_keyword(params: dict, context: dict) -> ToolResult:
    kw = normalize(str(params.get("keyword", "")))
    if not kw:
        return ToolResult(success=False, error="A `keyword` is required.")
    state = _state(context)

    hit = _find(state, kw)
    if hit:
        tid, section, row = hit
        facts = [f"'{kw}' IS in the {tid} ad group ({section})."]
        if section == "positives":
            for label, key in (
                ("why it was picked", "rationale"),
                ("rule that admitted it", "admitted_by"),
                ("found via", "source"),
                ("from seed", "source_seed"),
            ):
                if row.get(key):
                    facts.append(f"- {label}: {row[key]}")
            if "volume_at_pick" in row:  # 0 is meaningful — brand-protection picks
                facts.append(f"- volume when picked: {row['volume_at_pick']}")
            if row.get("match_type"):
                facts.append(f"- match type: {row['match_type']}")
        elif row.get("reason"):
            facts.append(f"- excluded because: {row['reason']}")
        return ToolResult(success=True, summary="\n".join(facts))

    rej = _rejection(state, kw)
    if rej:
        tid, r = rej
        why = {
            "not_selected": "it was scored but not selected",
            "zero_volume": "it has no Google search volume",
            "overlaps_positive": "it overlaps a keyword we target",
            "unsafe": "it was filtered by the safety rules",
            "duplicate": "it duplicates another keyword",
            "invented": "it was not a real scored candidate",
        }.get(r.get("rule", ""), r.get("rule", "it was not kept"))
        facts = [f"'{kw}' is NOT in the {tid} ad group — {why}."]
        if r.get("volume_at_eval"):
            facts.append(f"- volume when we scored it: {r['volume_at_eval']}")
        if r.get("reason"):
            facts.append(f"- reason recorded at the time: {r['reason']}")
        return ToolResult(success=True, summary="\n".join(facts))

    # No record. Say so — never invent a past reason (keyword_metrics can score it now).
    return ToolResult(
        success=True,
        summary=(
            f"No record of '{kw}': it was never a candidate in this run, so there is no "
            "recorded reason. Say that plainly. If the user wants a judgement, score it "
            "with keyword_metrics and assess it against the offering — and make clear that "
            "is a fresh check, not what happened during the run."
        ),
    )


async def _edit_keywords(params: dict, context: dict) -> ToolResult:
    edits = params.get("edits") or []
    if not isinstance(edits, list) or not edits:
        return ToolResult(success=False, error="`edits` must be a non-empty list.")
    if len(edits) > _MAX_EDITS:
        return ToolResult(
            success=False,
            error=f"Too many edits in one call ({len(edits)}); {_MAX_EDITS} max.",
        )

    # Imported here: keyword_update lives in the campaign tools layer, which imports the
    # keyword package — a module-level import would close the cycle.
    from app.agents.adzump.agents.campaign.tools.google.keyword_update import (
        _apply_edit,
        _emit_panel,
    )

    state = _state(context)

    def needs_volume(e: dict) -> bool:
        # Only a keyword whose text changes: re-pricing an unchanged one would overwrite a
        # real stored volume with a lookup that often returns nothing.
        action = str(e.get("action", "")).lower()
        if not str(e.get("keyword", "")).strip():
            return False
        if action == "add":
            return True
        return action == "edit" and normalize(str(e.get("keyword", ""))) != normalize(
            str(e.get("old_keyword", ""))
        )

    priced = [e for e in edits if isinstance(e, dict) and needs_volume(e)]
    if priced:
        await fill_volumes(context, priced)

    done: list[str] = []
    failed: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        ok, message = _apply_edit(edit, state)
        (done if ok else failed).append(message)

    if not done:
        return ToolResult(
            success=False,
            error="No edit applied. " + " ".join(failed[:3]),
        )
    # Without this the panel silently shows the old set after a spoken edit. Same keyed
    # upsert the panel's own click path uses, so it replaces in place without a flash.
    await _emit_panel(context, state)
    logger.info(
        "kw_edit applied=%d rejected=%d themes=%s",
        len(done),
        len(failed),
        ",".join(sorted(_themes(state))),
    )
    summary = " ".join(done)
    if failed:
        summary += f" ({len(failed)} rejected: {failed[0]})"
    return ToolResult(success=True, summary=summary, data={"applied": len(done)})


async def _research_ad_group(params: dict, context: dict) -> ToolResult:
    theme_id = str(params.get("keyword_type", "")).strip().lower()
    if theme_id not in KEYWORD_THEMES:
        return ToolResult(
            success=False,
            error=f"Unknown ad group '{theme_id}'. Known: {', '.join(sorted(KEYWORD_THEMES))}.",
        )
    state = _state(context)
    wanted = state.get("kw_wanted") or []
    if wanted and theme_id not in wanted:
        return ToolResult(
            success=False,
            error=(
                f"The user did not ask for a {get_theme(theme_id).label} ad group. Which ad "
                "groups to build is their choice, made at the consent step - offer it, do "
                "not create it."
            ),
        )
    dump = keyword_research(state)
    if not dump:
        return ToolResult(success=False, error="No keyword research in this session.")
    existing = (dump.get("themes") or {}).get(theme_id) or {}
    if existing.get("positives"):
        return ToolResult(
            success=False,
            error=(
                f"The {get_theme(theme_id).label} ad group already has keywords. Edit them "
                "with edit_keywords - researching again would discard the user's review."
            ),
        )

    # Imported here: both live in the campaign tools layer, which imports this package.
    from app.agents.adzump.agents.campaign.google.keyword.agent import (
        KeywordResearchAgent,
    )
    from app.agents.adzump.agents.campaign.tools.google.keyword_research import (
        _RESEARCH_TIMEOUT_SECONDS,
    )

    # The research singleton, never `self`: the pipeline needs the generation tools to submit.
    agent = KeywordResearchAgent.get_instance()
    partials: dict[str, KeywordSet] = {}
    result: KeywordSet | None
    try:
        result = await asyncio.wait_for(
            agent.research(
                keyword_type=theme_id,
                business_text=state.get("kw_business_text", ""),
                ad_account={
                    "customer_id": state.get("kw_customer_id", ""),
                    "login_customer_id": state.get("kw_login_customer_id", ""),
                },
                geo={
                    "geo_target_constants": state.get("kw_geo") or [],
                    "hl": state.get("kw_hl", "en"),
                    "gl": state.get("kw_gl", "US"),
                    "language": state.get("kw_language", ""),
                },
                parent_event_stream=context["event_stream"],
                auth=context["auth"],
                category=state.get("kw_category", ""),
                core_terms=list(state.get("kw_core_terms") or []),
                siblings=list(state.get("kw_siblings") or []),
                # This theme's own surfaces, not the manage session's union: BRAND takes no
                # informational sources.
                sources=BusinessProfile(
                    category=state.get("kw_category", ""),
                    includes_informational_funnel=bool(state.get("kw_informational")),
                ).source_names(theme_id),
                location=state.get("kw_location", ""),
                service_areas=list(state.get("kw_service_areas") or []),
                business_url=state.get("kw_business_url", ""),
                partial_sink=partials,
            ),
            _RESEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        # The caller hung up. Re-raised, or the manage loop keeps spending on a dead request.
        raise
    except Exception as exc:  # noqa: BLE001 - a timeout still leaves usable phases
        result = partials.get(theme_id)
        if result is None or not result.positives:
            logger.warning(
                "research_ad_group %s failed: %s", theme_id, type(exc).__name__
            )
            return ToolResult(
                success=False,
                error=f"Researching the {get_theme(theme_id).label} ad group did not finish.",
            )

    if result is None or not result.positives:
        return ToolResult(
            success=False,
            error=f"The {get_theme(theme_id).label} ad group produced no usable keywords.",
        )

    # Re-read: research ran for minutes, so the earlier copy predates any edit since.
    dump = keyword_research(state) or dump
    themes = dump.get("themes") or {}
    themes[theme_id] = result.model_dump(mode="json")
    dump["themes"] = themes
    meta = dump.get("meta") or {}
    for key in (AdGroupStatus.PENDING.value, AdGroupStatus.FAILED.value):
        # Left behind it renders a ghost tab beside the real one.
        if theme_id in (meta.get(key) or []):
            meta[key] = [t for t in meta[key] if t != theme_id]
    dump["meta"] = meta
    set_keyword_research(state, dump)

    from app.agents.adzump.agents.campaign.tools.google.keyword_update import (
        _emit_panel,
    )

    await _emit_panel(context, state)
    logger.info(
        "research_ad_group type=%s positives=%d negatives=%d status=%s",
        theme_id,
        len(result.positives),
        len(result.negatives),
        result.status.value,
    )
    return ToolResult(
        success=True,
        summary=(
            f"Researched the {result.label or theme_id} ad group: "
            f"{len(result.positives)} positives, {len(result.negatives)} negatives."
        ),
        data={"keyword_type": theme_id, "status": result.status.value},
    )


LOOKUP_KEYWORD = ToolDefinition(
    name="lookup_keyword",
    description=(
        "What we recorded about ONE keyword: whether it is in an ad group and why it was "
        "picked, or that it was passed over and why, or that it was never a candidate. "
        "Use this FIRST for any 'why is X here' / 'why isn't X here' question — answer "
        "from the record, not from a guess."
    ),
    display_name="Look Up Keyword",
    parameters=[
        ToolParameter(
            name="keyword",
            type="string",
            description="The keyword the user asked about, verbatim.",
            required=True,
        )
    ],
    execute=_lookup_keyword,
)

EDIT_KEYWORDS = ToolDefinition(
    name="edit_keywords",
    description=(
        "Add, remove, or change keywords in the saved ad groups. Batch every change from "
        "one request into a single call. This is the ONLY way to change a saved set — "
        "never re-submit the whole set."
    ),
    display_name="Edit Keywords",
    parameters=[
        ToolParameter(
            name="edits",
            type="array",
            description="The changes to apply, in order.",
            required=True,
            items={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "delete", "edit"]},
                    "keyword_type": {
                        "type": "string",
                        "description": "The ad group id to change (e.g. brand, generic).",
                    },
                    "section": {"type": "string", "enum": ["positives", "negatives"]},
                    "keyword": {"type": "string"},
                    "old_keyword": {
                        "type": "string",
                        "description": "edit only — the keyword being replaced.",
                    },
                    "match_type": {
                        "type": "string",
                        "description": "positives: EXACT|PHRASE. negatives: PHRASE|BROAD.",
                    },
                    "intent": {"type": "string", "description": "positives only."},
                    "reason": {
                        "type": "string",
                        "description": "negatives only — why excluded.",
                    },
                },
                "required": ["action", "keyword_type", "section"],
            },
        )
    ],
    execute=_edit_keywords,
)

RESEARCH_AD_GROUP = ToolDefinition(
    name="research_ad_group",
    description=(
        "Run the full research pipeline for an ad group that has NO keywords — one that "
        "failed or never ran. This is the ONLY way to create an ad group; edit_keywords "
        "cannot add to one that does not exist. Refuses an ad group that already has "
        "keywords, so it can never discard the user's review."
    ),
    display_name="Research Ad Group",
    parameters=[
        ToolParameter(
            name="keyword_type",
            type="string",
            description="The ad group id to research (e.g. brand, generic).",
            required=True,
        )
    ],
    execute=_research_ad_group,
)

MANAGE_TOOLS = [LOOKUP_KEYWORD, EDIT_KEYWORDS, RESEARCH_AD_GROUP]
