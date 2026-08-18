"""Campaign craft panel builder.

Single responsibility: assemble and emit the campaign side-panel craft card.
Platform-aware via explicit dispatch (_google_campaign_blocks / _meta_campaign_blocks);
new platforms add a section builder + one dispatch branch here — nowhere else.

Callers:
  keyword_research.py   — emit_campaign_craft() after initial research completes
  audience_targeting.py — same, for a Demand Gen campaign's audience
  keyword_update.py     — emit_section_update() for add/delete/edit (append=True, no flash)
  Future campaign tools add their own sections inside the platform section builders.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.campaign.google.audience.models import (
    IncomeRange,
    SignalKind,
)
from app.agents.adzump.agents.campaign.google.channel_controls import (
    AD_TYPE_LABEL,
    SURFACES,
    AdType,
    ad_type_for,
    locked_reason,
)
from app.agents.adzump.agents.campaign.google.channel_controls import (
    normalize as normalize_controls,
)
from app.agents.adzump.agents.campaign.google.keyword.models import AdGroupStatus
from app.agents.adzump.agents.campaign.models import (
    audience,
    channel_controls,
    creative,
    keyword_research,
)
from app.agents.adzump.platform import is_google as _is_google
from app.agents.adzump.platform import is_meta as _is_meta

logger = logging.getLogger(__name__)

_LOG_TRUNCATE = 160


# Block builders


def _spec_blocks(spec: dict) -> list[dict]:
    """Common campaign summary: platform badge + key-value row."""
    blocks: list[dict] = []
    platform = str(spec.get("platform") or "")
    if platform:
        blocks.append({"type": "badge", "label": platform})
    kv: list[dict] = []
    if spec.get("channel"):
        # Stored as the API enum (DEMAND_GEN); the panel is read by a person.
        kv.append(
            {"key": "Channel", "value": str(spec["channel"]).replace("_", " ").title()}
        )
    if spec.get("duration"):
        kv.append({"key": "Duration", "value": str(spec["duration"])})
    if spec.get("budget"):
        budget = str(spec["budget"])
        if spec.get("budget_currency"):
            budget = f"{spec['budget_currency']} {budget}"
        kv.append({"key": "Daily Budget", "value": budget})
    if kv:
        blocks.append({"type": "key_value", "items": kv})
    return blocks


def keyword_review_block(dump: dict) -> dict:
    """Exported so update handlers can re-emit only this block (keyed upsert, no panel flash)."""
    tabs: list[dict] = []
    # One tab per generated theme, in generation order. Each set carries its own label,
    # so a new theme needs no change here.
    for key, kset in (dump.get("themes") or {}).items():
        if not kset:
            continue
        label = kset.get("label") or key.replace("_", " ").title()
        pos_rows = [
            {
                "keyword": p.get("keyword", ""),
                "volume": p.get("volume", 0),
                "match_type": p.get("match_type", "PHRASE"),
                "intent": p.get("intent", ""),
            }
            for p in (kset.get("positives") or [])
        ]
        neg_rows = [
            {
                "keyword": n.get("keyword", ""),
                "volume": n.get("volume", 0),
                "match_type": n.get("match_type", "PHRASE"),
                "reason": n.get("reason", ""),
            }
            for n in (kset.get("negatives") or [])
        ]
        # Highest demand first, so review starts with the keywords that matter most.
        pos_rows.sort(key=lambda r: r.get("volume") or 0, reverse=True)
        neg_rows.sort(key=lambda r: r.get("volume") or 0, reverse=True)
        tabs.append(
            {
                "key": key,
                "label": label,
                "status": kset.get("status") or AdGroupStatus.COMPLETE.value,
                "sections": [
                    {
                        "key": "positives",
                        "label": f"Positives ({len(pos_rows)})",
                        "columns": ["keyword", "volume", "match_type", "intent"],
                        "rows": pos_rows,
                        "actions": ["add", "edit", "delete"],
                    },
                    {
                        "key": "negatives",
                        "label": f"Negatives ({len(neg_rows)})",
                        "columns": ["keyword", "volume", "match_type", "reason"],
                        "rows": neg_rows,
                        "actions": ["add", "edit", "delete"],
                    },
                ],
            }
        )
    # Ad groups still running or given up on get a tab too, so the panel accounts for every
    # one the user chose instead of silently showing a subset.
    meta = dump.get("meta") or {}
    for status in (AdGroupStatus.PENDING.value, AdGroupStatus.FAILED.value):
        for key in meta.get(status) or []:
            tabs.append(
                {
                    "key": key,
                    "label": key.replace("_", " ").title(),
                    "status": status,
                    "sections": [],
                }
            )
    return {"id": "keyword_review", "type": "keyword_review", "tabs": tabs}


_EVERYONE = "Everyone"

# Panel order, and the DemographicSpec field each row reads.
_DIMENSIONS = (
    ("age_ranges", "Age"),
    ("genders", "Gender"),
    ("income_ranges", "Household income"),
    ("parental_statuses", "Parental status"),
)


def _demographic_rows(spec: dict) -> list[dict]:
    """Every dimension with the reason it was set that way, narrowed or not.

    Leaving one open is a judgement; shown as a bare "Everyone" it reads as one nobody made.
    """
    ages = [
        f"{r['min_age']}-{r['max_age']}" if r.get("max_age") else f"{r['min_age']}+"
        for r in (spec.get("age_ranges") or [])
    ]
    values = {
        "age_ranges": ", ".join(ages),
        "genders": ", ".join(_titled(spec.get("genders") or [])),
        "income_ranges": ", ".join(
            IncomeRange(i).label for i in spec.get("income_ranges") or []
        ),
        "parental_statuses": ", ".join(_titled(spec.get("parental_statuses") or [])),
    }
    why = spec.get("rationales") or {}
    return [
        {
            "attribute": label,
            "value": values[field] or _EVERYONE,
            "rationale": why.get(field, ""),
        }
        for field, label in _DIMENSIONS
    ]


def _titled(values: list[str]) -> list[str]:
    return [str(v).replace("_", " ").capitalize() for v in values]


def audience_review_block(dump: dict) -> dict:
    """Exported so update handlers can re-emit only this block (keyed upsert, no panel flash).

    Flat sections grouped by signal kind, each row carrying its ancestors as a breadcrumb.
    The agent sees the taxonomy as a tree because it navigates a thousand entries; a handful
    of chosen segments spread across branches reads as a list.
    """
    signals = dump.get("signals") or []
    sections: list[dict] = []
    for kind in SignalKind:
        rows = [
            {
                # Not a column, but the panel needs it: a label is not an identity, and the
                # mutation matches on resource name or bare id.
                "ref": s.get("ref", ""),
                "segment": s.get("label", ""),
                "category": " > ".join((s.get("path") or [])[:-1]),
                "rationale": s.get("rationale", ""),
            }
            for s in signals
            if s.get("kind") == kind.value and not s.get("negative")
        ]
        if rows:
            sections.append(
                {
                    "key": kind.value.lower(),
                    "label": f"{kind.label} ({len(rows)})",
                    "help": kind.help,
                    "columns": ["segment", "category", "rationale"],
                    "rows": rows,
                    # No edit — segments are catalogue refs, not free text; change = delete + add.
                    # No add on custom segments — those are built in chat, not picked from a search.
                    "actions": ["delete"]
                    if kind is SignalKind.CUSTOM_AUDIENCE
                    else ["add", "delete"],
                }
            )

    # Only user lists are excludable (ExclusionSegment has one variant), so exclusions are
    # one section rather than a negatives split inside every kind.
    excluded = [
        {
            "ref": s.get("ref", ""),
            "segment": s.get("label", ""),
            "rationale": s.get("rationale", ""),
        }
        for s in signals
        if s.get("negative")
    ]
    if excluded:
        sections.append(
            {
                "key": "excluded",
                "label": f"Excluded ({len(excluded)})",
                "columns": ["segment", "rationale"],
                "rows": excluded,
            }
        )

    # Always present, even with no rows: "no narrowing" is the common and usually correct
    # answer, and a section the user can open is how they change it.
    demographics = dump.get("demographics") or {}
    sections.append(
        {
            "key": "demographics",
            # "Demographic Filters", not "Demographics": the segment section above is called
            # Detailed Demographics and ADDS people, while this one removes them.
            "label": "Demographic Filters",
            "help": (
                "Applied on top of every segment above. Someone has to match here AND be "
                "in one of those segments, so each filter you set makes the audience "
                "smaller. Leave one as Everyone unless it truly does not apply."
            ),
            "columns": ["attribute", "value", "rationale"],
            "rows": _demographic_rows(demographics),
            # The rows are prose ("25-44", "Top 10%"); the editor needs the values back, and
            # set_demographics REPLACES, so anything it cannot see it silently drops.
            "values": demographics,
            "actions": ["edit"],
        }
    )
    return {"id": "audience_review", "type": "audience_review", "sections": sections}


def channel_controls_block(controls: dict | None, ad_type: AdType) -> dict:
    """Exported so the toggle handler can re-emit only this block (keyed upsert, no flash).

    Every surface is listed, including ones this ad type cannot serve on — those carry the
    reason and are not toggleable. Hiding them would leave the user wondering where YouTube
    in-stream went.
    """
    selected = normalize_controls(controls, ad_type)
    rows = []
    for surface in SURFACES:
        reason = locked_reason(surface, ad_type)
        rows.append(
            {
                "surface": surface.key,
                "label": surface.label,
                "enabled": selected[surface.key],
                "locked": bool(reason),
                "reason": reason,
            }
        )
    return {
        "id": "channel_controls",
        "type": "channel_controls",
        "ad_type": AD_TYPE_LABEL[ad_type],
        "columns": ["label", "enabled", "reason"],
        "rows": rows,
        "actions": ["toggle"],
    }


# Platform section builders


def _google_campaign_blocks(session_ctx: dict) -> list[dict]:
    """All Google campaign craft sections.

    Add new Google-specific sections here as they land (ad copy review,
    quality score panel, etc.). Returns [] when the session has no content yet
    for a section — callers never get empty dividers.
    """
    blocks: list[dict] = []

    dump = keyword_research(session_ctx) or {}
    if dump:
        blocks.append({"type": "divider"})
        blocks.append({"type": "heading", "text": "Keyword Suggestions"})
        blocks.append(keyword_review_block(dump))

    # A build is one channel's, so at most one of these is ever populated.
    aud = audience(session_ctx) or {}
    if aud:
        blocks.append({"type": "divider"})
        # Surfaces before segments: campaign-level shape reads with the summary above it,
        # and the audience is long enough to bury anything under it.
        blocks.append({"type": "heading", "text": "Where Ads Show"})
        blocks.append(
            channel_controls_block(
                channel_controls(session_ctx), ad_type_for(creative(session_ctx))
            )
        )
        blocks.append({"type": "heading", "text": "Audience Targeting"})
        blocks.append(audience_review_block(aud))

    # Future sections (ad copy, quality score …) extend blocks here.
    return blocks


def _meta_campaign_blocks(session_ctx: dict) -> list[dict]:
    """All Meta campaign craft sections — placeholder for future features."""
    return []


#  Emitters


async def emit_campaign_craft(stream, craft_id: str, session_ctx: dict) -> None:
    """Full campaign craft rebuild (append=False).

    Called once after the initial research / creation step. Use
    emit_section_update() for incremental updates (add/delete/edit) to avoid
    a full-panel flash.
    """
    if stream is None:
        logger.info(
            "emit_campaign_craft: stream is None — craft NOT emitted (craft_id=%s)",
            craft_id,
        )
        return
    spec = session_ctx.get("campaign_spec") or {}
    platform = str(spec.get("platform") or "")

    blocks = _spec_blocks(spec)

    if _is_google(platform):
        blocks.extend(_google_campaign_blocks(session_ctx))
    elif _is_meta(platform):
        blocks.extend(_meta_campaign_blocks(session_ctx))

    if not blocks:
        logger.info(
            "emit_campaign_craft: no blocks — craft NOT emitted (craft_id=%s)", craft_id
        )
        return
    try:
        await stream.emit_craft(craft_id, "Campaign", blocks, append=False)
        logger.info(
            "emit_campaign_craft: emitted craft_id=%s blocks=%d types=%s",
            craft_id,
            len(blocks),
            [b.get("type") for b in blocks],
        )
    except Exception as exc:
        # Warning, not debug: the panel is the whole output of a build, so a failure here is
        # the user staring at an empty card - not a detail worth hiding at INFO.
        logger.warning("emit_campaign_craft failed: %s", str(exc)[:_LOG_TRUNCATE])


async def emit_section_update(stream, craft_id: str, block: dict) -> None:
    """Keyed upsert — block MUST carry a stable ``id`` so LazyPrompt finds and replaces it."""
    if stream is None or not craft_id:
        return
    try:
        await stream.emit_craft(craft_id, "Campaign", [block], append=True)
    except Exception as exc:
        logger.warning("emit_section_update failed: %s", str(exc)[:_LOG_TRUNCATE])
