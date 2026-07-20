"""AdzumpAgent - conversational agent for ad-campaign construction.

Core design: keep the BaseAgent + tool loop, put every ounce of steering
into the **dynamic context**. Each turn renders:

1. ``## State`` - what's collected, with provenance ("just set" / "set N turns ago").
2. ``## User just said`` - last user message verbatim.
3. ``## What's still missing`` - ordered list from ``_next_action``.
4. ``## How to respond`` - 6-case priority rule for the LLM.

The static system prompt carries persona + non-negotiable rules only. The
workflow tree (``_next_action`` over a typed ``CampaignContext``) lives in
``next_action.py``; the section renderers live in ``prompt_sections.py``.
This module keeps the BaseAgent overrides and the turn-start capture rails
(tagged answers, prose declines, elicitation resume).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.agents.adzump.context import build_adzump_context
from app.agents.adzump.next_action import (
    CampaignContext,
    _is_custom_reply,
    _next_action,
)
from app.agents.adzump.platform import is_mapped_for
from app.agents.adzump.prompt_sections import (
    _how_to_respond_section,
    _missing_section,
    _state_section,
    _user_said_section,
)
from app.agents.adzump.tools.campaign_data import (
    _ACCOUNT_LIKE_FIELDS,
    _apply_field,
    _current_turn,
    _last_user_text,
    _normalize_id,
    is_clear_decline_reply,
)
from app.agents.adzump._shared import primary_screenshot_url
from app.agents.adzump.answer_parse import parse_typed_answer, currency_for
from app.agents.adzump.tools.registry import ALL_TOOLS
from app.agents.adzump.tools.suggestions import infer_suggestions
from app.config import settings

logger = logging.getLogger(__name__)



def _hydrate_location_from_product_data(ctx: dict) -> None:
    """Restore campaign_spec.location from product_data.place on returning
    sessions, so the agent doesn't re-ask for a location already confirmed.

    Only runs when spec.location is unset but product_data.location exists. For
    local businesses, requires that geo-targets were already resolved (otherwise
    we must go through confirm_location again).
    """
    from app.agents.adzump.agents.location.models import is_local_business

    spec = ctx.setdefault("campaign_spec", {})
    if spec.get("location"):
        return
    product = ctx.get("product_data") or {}
    place = product.get("place") or {}
    if not place.get("address"):
        return

    scale = (product.get("business_scale") or "national").lower().strip()
    # Resolved for ANY platform counts - the handle rides nested on each area.
    has_resolved_targets = any(
        a.get("google") or a.get("meta") for a in product.get("target_areas") or []
    )
    if is_local_business(scale) and not has_resolved_targets:
        return

    spec["location"] = place["address"]
    logger.info(
        "hydrated_location_from_product_data: location=%s", place["address"]
    )


class AdzumpAgent(BaseAgent):
    """Chat agent that manages ad campaigns through conversation."""

    _instance: "AdzumpAgent | None" = None

    # v9 I-1 · data-backed (two present_options stacked live 2026-05-30). When
    # the LLM batches a deferred elicitation with other tools, run them serially
    # and early-exit after the first elicitation so a second widget can't stack.
    force_serial_on_elicitation = True

    # ── construction ──

    def __init__(self) -> None:
        # _current_stream is stashed per-run so _on_loop_complete can emit
        # without needing event_stream in session.context (which is persisted
        # and can't hold live coroutine objects).
        self._current_stream = None
        context = build_adzump_context()
        provider = getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER)
        super().__init__(
            name="adzump",
            tools=ALL_TOOLS,
            context_builder=context,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=provider,
        )

    @classmethod
    def get_instance(cls) -> "AdzumpAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("AdzumpAgent created with %d tools", len(ALL_TOOLS))
        return cls._instance

    # ── prompt-section builders (private) ──

    def _capture_tagged_answer(self, session: BaseSession, turn: int = 1) -> str:
        """PR2 · store the user's reply to a tagged ``present_options`` directly,
        before the LLM runs - so a forgotten ``set_campaign_spec`` follow-up
        can't drop the answer (the Bug-B family). Returns a one-line
        acknowledgement steer on a successful capture, else "".

        Gated to agentic ``turn == 1`` (the reply only just arrived) - NOT
        ``session._turn_count`` (restored to the max turn on resume). Match by
        exact option value (chips), else a conservative typed parser
        (duration/budget/platform). Ambiguous answers (Custom / "Yes" /
        off-topic / unparseable) leave ``_pending_elicitation`` intact and fall
        through to the LLM (then ``_resume_elicitation_section`` steers it)."""
        if turn != 1:
            return ""
        pe = session.context.get("_pending_elicitation")
        if not pe or not pe.get("field") or pe.get("expects") != "single":
            return ""
        field = pe["field"]
        answers = pe.get("answers") or {}
        last_user = _last_user_text({"_session": session})
        if not last_user:
            return ""
        value = answers.get(last_user)  # (a) exact chip match
        if value is None and field in ("duration", "budget", "platform"):
            value = parse_typed_answer(
                field, last_user, currency_for(session.context)
            )  # (b) typed
        if value is None and field == "competitive_analysis_declined" \
                and is_clear_decline_reply(last_user):
            value = "true"  # (c) F17 · typed clear decline
        if value is None:
            # v4 · F10 - the user picked the "Custom" escape on a duration/budget
            # chip ask. Don't pop the elicitation: keep it OPEN (mark it) so their
            # NEXT typed reply is captured by the typed-parser above, and steer a
            # free-text ask instead of re-rendering the same chips (the live loop,
            # bug #11). The mark also drives _next_action (awaiting_custom_field)
            # and tells _resume_elicitation_section not to pop.
            if (
                field in ("duration", "budget")
                and _is_custom_reply(last_user)
                and not pe.get("awaiting_custom")
            ):
                pe["awaiting_custom"] = True
                logger.info(
                    "tagged_capture: custom escape for field=%s - awaiting typed value",
                    field,
                )
                return (
                    "## The user chose a custom value\n"
                    f"They want to enter their own {field}. Ask them in ONE short line "
                    f'to TYPE it (e.g. "45 days" / "₹7,500/day") - do NOT call '
                    f"present_options or show chips. Their typed reply is captured automatically."
                )
            return ""  # Yes / off-topic / unparseable → LLM
        stored, info = _apply_field(
            field,
            value,
            last_user,
            session.context,
            _current_turn({"_session": session}),
        )
        if not stored:
            logger.info(
                "tagged_capture: rejected field=%s value=%r reason=%s user_said=%r",
                field,
                value,
                info,
                last_user[:80],
            )
            return ""
        session.context.pop("_pending_elicitation", None)
        # v3 · F4 - one-run marker: a tagged answer was captured this turn. Read
        # (and cleared) by get_pending_suggestions to suppress the untagged
        # infer_suggestions fallback when the LLM asks the next question as prose
        # (its chips would carry no field tag → silent drop → re-ask). Lives in
        # session.context but is popped on read, so it never leaks to next turn.
        session.context["_captured_this_turn"] = field
        logger.info(
            "tagged_capture: stored field=%s value=%r user_said=%r",
            field,
            value,
            last_user[:80],
        )
        return (
            "## You just captured the user's answer\n"
            f"Their last message set **{field} = {value}**. It is already stored - "
            "do NOT call set_campaign_spec for it. Acknowledge it in one short "
            "phrase, then CALL the next tool from the missing-list (a fetch tool or "
            "present_options) - do NOT write the next question as plain text, and "
            "NEVER end your turn without making that tool call (a live run stalled "
            "on a dead-end turn that acknowledged and stopped)."
        )

    def _record_prose_decline(
        self, session: BaseSession, cctx: "CampaignContext", last_user: str, turn: int,
    ) -> bool:
        """F18 · the competitor offer is non-deterministically asked as PROSE (no
        tagged ``present_options``), so a typed decline has no elicitation for
        ``_capture_tagged_answer`` to match - and the model often just advances
        without recording it, leaving ``competitive_analysis_declined`` unset and
        the prescription re-firing every turn. Record it in code at turn-start,
        with a NARROW guard (Kiran): only the competitor-offer state, only a
        clear-decline reply (``is_clear_decline_reply`` excludes ambiguous "no…"
        like "no competitors named yet" / "not now, first tell me about X"), and
        never when a competitor elicitation is already pending (tagged-capture
        owns that). Gated agentic ``turn == 1``. Returns True iff it stored."""
        if turn != 1 or not last_user:
            return False
        pe = session.context.get("_pending_elicitation")
        if pe and pe.get("field") == "competitive_analysis_declined":
            return False                                     # tagged-capture owns it
        if not (cctx.is_google
                and not cctx.competitor_analysis_attempted
                and "competitive_analysis_declined" not in cctx.spec):
            return False
        if not is_clear_decline_reply(last_user):
            return False                                     # ambiguous → let the LLM judge
        stored, _ = _apply_field(
            "competitive_analysis_declined", "true", last_user,
            session.context, _current_turn({"_session": session}),
        )
        if stored:
            logger.info("prose_decline_recorded: competitive_analysis_declined=true user_said=%r",
                        last_user[:80])
        return bool(stored)

    def _resume_elicitation_section(self, session: BaseSession, turn: int = 1) -> str:
        """v8 Plan B WS2 · when the previous turn ended on a deferred
        elicitation, tell the LLM it is resuming so it does NOT re-ask or
        paraphrase the question (Bug B). Read from session.context, which
        survives restore - message history drops tool blocks (session.py).

        Single-reply elicitations (location, options) are one-shot: emit the
        reminder, then clear the flag. Multi-reply elicitations (asset uploads)
        persist - the run loop closes them when the LLM moves on.

        Gated to agentic ``turn == 1``: the resume hint steers how the model
        reads the just-arrived reply, which only applies on the first turn of
        the run. On later turns return "" WITHOUT popping, so the one-shot flag
        survives (Approach B re-runs this builder every agentic turn - without
        the gate the pop would fire on turn 1 and the hint would vanish on
        turn 2+ of the SAME run, and the flag would be lost)."""
        if turn != 1:
            return ""
        pe = session.context.get("_pending_elicitation")
        if not pe:
            return ""
        if pe.get("expects") == "multi":
            return (
                "## Resuming - upload request is still open\n"
                "Last turn you asked the user to upload assets. They may send "
                "several messages (one per file) or say they're done. Do NOT "
                "restate the upload request unless they ask what's still needed, "
                "and do NOT assume it's closed until they signal completion or "
                "you judge the captured assets sufficient."
            )
        # v4 · F10 - awaiting a typed custom value: keep the elicitation OPEN
        # (do NOT pop) so the next typed reply is captured by the typed-parser.
        # _capture_tagged_answer already emitted the free-text steer this turn.
        if pe.get("awaiting_custom"):
            return ""
        # single: one-shot - clear after emitting so it fires for exactly this turn
        session.context.pop("_pending_elicitation", None)
        tool = pe.get("tool", "the previous step")
        return (
            "## Resuming after a question\n"
            f"Last turn you asked the user a question (via {tool}); the widget is "
            "already on screen. Their current message IS the reply. Do NOT restate "
            "or paraphrase the question, and do NOT call another tool with the "
            "previous tool's result as input - read their answer and pick the next action."
        )

    def _uploaded_assets_section(self, session: BaseSession) -> str:
        """v9 I-0 · when the user attached image(s) this turn, hand them to the
        Asset Manager via manage_assets (bytes are stashed on the session by the
        /chat handler). First action of the turn - otherwise the upload is lost
        (it only lives in the pending stash)."""
        pending = session.context.get("_pending_uploads")
        if not pending:
            return ""
        n = len(pending)
        return (
            f"## The user just uploaded {n} image{'s' if n != 1 else ''}\n"
            "FIRST, call `manage_assets` to hand the upload(s) to the Asset "
            "Manager - it looks at each image, decides what it is, and saves or "
            "skips it. You do NOT classify the image yourself; if the user said "
            "what it is, pass that as `note`. Do this before anything else, then "
            "continue."
        )

    # ── static leaf helpers - migrations ──

    @staticmethod
    def _migrate_legacy_keys(ctx: dict) -> None:
        """Rename ``campaign_data`` → ``campaign_spec`` for pre-rename sessions.

        Lazy migration. O(1). Existing sessions survive the rename transparently.
        """
        if "campaign_data" in ctx and "campaign_spec" not in ctx:
            ctx["campaign_spec"] = ctx.pop("campaign_data")

    @staticmethod
    def _migrate_campaign_ids(session_ctx: dict) -> None:
        """Canonicalize account/page ids (strip dashes/whitespace) on read.

        Lazy migration for sessions that stored dashed or fullwidth-digit IDs
        before the write-side normalizer shipped. Idempotent.
        """
        spec = session_ctx.get("campaign_spec") or {}
        for field_name in _ACCOUNT_LIKE_FIELDS:
            v = spec.get(field_name)
            if isinstance(v, str):
                canonical = _normalize_id(v)
                if canonical != v:
                    spec[field_name] = canonical

    # ── public surface - BaseAgent override hooks (last, per Kiran's BaseAgent) ──

    async def run(self, user_message, session, event_stream, image_blocks=None, model_override=None):
        """Stash event_stream so _on_loop_complete can emit without session.context."""
        self._current_stream = event_stream
        try:
            await super().run(user_message, session, event_stream, image_blocks, model_override)
        finally:
            self._current_stream = None

    async def build_dynamic_context(self, session: BaseSession) -> str:
        return (
            ""  # adzump context is fully per-turn - see build_turn_reminder (Layer 2)
        )

    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str:
        self._migrate_legacy_keys(session.context)
        self._migrate_campaign_ids(session.context)
        # PR2 · capture the user's tagged answer into campaign_spec BEFORE the
        # snapshot, so the just-answered field drops out of the missing-list
        # this turn. AFTER the migrations (its setdefault would otherwise strand
        # a legacy rename); gated to agentic turn==1 internally.
        ack = self._capture_tagged_answer(session, turn)

        _hydrate_location_from_product_data(session.context)
        cctx = CampaignContext.from_session(session)
        last_user = _last_user_text({"_session": session})
        # F18 · when the competitor offer was asked as PROSE (not a tagged
        # present_options), a clear typed decline has no capture rail - record it
        # in code at turn-start so competitive_analysis_declined doesn't persist
        # in `missing` forever. Re-derive cctx since the spec changed.
        if self._record_prose_decline(session, cctx, last_user, turn):
            cctx = CampaignContext.from_session(session)
        missing = _next_action(cctx)
        logger.info(
            "next_action: turn=%d agentic=%d missing=%s user_said=%r",
            cctx.current_turn,
            turn,
            missing,
            last_user[:80],
        )
        reminder = "\n".join(
            filter(
                None,
                [
                    ack,
                    self._uploaded_assets_section(session),
                    self._resume_elicitation_section(session, turn),
                    _state_section(cctx),
                    _user_said_section(last_user),
                    _how_to_respond_section(),
                    _missing_section(missing),
                ],
            )
        )
        return reminder

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        # Use the full session_id - the previous `[:8]` truncation left only
        # one hex char of entropy after the `SYSTEM_` prefix, so distinct
        # sessions routinely collided onto the same craft_id and stomped
        # each other's UI panel state.
        session.context.setdefault("craft_id", f"adzump_{session.session_id}")
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    async def _on_loop_complete(
        self, session: BaseSession, tool_call_log: list[dict[str, Any]],
    ) -> None:
        await super()._on_loop_complete(session, tool_call_log)
        await self._autosave_campaign(session)
        await self._map_targets_for_new_platform(session)
        await self._emit_stored_targeting_panel(session)

    async def _autosave_campaign(self, session: BaseSession) -> None:
        """Every-turn durable save of whatever the spec holds (draft status)."""
        ctx = session.context
        from app.agents.adzump.services.business_storage import save_campaign, resolve_url
        if not resolve_url(ctx):
            return
        try:
            await save_campaign(ctx, self.build_tool_context(session))
        except Exception as e:
            logger.warning("End-of-turn campaign save failed (non-fatal): %s", e)

    async def _map_targets_for_new_platform(self, session: BaseSession) -> None:
        """If platform was set this turn but existing locations aren't yet mapped
        for it (storage-reuse path: locations existed before platform was chosen),
        map them now so has_mapped_geo_targets is true for the next turn's prompt.
        Runs at end-of-turn where external I/O belongs - NOT in build_turn_reminder."""
        ctx = session.context
        platform = (ctx.get("campaign_spec") or {}).get("platform") or ""
        product = ctx.get("product_data") or {}
        target_areas = product.get("target_areas") or []
        if not (platform and target_areas) or is_mapped_for(target_areas, platform):
            return
        from app.agents.adzump.agents.location.platform_mapping import PlatformGeoMapper
        country_code = (product.get("place") or {}).get("country_code") or "IN"
        try:
            mapped = await PlatformGeoMapper(self.build_tool_context(session)).map_target_areas(
                target_areas, platform, country_code
            )
            if mapped:
                product["target_areas"] = mapped
        except Exception as e:
            logger.warning("End-of-turn geo auto-mapping failed (non-fatal): %s", e)

    async def _emit_stored_targeting_panel(self, session: BaseSession) -> None:
        """If platform was just set this turn and mapped locations already exist
        (storage reuse path), emit the craft panel with platform so the map
        appears - manage_targeting_locations won't fire because
        has_mapped_geo_targets is already True."""
        ctx = session.context
        from app.agents.adzump.services.business_storage import resolve_url
        cctx = CampaignContext.from_session(session)
        platform = cctx.spec.get("platform") or ""
        if not (platform and cctx.has_mapped_geo_targets) \
                or ctx.get("_last_craft_platform") == platform:
            return
        ctx["_last_craft_platform"] = platform
        url = resolve_url(ctx)
        product = ctx.get("product_data") or {}
        stream = self._current_stream
        craft_id = ctx.get("craft_id") or ctx.get("_craft_id")
        if not (stream and craft_id and url):
            return
        from app.agents.adzump.tools.craft import emit_craft_panel
        try:
            await emit_craft_panel(
                stream, craft_id, url, product,
                ctx.get("competitor_analysis") or {},
                screenshot_url=primary_screenshot_url(product),
                baked_summary=(
                    (ctx.get("product_profile") or {}).get("summary")
                    or product.get("summary", "")
                ),
                platform=platform,
            )
        except Exception as e:
            logger.debug("Post-platform craft emit failed (non-fatal): %s", e)

        # Emit a visible locations row so the user sees the stored
        # targeting areas being applied (mirrors the discover UX).
        try:
            mapped = product.get("target_areas") or []
            if mapped:
                place = product.get("place") or {}
                await stream.emit_data(
                    "suggested_locations",
                    {
                        "locations": [loc["name"] for loc in mapped if loc.get("name")],
                        "targeting_type": product.get("business_scale", "local"),
                        "location": place.get("address") or "",
                        "from_storage": True,
                    },
                )
        except Exception as e:
            logger.debug("Stored-locations row emit failed (non-fatal): %s", e)

    @staticmethod
    def _advance_chip(text: str) -> dict[str, Any] | None:
        """F23 · a prose advance/confirm ask (e.g. "Let's confirm the location for
        the campaign") with no live widget gets ONE value-only quick-reply chip so
        the user can click instead of having to type to proceed. Value-only (no
        ``field``/``answer``) → the click just sends "yes …" text that routes to the
        model, so it CANNOT reintroduce the F4 untagged-capture loop. Keyword-gated
        + deterministic (no extra LLM call), per the panel. Returns the chip dict or
        None."""
        lt = (text or "").lower()
        if not lt:
            return None
        # F27 · evaluate the TRAILING line (the actual ask), NOT the whole blob -
        # a launch/review summary lists a "Location:" bullet and contains "ready
        # to" (in "Ready to launch"), which made this fire a "Confirm location"
        # chip at the launch step. The genuine advance ask is always its own short
        # trailing line, so anchoring there fixes the over-match.
        tail = next((ln for ln in reversed(lt.splitlines()) if ln.strip()), "")
        markers = (
            "let's confirm", "lets confirm", "confirm the location", "shall i",
            "shall we", "ready to", "ready when you", "go ahead", "look good",
            "looks good", "proceed", "all set",
        )
        if not any(m in tail for m in markers):
            return None
        # Label precedence: launch → location → generic (launch must win - the
        # launch ask's trailing line is "…Ready to launch the campaign?").
        if "launch" in tail:                                   # F27 · launch step
            return {"options": [{"label": "Yes, launch",
                                 "value": "yes, launch"}], "mode": "single"}
        if "location" in tail:
            return {"options": [{"label": "Confirm location",
                                 "value": "yes, confirm the location"}], "mode": "single"}
        return {"options": [{"label": "Go ahead", "value": "yes, go ahead"}], "mode": "single"}

    async def get_pending_suggestions(
        self,
        session: BaseSession,
        assistant_text: str = "",
    ) -> dict[str, Any] | None:
        # v3 · F4 - pop the one-run capture marker FIRST, before any early
        # return, so it can never leak into the next turn (it rides the
        # persisted context dict, which the generic run-loop doesn't clear).
        # Kept here (not in core/agent.py) so the Adzump-specific marker stays
        # out of the generic runtime.
        captured = session.context.pop("_captured_this_turn", None)
        pending = session.context.pop("_pending_suggestions", None)
        if pending:
            return pending
        # When a map widget is in flight, the widget IS the answer mechanism -
        # don't let the inferrer auto-inject competing Yes/No chips.
        if session.context.get("_pending_location_confirm"):
            return None
        # v8 Plan B WS6 · any deferred elicitation already owns the ask (map,
        # upload prompt, explicit chips). Suppress the untraced infer_suggestions
        # fallback so it can't overlay competing chips on the elicitation bubble
        # (and to skip its extra per-turn gpt-4o-mini call). Full tracing-wrap
        # of infer_suggestions remains a fast-follow.
        if session.context.get("_pending_elicitation"):
            return None
        # v3 · F4 - a tagged answer was captured this turn, but no tagged
        # present_options was emitted (we'd have returned above). The LLM asked
        # the next question as prose → suppress the untagged infer fallback whose
        # chips would carry no field tag (a click wouldn't be captured → re-ask
        # loop). The user can type the answer (typed-capture handles
        # duration/budget/platform); next turn the LLM re-asks via a tagged tool.
        # F23 · a prose advance/confirm ask (no live widget) gets one value-only
        # "Go ahead"/"Confirm location" chip so the user needn't type to proceed.
        # BEFORE the captured-guard so it fires even right after a chip answer (the
        # dead-end case). Value-only → can't reintroduce the F4 untagged-capture loop.
        adv = AdzumpAgent._advance_chip(assistant_text)
        if adv:
            return adv
        if captured:
            return None
        return await infer_suggestions(assistant_text, session.context)
