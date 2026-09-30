"""audience_update — the audience-review-panel mutation logic.

The panel sends structured actions as pure JSON:
  {"type": "audience_widget", "action": "add|delete", "ref": "<resource name or id>"}
  {"type": "audience_widget", "action": "set_demographics", "age_ranges": [...],
   "genders": [...], "income_ranges": [...], "parental_statuses": [...],
   "include_undetermined": {"<dimension>": bool}}

``apply_edit`` applies one to the saved audience (through ``campaign/models.py``, never by
session key); ``update_audience`` re-emits only the audience_review block. HTTP transport is
in campaign/api.py.

``add`` re-resolves its ref through ``catalogue`` rather than trusting what the caller
attached — see there for why a ref alone is not enough. That also re-checks targetability,
which a stored snapshot could not.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from pydantic import ValidationError

from app.agents.adzump.agents.campaign.craft import (
    audience_review_block,
    emit_section_update,
)
from app.agents.adzump.agents.campaign.google.audience import catalogue
from app.agents.adzump.agents.campaign.google.audience.constants import (
    BLUEPRINTS_KEY,
    CUSTOM_SEGMENT_KEYWORD_TARGET_MAX,
    MAX_SIGNALS_PER_KIND,
    is_pending,
)
from app.agents.adzump.agents.campaign.google.audience.models import (
    AudienceSignal,
    AudienceTargetingResult,
    CustomSegmentApp,
    CustomSegmentTerm,
    CustomSegmentUrl,
    DemographicSpec,
    SignalKind,
    SignalSource,
)
from app.agents.adzump.agents.campaign.models import (
    audience,
    resolve_channel,
    set_audience,
)
from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"add", "delete", "set_demographics"})


class Member(NamedTuple):
    """One member kind. A term's param is "keyword", not "term", so neither half is
    derivable from the action name - holding them together is what stops them drifting."""

    field: str  # the blueprint list it lands in
    param: str  # the key its value arrives under


MEMBERS = {
    "term": Member("terms", "keyword"),
    "url": Member("urls", "url"),
    "app": Member("apps", "app"),
}


def _positive_refs(signals: list[dict]) -> list[str]:
    return [s["ref"] for s in signals if not s.get("negative")]


def _rebuild(dump: dict, signals: list[dict]) -> tuple[bool, str]:
    """Re-assemble through the model rather than patching the dict: the dimension groups
    have to keep partitioning the positives, and a group that gains or loses a ref silently
    changes who the campaign reaches."""
    try:
        result = AudienceTargetingResult(
            signals=[AudienceSignal(**s) for s in signals],
            demographics=DemographicSpec.model_validate(dump.get("demographics") or {}),
            dimension_groups=[_positive_refs(signals)] if signals else [],
            meta=dump.get("meta") or {},
        )
    except ValidationError as exc:
        return False, str(exc.errors()[0].get("msg", "that change is not valid"))
    dump.update(result.model_dump(mode="json"))
    return True, ""


async def _resolve_ref(
    ref: str, session_ctx: dict, dump: dict, context: dict
) -> dict | None:
    """The catalogue entry for a reference, or None if it cannot serve this campaign.

    The index is memoised on the request context: a spoken edit can carry a dozen adds, and
    rebuilding a thousand-entry index per add is wasted work even though the fetch is cached.
    """
    if (index := context.get("_audience_index")) is None:
        spec = session_ctx.get("campaign_spec") or {}
        customer_id = str(spec.get("account") or "").strip()
        if not customer_id:
            return None
        candidates = await catalogue.load(
            customer_id=customer_id,
            channel_type=resolve_channel(spec).google_channel_type.value,
            country_code=str((dump.get("meta") or {}).get("country") or ""),
            login_customer_id=str(spec.get("parent_account") or "").strip(),
            client_code=context.get("client_code", ""),
            auth_headers=context.get("headers", {}),
        )
        index = catalogue.by_key(candidates)
        context["_audience_index"] = index
    return index.get(ref)


async def apply_edit(params: dict, context: dict) -> tuple[bool, str]:
    """Apply ONE add/delete/set_demographics to the saved audience. Returns (ok, message).

    The single mutation path: panel clicks (0 LLM) and the audience agent both come through
    here, so an edit made in words cannot break an invariant a click couldn't.
    """
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return False, "No session context available."
    dump = audience(session_ctx)
    if not dump:
        return False, "No audience in session — build the campaign first."

    action = str(params.get("action", "")).lower()
    if action not in _VALID_ACTIONS:
        return (
            False,
            f"Invalid action '{action}'. Must be: add, delete, or set_demographics.",
        )

    signals: list[dict] = list(dump.get("signals") or [])

    if action == "set_demographics":
        try:
            spec = DemographicSpec.model_validate(
                {
                    "age_ranges": params.get("age_ranges") or [],
                    "genders": params.get("genders") or [],
                    "income_ranges": params.get("income_ranges") or [],
                    "parental_statuses": params.get("parental_statuses") or [],
                    "include_undetermined": params.get("include_undetermined") or {},
                    # Carried through: the panel edits values, not reasoning, so dropping
                    # these would blank the "why" column on every click.
                    "rationales": params.get("rationales")
                    or (dump.get("demographics") or {}).get("rationales")
                    or {},
                }
            )
        except ValidationError as exc:
            return False, f"Invalid demographics: {exc.errors()[0].get('msg', '')}"
        dump["demographics"] = spec.model_dump(mode="json")
        message = (
            "Cleared the demographic narrowing — the campaign reaches every age and gender."
            if spec.is_empty
            else "Updated the demographics."
        )

    elif action == "add":
        ref = str(params.get("ref") or "").strip()
        if not ref:
            return False, "A segment reference is required."
        cand = await _resolve_ref(ref, session_ctx, dump, context)
        if cand is None:
            # The gate that matters: an unknown reference reaches the wrong people, or
            # nobody, and nothing downstream would report it.
            return (
                False,
                f"'{ref}' is not a segment that can serve this campaign — search for one first.",
            )
        if any(s["ref"] == cand["ref"] for s in signals):
            return False, f"'{cand['label']}' is already targeted."
        try:
            signal = AudienceSignal(
                kind=SignalKind(cand["kind"]),
                ref=cand["ref"],
                label=cand["label"],
                source=SignalSource.TAXONOMY,
                rationale=str(params.get("rationale") or "").strip(),
                path=cand["path"],
            )
        except ValidationError:
            return False, f"'{ref}' cannot be added to this audience."
        signals.append(signal.model_dump(mode="json"))
        message = f"Added '{cand['label']}'."

    else:  # delete
        ref = str(params.get("ref") or "").strip()
        # By resource name or bare id — the panel holds names, the agent reads ids off the
        # tree, and catalogue.by_key indexes both.
        target = next(
            (s for s in signals if ref in (s["ref"], s["ref"].rsplit("/", 1)[-1])), None
        )
        if target is None:
            return False, f"'{ref}' is not in this audience."
        remaining = [s for s in signals if s is not target]
        if not _positive_refs(remaining):
            # Grouped mode has no untargeted fallback: an ad group with no positive segment
            # cannot run, so an empty audience is a broken campaign rather than a broad one.
            return (
                False,
                f"'{target['label']}' is the only segment left — a campaign cannot run with no audience. Add a replacement first.",
            )
        signals = remaining
        # Goes with the signal whether or not Google holds the segment - nothing reads a
        # blueprint for a row that is no longer targeted. The resource itself stays.
        blueprints = dict(session_ctx.get(BLUEPRINTS_KEY) or {})
        blueprints.pop(target["ref"], None)
        session_ctx[BLUEPRINTS_KEY] = blueprints
        message = f"Removed '{target['label']}'."

    ok, note = _persist(session_ctx, dump, signals)
    if not ok:
        return False, note
    logger.info(
        "aud_update action=%s ref=%r signals=%d",
        action,
        params.get("ref"),
        len(signals),
    )
    return True, message + note


def _persist(session_ctx: dict, dump: dict, signals: list[dict]) -> tuple[bool, str]:
    """Revalidate, save, and report any guard the result now exceeds.

    The only write path, shared by the panel's actions and by a custom segment we just
    created - so neither can save an audience the other would have refused.
    """
    ok, err = _rebuild(dump, signals)
    if not ok:
        return False, err
    set_audience(session_ctx, dump)
    over = AudienceTargetingResult.model_validate(dump).over_cap()
    if not over:
        return True, ""
    return True, " Now over the guard for " + ", ".join(
        f"{k.value} ({n} of {MAX_SIGNALS_PER_KIND})" for k, n in over.items()
    )


def add_signal(session_ctx: dict, signal: AudienceSignal) -> tuple[bool, str]:
    """Append an already-resolved signal - one whose ref did not come from the catalogue,
    because we just created it. Goes through the same invariants as every other add."""
    dump = audience(session_ctx)
    if not dump:
        return False, "No audience in session - build the campaign first."
    signals: list[dict] = list(dump.get("signals") or [])
    if any(s["ref"] == signal.ref for s in signals):
        return False, f"'{signal.label}' is already targeted."
    signals.append(signal.model_dump(mode="json"))
    ok, note = _persist(session_ctx, dump, signals)
    if not ok:
        return False, note
    return True, f"Added '{signal.label}'.{note}"


async def emit_panel(context: dict, session_ctx: dict) -> None:
    """Re-emit only the audience block (keyed upsert, no panel flash)."""
    dump = audience(session_ctx) or {}
    # Key off the craft_id the panel was ORIGINALLY drawn under — audience_targeting records
    # it in the dump's meta. Reading it from the data (not a session key that only
    # coincidentally holds the same value) guarantees the edit upserts into the same craft.
    craft_id = (
        (dump.get("meta") or {}).get("craft_id")
        or session_ctx.get("campaign_craft_id")
        or f"campaign_{context.get('session_id', '')}"
    )
    await emit_section_update(
        context.get("event_stream"),
        craft_id,
        audience_review_block(dump, session_ctx.get(BLUEPRINTS_KEY)),
    )


async def update_audience(params: dict, context: dict) -> ToolResult:
    """Panel-click entry: one mechanical edit, zero LLM."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    ok, message = await apply_edit(params, context)
    if not ok:
        return ToolResult(success=False, error=message)

    await emit_panel(context, session_ctx)
    return ToolResult(
        success=True,
        summary=message,
        data={"action": str(params.get("action", "")).lower()},
    )


