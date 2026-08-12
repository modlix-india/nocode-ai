"""Tools the KeywordResearchAgent uses AFTER generation — to answer and to edit.

  lookup_keyword   what we recorded about one keyword (in the set / passed over / unseen)
  edit_keywords    add, remove or change keywords — batched

Both are general capabilities, not one handler per question: "why this?", "why not that?",
"add location keywords", "drop the low-volume ones" are all composed from these plus the
generation tools (expand_keywords, keyword_metrics) and the theme's own guidance.

edit_keywords goes through the SAME ``_apply_edit`` the panel's click path uses, so an edit
made in words cannot break an invariant a click couldn't — and it mutates the saved set in
place rather than re-submitting it, which would fabricate provenance and clobber a
concurrent panel click.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.campaign.google.keyword.models import normalize
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_MAX_EDITS = 25  # one conversational turn's worth


def _state(context: dict) -> dict:
    return context.get("session_context") or {}


def _themes(state: dict) -> dict[str, dict]:
    return (state.get("keyword_research") or {}).get("themes") or {}


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
    # Re-emit the keyword block so the panel matches the mutated set — the SAME keyed
    # upsert (append=True, stable block id → replace-in-place, no flash) the panel's own
    # click path does. Without this the panel silently shows the old set after a spoken edit.
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
                    "volume": {"type": "integer"},
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

MANAGE_TOOLS = [LOOKUP_KEYWORD, EDIT_KEYWORDS]
