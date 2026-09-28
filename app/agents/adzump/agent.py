"""AdzumpAgent: the chat agent that builds an ad campaign through conversation.

    run()                    refresh the product and competitors, then the shared tool loop
    build_turn_reminder()    before every model call: capture the user's answer,
                             snapshot the chat, render ORCHESTRATOR_CONTEXT
    get_pending_suggestions()  the quick-reply chips under the reply
    _on_loop_complete()      after each reply: autosave, map targets, show the map

The fixed system prompt is context.py. The per-turn reminder is rendered by
core/dynamic_context.py from the NEW_CAMPAIGN journey in workflow.py.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.streaming import AgentEventStream
from app.agents.adzump.context import build_adzump_context
from app.agents.adzump.workflow import ORCHESTRATOR_CONTEXT, AdzumpContext
from app.agents.adzump.models import OfferState, offer_state
from app.agents.adzump.observability import log_turn_decision
from app.agents.adzump.platform import is_mapped_for
from app.agents.adzump.tools.campaign_data import (
    _apply_field,
    _current_turn,
    _last_user_text,
    analysis_offer_resolution,
    instagram_offer_resolution,
    is_clear_decline_reply,
    product_changes_for,
    save_product_changes,
)
from app.agents.adzump._shared import primary_screenshot_url, resolve_url
from app.agents.adzump.tools.registry import ALL_TOOLS
from app.agents.adzump.tools.suggestions import infer_suggestions
from app.config import settings

logger = logging.getLogger(__name__)


class AdzumpAgent(BaseAgent):
    """Chat agent that builds ad campaigns through conversation."""

    _instance: "AdzumpAgent | None" = None

    # When the model batches a question widget with other tools, run them one by
    # one and stop after the first question, so two widgets never stack.
    force_serial_on_elicitation = True

    def __init__(self) -> None:
        # The live event stream of the current run, for _on_loop_complete (a
        # coroutine-backed stream can't ride the persisted session.context).
        self._current_stream: AgentEventStream | None = None
        super().__init__(
            name="adzump",
            tools=ALL_TOOLS,
            context_builder=build_adzump_context(),
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER),
        )

    @classmethod
    def get_instance(cls) -> "AdzumpAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("AdzumpAgent created with %d tools", len(ALL_TOOLS))
        return cls._instance

    # ── Entry point: one user message ────────────────────────────────────────

    async def run(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
        image_blocks: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> None:
        self._current_stream = event_stream
        try:
            await self._refresh_from_storage(session, event_stream)
            await super().run(user_message, session, event_stream, image_blocks, model_override)
        finally:
            self._current_stream = None

    # Before the model runs: re-read the product and its competitors from their
    # saved rows - another chat on this product (a second tab, a teammate) may
    # have changed them since the last reply - and repaint the panel only if
    # something changed. On a database error the chat's own copy serves.
    async def _refresh_from_storage(
        self, session: BaseSession, event_stream: AgentEventStream,
    ) -> None:
        from app.agents.adzump.services.product_service import (
            refresh_competitor_list, refresh_product,
        )
        from app.agents.adzump.tools.craft import rerender_craft
        ctx = session.context
        tool_ctx = {**self.build_tool_context(session), "event_stream": event_stream}
        try:
            product_changed = await refresh_product(ctx, tool_ctx)
            list_changed = await refresh_competitor_list(ctx, tool_ctx)
        except Exception as e:
            logger.warning("storage_refresh_skipped: %s: %s",
                           type(e).__name__, str(e)[:200])
            return
        if product_changed or list_changed:
            await rerender_craft(ctx, tool_ctx, ctx.get("product_data") or {},
                                 (ctx.get("campaign_spec") or {}).get("platform") or "")

    # ── BaseAgent hooks, in the order the loop calls them ────────────────────

    async def build_dynamic_context(self, session: BaseSession) -> str:
        return ""  # adzump's context is fully per-turn: see build_turn_reminder

    # Called before every model call. In order:
    #   1. note the open question the user's message arrived into (for the log)
    #   2. capture a chip answer / a typed decline in code, before the snapshot,
    #      so the answered step is already done in this reminder; an answer that
    #      changes the product (an ad account) is saved on it now, and the note
    #      tells the model whether that worked
    #   3. snapshot the chat (AdzumpContext) and render ORCHESTRATOR_CONTEXT,
    #      with this turn's one-off notes (answer ack, uploads, resume) on top
    #   4. write the turn decision record
    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str:
        rail = session.context.get("_pending_elicitation") or {}
        open_rail_field, open_rail_untagged = rail.get("field"), bool(rail) and not rail.get("field")
        ack = self._capture_tagged_answer(session, turn)
        captured = session.context.get("_captured_this_turn") or ""
        if ack and product_changes_for(captured, session.context):
            saved_note = await save_product_changes(
                [captured], session.context, self.build_tool_context(session))
            ack = f"{ack}\n{saved_note}"

        _hydrate_location_from_product_data(session.context)
        actx = AdzumpContext.from_session(session)
        last_user = _last_user_text({"_session": session})
        prose_declined = self._record_prose_decline(session, actx, last_user, turn)
        if prose_declined and not ack:
            ack = ("## You just recorded the user's answer\n"
                   "They declined competitive analysis; it is stored - never offer it "
                   "again. Acknowledge briefly in your own words, then take the next "
                   "action.")
        if prose_declined:
            actx = AdzumpContext.from_session(session)  # the spec just changed
        uploads = self._uploaded_assets_section(session)
        resume = self._resume_elicitation_section(session, turn)
        reminder, progress = ORCHESTRATOR_CONTEXT.render(
            actx, last_user=last_user, agentic_turn=turn, set_at=actx.set_at,
            session_turn=actx.current_turn, steers=(ack, uploads, resume))

        # prior_capture rotates on agentic turn 1 (captures only happen there),
        # so the record pairs a repeat-ask with what landed the turn before.
        captures = session.context.pop("_turn_captures", [])
        prior_capture = session.context.get("_prior_capture")
        if turn == 1:
            session.context["_prior_capture"] = (
                {"field": captures[-1]["field"], "verdict": captures[-1]["verdict"]}
                if captures else None
            )
        log_turn_decision(
            session_id=str(getattr(session, "session_id", "")),
            turn=actx.current_turn,
            agentic_turn=turn,
            missing=list(progress.missing),
            steers=[name for name, fired in (
                ("capture_ack", bool(ack)),
                ("prose_decline", prose_declined),
                ("uploaded_assets", bool(uploads)),
                ("resume_elicitation", bool(resume)),
            ) if fired],
            captures=captures,
            prior_capture=prior_capture,
            open_rail_field=open_rail_field,
            open_rail_untagged=open_rail_untagged,
            offers={
                "competitive_analysis": analysis_offer_resolution(
                    actx.spec, actx.competitor_analysis_attempted).value,
                "competitor_creatives": actx.competitor_creatives_resolution.value,
                "instagram": instagram_offer_resolution(actx.spec).value,
            },
        )
        return reminder

    # Saves the user's answer to a chip question in code, before the model runs,
    # so a forgotten set_campaign_spec can't lose it:
    #   exact chip value     -> saved (same checks as a model write)
    #   clear typed decline  -> saved as declined
    #   anything else        -> left for the model (_resume_elicitation_section)
    # Returns a note telling the model it's saved, or "". Only on the first model
    # call of a reply (agentic turn 1), when the answer just arrived - not the
    # session turn, which a resumed chat restores to its last value.
    def _capture_tagged_answer(self, session: BaseSession, turn: int = 1) -> str:
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
        value = answers.get(last_user)  # exact chip match
        if value is None and is_clear_decline_reply(last_user):
            # A typed clear decline. Legacy field names ride an old rail;
            # _apply_field canonicalizes their "true" to the enum.
            if field in ("competitive_analysis", "competitor_creatives"):
                value = OfferState.DECLINED.value
            elif field in (
                "competitive_analysis_declined", "competitor_creatives_declined"
            ):
                value = "true"
        if value is None:
            # No chip match, no clear decline: the model owns the reply.
            # Logged so no-matches are countable.
            logger.info(
                "tagged_capture: layer2_fallthrough field=%s user_said=%r",
                field, last_user[:80],
            )
            return ""
        stored, info = _apply_field(
            field,
            value,
            last_user,
            session.context,
            _current_turn({"_session": session}),
        )
        session.context.setdefault("_turn_captures", []).append(
            {"layer": 1, "field": field, "value": str(value),
             "verdict": "stored" if stored else "rejected"}
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
        # One-reply marker, popped by get_pending_suggestions: if the model then
        # asks the next question as prose, no untagged fallback chips (a click
        # on them wouldn't be captured, so the question would repeat).
        session.context["_captured_this_turn"] = field
        # What the user clicked, in their words: a chip sends its label as its
        # value, except accounts, whose names the fetch stored.
        label = (session.context.get("account_names") or {}).get(str(value)) or last_user
        logger.info(
            "tagged_capture: stored field=%s value=%r user_said=%r",
            field,
            value,
            last_user[:80],
        )
        # The write's side effects (saved accounts reused, stale ones cleared)
        # must reach the model, or a platform click silently picks the ad
        # account the money goes through.
        side_effects = (
            f"The write also did: {info}. Name any reused accounts in your "
            "acknowledgement so the user knows where the campaign will run. "
            if info != field else ""
        )
        return (
            "## You just captured the user's answer\n"
            f"Their last message picked **{label}** ({field} = {value}). It is already "
            f"stored - do NOT call set_campaign_spec for it. {side_effects}Acknowledge "
            "their choice briefly, in your own words and only once in this reply, "
            "then CALL the next tool from the missing-list (a "
            "fetch tool or present_options) - do NOT write the next question as "
            "plain text, and NEVER end your turn without making that tool call (a "
            "live run stalled on a dead-end turn that acknowledged and stopped)."
        )

    # Saves "no" to the competitor-analysis offer when the model asked it as
    # plain text (no chip row, so _capture_tagged_answer can't see it) -
    # otherwise the offer is asked again every turn. Only when all hold:
    #   - a Google campaign, and the offer is still unanswered
    #   - no chip question for it is open
    #   - a clear decline ("no thanks", not "no competitors named yet")
    #   - the first model call of the reply
    # Returns True if it saved.
    def _record_prose_decline(
        self, session: BaseSession, actx: AdzumpContext, last_user: str, turn: int,
    ) -> bool:
        if turn != 1 or not last_user:
            return False
        pe = session.context.get("_pending_elicitation")
        if pe and pe.get("field") in (
            "competitive_analysis", "competitive_analysis_declined"
        ):
            return False                                     # tagged-capture owns it
        if not (actx.is_google
                and not actx.competitor_analysis_attempted
                and offer_state(actx.spec, "competitive_analysis")
                is OfferState.UNSET):
            return False
        if not is_clear_decline_reply(last_user):
            return False                                     # ambiguous → let the LLM judge
        stored, _ = _apply_field(
            "competitive_analysis", OfferState.DECLINED.value, last_user,
            session.context, _current_turn({"_session": session}),
        )
        if stored:
            session.context.setdefault("_turn_captures", []).append(
                {"layer": 1, "field": "competitive_analysis",
                 "value": OfferState.DECLINED.value, "verdict": "stored"}
            )
            logger.info("prose_decline_recorded: competitive_analysis=declined user_said=%r",
                        last_user[:80])
        return bool(stored)

    # When the user attached images: a note making manage_assets the first
    # action. The images exist only in the stash the /chat route wrote, so
    # skipping it loses them.
    def _uploaded_assets_section(self, session: BaseSession) -> str:
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

    # When the last reply ended on a question: a note telling the model this
    # message IS the answer, so it doesn't ask again.
    #   question already answered -> dropped, no note
    #   upload request            -> note; stays open until the model moves on
    #   chip question             -> note with the chip values; cleared
    #   any other question        -> short note; cleared
    # Only on the first model call of a reply; later calls return "" and keep
    # the question, since this runs before every model call.
    def _resume_elicitation_section(self, session: BaseSession, turn: int = 1) -> str:
        if turn != 1:
            return ""
        pe = session.context.get("_pending_elicitation")
        if not pe:
            return ""
        # Already answered: drop it silently.
        pe_field = pe.get("field")
        if pe_field and (session.context.get("campaign_spec") or {}).get(pe_field):
            session.context.pop("_pending_elicitation", None)
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
        session.context.pop("_pending_elicitation", None)  # single: one-shot
        tool = pe.get("tool", "the previous step")
        if pe_field and pe.get("answers"):
            # A typed reply to a chip question is the model's to land: pick the
            # canonical value and write it.
            canonical = ", ".join(f'"{v}"' for v in dict(pe["answers"]).values())
            return (
                "## Resuming after a question\n"
                f"Last turn you asked the user to pick **{pe_field}** (chips are "
                f"already on screen; canonical values: {canonical}). Their current "
                "message IS the reply - do NOT restate or paraphrase the question.\n"
                f"- It clearly selects an option (typed variant, \"60 days please\") "
                f"→ call `set_campaign_spec({pe_field}=<canonical value>)` NOW; for "
                "duration/budget a clearly stated non-preset value counts too "
                '(normalize it, e.g. "45 days" / "₹7,500/day").\n'
                "- It is about a DIFFERENT field or a question → it selects NOTHING "
                "here; handle it, then re-render the SAME chips via present_options "
                'with a short "pick one below" - do not guess.\n'
                "- Never store a value the user didn't state."
            )
        return (
            "## Resuming after a question\n"
            f"Last turn you asked the user a question (via {tool}); the widget is "
            "already on screen. Their current message IS the reply. Do NOT restate "
            "or paraphrase the question, and do NOT call another tool with the "
            "previous tool's result as input - read their answer and pick the next action."
        )

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        # The full session_id: a truncated one let distinct sessions share a
        # craft_id and overwrite each other's panel.
        session.context.setdefault("craft_id", f"adzump_{session.session_id}")
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    # The quick-reply chips under the reply. First match wins:
    #   1. chips a tool queued (_pending_suggestions)
    #   2. none while a map or any question widget is on screen - it owns the ask
    #   3. one value-only "Go ahead" / "Confirm location" / "Yes, launch" chip for
    #      a prose advance ask (_advance_chip)
    #   4. none right after a captured chip answer - untagged chips on a prose
    #      question can't be captured, so the question would repeat
    #   5. otherwise inferred from the reply text (infer_suggestions)
    async def get_pending_suggestions(
        self,
        session: BaseSession,
        assistant_text: str = "",
    ) -> dict[str, Any] | None:
        # Pop the one-reply capture marker before any return, so it never leaks
        # into the next reply (the loop doesn't clear the persisted context).
        captured = session.context.pop("_captured_this_turn", None)
        pending = session.context.pop("_pending_suggestions", None)
        if pending:
            return pending
        if session.context.get("_pending_location_confirm"):
            return None
        if session.context.get("_pending_elicitation"):
            return None
        adv = AdzumpAgent._advance_chip(assistant_text)
        if adv:
            return adv
        if captured:
            return None
        return await infer_suggestions(assistant_text, session.context)

    # A one-click reply when the model asks to move on in plain text ("Shall we
    # proceed?") and no widget is on screen:
    #   last line mentions "launch"    -> [Yes, launch]
    #   last line mentions "location"  -> [Confirm location]
    #   any other move-on ask          -> [Go ahead]
    #   not a move-on ask              -> no chip
    # A click just sends the chip's text as a normal message; nothing is saved.
    @staticmethod
    def _advance_chip(text: str) -> dict[str, Any] | None:
        lt = (text or "").lower()
        if not lt:
            return None
        # Only the trailing line is the ask: a summary card above it lists a
        # "Location:" bullet and "Ready to launch", which must not fire a
        # "Confirm location" chip at the launch step.
        tail = next((ln for ln in reversed(lt.splitlines()) if ln.strip()), "")
        markers = (
            "let's confirm", "lets confirm", "confirm the location", "shall i",
            "shall we", "ready to", "ready when you", "go ahead", "look good",
            "looks good", "proceed", "all set",
        )
        if not any(m in tail for m in markers):
            return None
        # Launch wins over location: the launch ask's line is "...Ready to launch the campaign?".
        if "launch" in tail:
            return {"options": [{"label": "Yes, launch",
                                 "value": "yes, launch"}], "mode": "single"}
        if "location" in tail:
            return {"options": [{"label": "Confirm location",
                                 "value": "yes, confirm the location"}], "mode": "single"}
        return {"options": [{"label": "Go ahead", "value": "yes, go ahead"}], "mode": "single"}

    # After each reply: catch up the map for a newly picked platform, save this
    # chat's draft, show the map.
    async def _on_loop_complete(
        self, session: BaseSession, tool_call_log: list[dict[str, Any]],
    ) -> None:
        await super()._on_loop_complete(session, tool_call_log)
        await self._map_targets_for_new_platform(session)
        await self._autosave_campaign(session)
        await self._emit_stored_targeting_panel(session)
        # The loop saved the context before these hooks ran; the mapping and the
        # panel marker changed it since, and the next request reloads it.
        await session.save_context()

    # Saves this chat's campaign draft after every reply (skipped until a
    # product URL exists). A failed save is logged, never raised.
    async def _autosave_campaign(self, session: BaseSession) -> None:
        ctx = session.context
        from app.agents.adzump.services.product_service import save_campaign
        if not resolve_url(ctx):
            return
        try:
            await save_campaign(ctx, self.build_tool_context(session))
        except Exception as e:
            logger.warning("End-of-turn campaign save failed (non-fatal): %s", e)

    # Target areas saved before a platform was picked aren't mapped for it yet:
    # map them now, so the next reminder counts them as mapped. Network calls
    # belong here, after the reply, never in build_turn_reminder.
    async def _map_targets_for_new_platform(self, session: BaseSession) -> None:
        ctx = session.context
        platform = (ctx.get("campaign_spec") or {}).get("platform") or ""
        product = ctx.get("product_data") or {}
        target_areas = product.get("target_areas") or []
        if not (platform and target_areas) or is_mapped_for(target_areas, platform):
            return
        from app.agents.adzump.agents.location.platform_mapping import PlatformGeoMapper
        from app.agents.adzump.services.product_service import save_product_fields
        country_code = (product.get("place") or {}).get("country_code") or "IN"
        try:
            tool_ctx = self.build_tool_context(session)
            mapped = await PlatformGeoMapper(tool_ctx).map_target_areas(
                target_areas, platform, country_code
            )
            if mapped:
                await save_product_fields(ctx, tool_ctx, {"target_areas": mapped})
        except Exception as e:
            logger.warning("End-of-turn geo auto-mapping failed (non-fatal): %s", e)

    # A platform was just picked and the saved areas are already mapped for it:
    # show the map now, because manage_targeting_locations won't run (nothing is
    # owed). Once per platform.
    async def _emit_stored_targeting_panel(self, session: BaseSession) -> None:
        ctx = session.context
        actx = AdzumpContext.from_session(session)
        platform = actx.spec.get("platform") or ""
        if not (platform and actx.has_mapped_geo_targets) \
                or ctx.get("_last_craft_platform") == platform:
            return
        ctx["_last_craft_platform"] = platform
        url = resolve_url(ctx)
        product = ctx.get("product_data") or {}
        stream = self._current_stream
        craft_id = ctx.get("craft_id")
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

        # A visible locations row, so the user sees the saved areas applied.
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


# A returning product already has a confirmed place: copy it into
# campaign_spec.location so the location isn't asked again. A local business
# also needs mapped target areas first; otherwise it confirms again.
def _hydrate_location_from_product_data(ctx: dict) -> None:
    from app.agents.adzump.agents.location.models import is_local_business

    spec = ctx.setdefault("campaign_spec", {})
    if spec.get("location"):
        return
    product = ctx.get("product_data") or {}
    place = product.get("place") or {}
    if not place.get("address"):
        return

    scale = (product.get("business_scale") or "national").lower().strip()
    # Mapped for ANY platform counts - the handle rides nested on each area.
    has_resolved_targets = any(
        a.get("google") or a.get("meta") for a in product.get("target_areas") or []
    )
    if is_local_business(scale) and not has_resolved_targets:
        return

    spec["location"] = place["address"]
    logger.info(
        "hydrated_location_from_product_data: location=%s", place["address"]
    )