def _member(kind: str, params: dict) -> tuple[object, str]:
    """The member the panel sent, validated, or the reason it cannot be used."""
    value = str(params.get(MEMBERS[kind].param) or "")
    try:
        if kind == "term":
            return CustomSegmentTerm(
                keyword=value,
                volume=int(params.get("volume") or 0),
            ).model_dump(), ""
        if kind == "url":
            return CustomSegmentUrl(url=value).url, ""
        return CustomSegmentApp(app=value).app, ""
    except ValidationError as exc:
        return None, str(exc.errors()[0].get("msg", f"invalid {kind}"))


def _is_same(kind: str, a: object, b: object) -> bool:
    """Two members, both already through _member.

    Case-folded for terms and urls, which are the same signal typed differently, but NOT for
    apps: an Android package name is case-sensitive, so two spellings are two apps.
    """
    if kind == "term":
        if not (isinstance(a, dict) and isinstance(b, dict)):
            return False
        return a["keyword"].casefold() == b["keyword"].casefold()
    if kind == "url":
        return str(a).casefold() == str(b).casefold()
    return a == b


async def apply_member_edit(params: dict, context: dict) -> tuple[bool, str]:
    """Add or remove one member of a PENDING custom segment.

    Google does allow editing a created one; that path is not built - AGENT.md 6.3.
    """
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return False, "No session context available."

    verb, _, kind = str(params.get("action") or "").partition("_")
    if verb not in ("add", "delete", "edit") or kind not in MEMBERS:
        return False, f"Invalid action '{params.get('action')}'."
    if verb == "edit" and kind == "term":
        # A term's volume is looked up from the keyword, so changing the text means a new
        # lookup - delete and add keeps the two in step.
        return (
            False,
            "Search terms cannot be edited in place - remove it and add the new one.",
        )

    ref = str(params.get("ref") or "").strip()
    if not is_pending(ref):
        return False, "This segment is already in the account - its terms are fixed."

    blueprints = dict(session_ctx.get(BLUEPRINTS_KEY) or {})
    plan = blueprints.get(ref)
    if plan is None:
        return False, "That custom segment is no longer in this campaign."

    # Normalised before comparing: raw "home  loan" would land beside a stored "home loan".
    item, error = _member(kind, params)
    if error:
        return False, error

    field = MEMBERS[kind].field
    items = list(plan.get(field) or [])

    if verb == "add":
        if any(_is_same(kind, existing, item) for existing in items):
            return False, "That is already in this segment."
        items.append(item)
        over = kind == "term" and len(items) > CUSTOM_SEGMENT_KEYWORD_TARGET_MAX
        message = f"Added to '{plan['label']}'." + (
            f" That is past Google's guidance of {CUSTOM_SEGMENT_KEYWORD_TARGET_MAX} "
            "search terms."
            if over
            else ""
        )
    elif verb == "edit":
        previous, error = _member(kind, {kind: params.get("old")})
        if error:
            return False, error
        at = next(
            (
                i
                for i, existing in enumerate(items)
                if _is_same(kind, existing, previous)
            ),
            None,
        )
        if at is None:
            return False, "That is not in this segment."
        if any(
            _is_same(kind, existing, item)
            for i, existing in enumerate(items)
            if i != at
        ):
            return False, "That is already in this segment."
        items[at] = item  # in place, so the row does not jump while being edited
        message = f"Updated in '{plan['label']}'."

    else:
        remaining = [i for i in items if not _is_same(kind, i, item)]
        if len(remaining) == len(items):
            return False, "That is not in this segment."
        if field == "terms" and not remaining:
            # Keywords are the one member type a segment cannot be built without.
            return False, "A custom segment needs at least one search term."
        items = remaining
        message = f"Removed from '{plan['label']}'."

    blueprints[ref] = {**plan, field: items}
    session_ctx[BLUEPRINTS_KEY] = blueprints
    logger.info(
        "aud_member %s ref=%s %s=%d", params.get("action"), ref, field, len(items)
    )
    return True, message


async def update_custom_segment(params: dict, context: dict) -> ToolResult:
    """Panel-click entry for a custom segment's members: one edit, zero LLM."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    ok, message = await apply_member_edit(params, context)
    if not ok:
        return ToolResult(success=False, error=message)

    await emit_panel(context, session_ctx)
    return ToolResult(success=True, summary=message)
