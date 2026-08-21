"""AudienceAgent — chooses who a campaign reaches.

A worker sub-agent the Campaign Agent spawns once per campaign. It reasons over Google's real
audience catalogue through its tools rather than a fixed pipeline: load the catalogue, pick
the segments that fit the business, decide any demographic narrowing.

Channel-neutral. One ``Audience`` resource serves Demand Gen, Performance Max and App
campaigns, so the channel arrives as an argument and only decides which segments can serve.

The base prompt stays small; ``build_turn_reminder`` injects only the current phase's
guidance, and the phase comes from what the run has produced rather than a counter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from string import Template
from typing import Any

from app.agents.adzump.agents._child_stream import ChildAgentStream
from app.agents.adzump.agents.campaign.brief import wider_brief
from app.agents.adzump.agents.campaign.google.audience import catalogue
from app.agents.adzump.agents.campaign.google.audience.constants import BLUEPRINTS_KEY
from app.agents.adzump.agents.campaign.google.audience.context import (
    BASE,
    BASE_MANAGE,
    Phase,
    current_phase,
    phase_prompt,
)
from app.agents.adzump.agents.campaign.google.audience.custom_segment import (
    CUSTOM_SEGMENT_TOOLS,
    DRAFT_ID_KEY,
)
from app.agents.adzump.agents.campaign.google.audience.manage_tools import MANAGE_TOOLS
from app.agents.adzump.agents.campaign.google.audience.models import (
    AudienceSignal,
    AudienceTargetingResult,
    DemographicSpec,
    span_label,
)
from app.agents.adzump.agents.campaign.google.audience.tools import (
    ALL_TOOLS,
    SEARCH_AUDIENCE_SEGMENTS,
)
from app.agents.adzump.agents.campaign.models import (
    Channel,
    audience,
    resolve_channel,
    set_audience,
)
from app.agents.adzump.services.business_storage import resolve_url
from app.config import settings
from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream, current_agent_id
from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)

PROVIDER = "deepseek"  # matches the keyword agent it runs alongside
MODEL_TIER = "balanced"  # picking an audience is judgement, not lookup
# load -> select -> demographics is three tool calls; the rest is room to search and revise.
MAX_TURNS = 8
MAX_TOKENS = settings.AGENT_MAX_TOKENS
_BUSINESS_TEXT_MAX = 4000
AUD_MANAGE_HISTORY_TURNS = 4
_MANAGE_REPLY_CAP = 1500  # chars of a stored reply — bounds the seeded history

# No submit_segments in manage mode: it replaces a set wholesale, fabricating rationales for
# segments the model never re-derived. Edits go through edit_audience.
_MANAGE_TOOL_SET = [SEARCH_AUDIENCE_SEGMENTS, *MANAGE_TOOLS, *CUSTOM_SEGMENT_TOOLS]

# A custom segment is drafted on one turn and created on the next, so the drafted terms ride
# the parent session between two throwaway ones - the user confirms what they were shown.
_CARRIED_KEYS = ("aud_custom_candidates", "aud_custom_theme", DRAFT_ID_KEY)
_DRAFT_SEEN = "aud_custom_offered"


def carry_draft(parent_ctx: dict) -> dict:
    """The drafted terms a manage run may submit - good until it is used or redrafted.

    Expiry follows the draft's own id, not a "this was offered" flag. A run that offers a
    draft often redrafts on the same turn, and the flag then killed the NEW list before the
    user's "yes" reached it: the yes failed, the model drafted a third time, and the segment
    was created from terms the user had never seen.
    """
    # Presence, not value: a draft written before this key existed has no id, and comparing
    # two Nones would expire it on the turn it was offered.
    if _DRAFT_SEEN in parent_ctx and parent_ctx[_DRAFT_SEEN] == parent_ctx.get(
        DRAFT_ID_KEY
    ):
        for key in (*_CARRIED_KEYS, _DRAFT_SEEN):
            parent_ctx.pop(key, None)
        return {}
    carried = {k: parent_ctx[k] for k in _CARRIED_KEYS if k in parent_ctx}
    if carried:
        parent_ctx[_DRAFT_SEEN] = parent_ctx.get(DRAFT_ID_KEY)
    return carried


def _reply_text(messages: list[dict]) -> str:
    """The assistant's spoken text across ``messages`` — the audience agent's reply."""
    parts: list[str] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
    return "".join(parts).strip()


class _AudienceStream(ChildAgentStream):
    """Swallows the prose - the panel is the output. Tool calls forward: the run is a minute
    of silence, and a step that came back empty must not look like one that worked."""

    label = "audience_targeting"


class _ManageStream(_AudienceStream):
    """Here the prose IS the answer, not the panel."""

    async def emit_text(self, text: str) -> None:
        await self._parent.emit_text(text)


class AudienceAgent(BaseAgent):
    """One class, two configured singletons: the build instance and the manage instance, so
    neither mode sees the other's instructions.

    All per-run state is isolated in BaseSession; instance attributes are read-only after
    __init__. Do NOT add mutable instance state.
    """

    display_name = "Audience Targeting"
    _instance: AudienceAgent | None = None
    _manage_instance: AudienceAgent | None = None

    def __init__(self, *, manage: bool = False) -> None:
        context = BaseContext(static_prefix=BASE_MANAGE if manage else BASE)
        context.use_static_prefix_only()  # no async docs to load
        super().__init__(
            name="audience_targeting",
            tools=_MANAGE_TOOL_SET if manage else ALL_TOOLS,
            context_builder=context,
            model_tier=MODEL_TIER,
            max_turns=MAX_TURNS,
            max_tokens=MAX_TOKENS,
            provider=PROVIDER,
        )

    @classmethod
    def get_instance(cls) -> AudienceAgent:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def get_manage_instance(cls) -> AudienceAgent:
        if cls._manage_instance is None:
            cls._manage_instance = cls(manage=True)
        return cls._manage_instance

    # context hooks

    async def build_dynamic_context(self, session: BaseSession) -> str:
        ctx = session.context
        parts = [
            f"BUSINESS\n{(ctx.get('aud_business_text') or '')[:_BUSINESS_TEXT_MAX]}"
        ]
        if country := ctx.get("aud_country"):
            parts.append(f"COUNTRY: {country}")
        if regions := ctx.get("aud_hec_regions"):
            parts.append(
                f"AD POLICY — this campaign targets {', '.join(regions)}. If the product is "
                "housing, real estate, employment or credit, Google PROHIBITS narrowing by "
                "age, gender, parental status or marital status there, and ads that do are "
                "disapproved. Leave those open and say why. Income is not on Google's list, "
                "but treat it the same way for these verticals."
            )

        if ctx.get("aud_mode") == "manage":
            # The manage step says "the audience below", so it has to be below. Without it
            # "drop the finance ones" has no labels to work from and the model can only
            # guess at names to look up.
            parts.append(
                _current_audience(audience(ctx) or {}, ctx.get(BLUEPRINTS_KEY))
            )
        return "\n\n".join(parts)

    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str:
        """Inject only the current phase's focused instructions, or nothing once done."""
        ctx = session.context
        phase = Phase.MANAGE if ctx.get("aud_mode") == "manage" else current_phase(ctx)
        if phase is None:
            return ""
        prompt = phase_prompt(phase)
        if phase is Phase.MANAGE:
            prompt = Template(prompt).safe_substitute(
                user_message=ctx.get("aud_user_message", "")
            )
        return prompt

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        # tools read and write aud_* run state here
        ctx["session_context"] = session.context
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    # entry point (called by the Campaign Agent's audience tool)

    async def suggest(
        self,
        *,
        business_text: str,
        ad_account: dict,
        channel: Channel,
        country_code: str,
        hec_regions: list[str],
        parent_event_stream: AgentEventStream,
        auth: AuthContext,
    ) -> AudienceTargetingResult:
        """Build one campaign's audience and return it.

        ``ad_account`` = {customer_id, login_customer_id}. ``channel`` decides which segments
        can actually serve, and is the caller's to supply.
        """
        session = BaseSession(agent_name="audience_targeting")
        await session.get_or_create(None, auth)
        session.context = {
            "aud_business_text": business_text,
            "aud_customer_id": ad_account.get("customer_id", ""),
            "aud_login_customer_id": ad_account.get("login_customer_id", ""),
            "aud_channel_type": channel.google_channel_type.value,
            "aud_country": country_code,
            "aud_hec_regions": hec_regions,
        }

        run_start = time.monotonic()
        await parent_event_stream.emit_agent_started(
            agent_id="audience_targeting",
            label="Audience Targeting",
            parent_id=current_agent_id.get(),
        )
        stream = _AudienceStream(parent_event_stream)
        try:
            await self.run(
                user_message="Choose the audience for this campaign.",
                session=session,
                event_stream=stream,
            )
        except asyncio.CancelledError:
            # A timeout cancels us with CancelledError, which the handler below misses -
            # close the card, then re-raise. No partial audience: an ad group needs them all.
            await stream._emit_finished(
                agent_id="audience_targeting",
                run_start=run_start,
                session=session,
                status="error",
                summary="cancelled",
            )
            raise
        except Exception as exc:
            await stream._emit_finished(
                agent_id="audience_targeting",
                run_start=run_start,
                session=session,
                status="error",
                summary=type(exc).__name__,
            )
            raise

        result = _build_result(session.context)
        logger.info(
            "audience_targeting done segments=%d demographics=%s",
            len(result.signals),
            not result.demographics.is_empty,
        )
        await stream._emit_finished(
            agent_id="audience_targeting",
            run_start=run_start,
            session=session,
            status="success",
            summary=f"{len(result.signals)} segments",
        )
        return result

    # entry point (called by the orchestrator's manage_audience tool)

    async def handle(self, user_message: str, context: dict) -> ToolResult:
        """Answer or edit an existing audience, from the user's verbatim words.

        Throwaway session, as in generation, holding its OWN copy of the audience — handed
        back once the run ends. Sharing the parent's dict instead would carry exactly one
        edit: the build envelope's writer copies, so later edits would land on a copy the
        parent never sees.
        """
        parent_ctx = context.get("session_context")
        if parent_ctx is None:
            return ToolResult(success=False, error="No session context available.")
        auth = context.get("auth")
        if auth is None:
            return ToolResult(
                success=False, error="No auth context for the audience agent."
            )
        dump = audience(parent_ctx)
        if not dump or not dump.get("signals"):
            return ToolResult(
                success=False,
                error="No audience has been built yet — build the campaign first.",
            )

        spec = parent_ctx.get("campaign_spec") or {}
        session = BaseSession(agent_name="audience_targeting")
        await session.get_or_create(None, auth)
        session.context = {
            "aud_mode": "manage",
            "aud_user_message": user_message,
            # Stored raw, as suggest() does — build_dynamic_context is the one truncator.
            "aud_business_text": wider_brief(parent_ctx),
            "aud_customer_id": str(spec.get("account") or ""),
            "aud_login_customer_id": str(spec.get("parent_account") or ""),
            "aud_channel_type": resolve_channel(spec).google_channel_type.value,
            "aud_country": str((dump.get("meta") or {}).get("country") or ""),
            # Seeds custom-segment expansion, and stamps the OWNED_MARKER description.
            "aud_product_name": str(
                (parent_ctx.get("product_data") or {}).get("product_name") or ""
            ),
            # Rides the Planner call as a keywordAndUrlSeed, so Google reads the landing
            # page for ideas rather than working from the theme alone.
            "aud_business_url": resolve_url(parent_ctx) or "",
            # The Planner scores against this; unset, it falls back to its India default.
            "aud_geo": [
                g
                for g in [
                    ((parent_ctx.get("product_data") or {}).get("place") or {}).get(
                        "country_geo_constant"
                    )
                ]
                if g
            ],
            # apply_edit reads the account off this to re-resolve a ref against the catalogue.
            "campaign_spec": spec,
            "campaign_craft_id": parent_ctx.get("campaign_craft_id") or "",
        }
        set_audience(session.context, dump)
        session.context.update(carry_draft(parent_ctx))
        # Its own copy, not a shared ref - _put rebinds, so an alias survives one edit and
        # then silently diverges. Handed back in `finally`.
        session.context[BLUEPRINTS_KEY] = deepcopy(parent_ctx.get(BLUEPRINTS_KEY) or {})

        # Manage mode has no fetch tool, so without this every "add something for X" would
        # dead-end at the search it is told to run first. Cached 24h by the adapter.
        session.context["aud_candidates"] = await catalogue.load(
            customer_id=session.context["aud_customer_id"],
            channel_type=session.context["aud_channel_type"],
            country_code=session.context["aud_country"],
            login_customer_id=session.context["aud_login_customer_id"],
            client_code=context.get("client_code", ""),
            auth_headers=context.get("headers", {}),
        )

        # Replay the recent manage exchanges as conversation history, through the session's
        # own message constructors so the shape stays whatever the providers consume.
        history: list[dict] = parent_ctx.get("aud_conversation") or []
        for past in history[-AUD_MANAGE_HISTORY_TURNS:]:
            session.append_user_message(str(past.get("user", "")))
            session.append_assistant_message(
                [{"type": "text", "text": str(past.get("reply", ""))}]
            )
        seeded_len = len(session.messages)

        parent_stream = context.get("event_stream")
        agent_id = "audience_targeting_manage"
        run_start = time.monotonic()
        if parent_stream is not None:
            await parent_stream.emit_agent_started(
                agent_id=agent_id,
                label="Audience Targeting · manage",
                parent_id=current_agent_id.get(),
            )
        # Unlike generation, the answer is the prose — forward it instead of swallowing it.
        stream = _ManageStream(parent_stream) if parent_stream else AgentEventStream()

        status, summary = "success", "done"
        try:
            await self.run(
                user_message=user_message, session=session, event_stream=stream
            )
        except Exception as exc:
            status, summary = "error", type(exc).__name__
            logger.exception("audience handle failed")
            return ToolResult(
                success=False,
                error="The audience agent couldn't complete that — try rephrasing.",
            )
        finally:
            # Hand the edited audience back. In `finally` because edits already applied are
            # real even when the turn later fails — losing them would contradict what the
            # user was already shown in the panel.
            edited = audience(session.context)
            if edited is not None:
                set_audience(parent_ctx, edited)
            parent_ctx[BLUEPRINTS_KEY] = session.context.get(BLUEPRINTS_KEY) or {}
            for key in _CARRIED_KEYS:
                value = session.context.get(key)
                if value is None:
                    parent_ctx.pop(key, None)  # spent, or never drafted
                else:
                    parent_ctx[key] = value
            if isinstance(stream, ChildAgentStream):
                await stream._emit_finished(
                    agent_id=agent_id,
                    run_start=run_start,
                    session=session,
                    status=status,
                    summary=summary,
                )

        reply = _reply_text(session.messages[seeded_len:])
        if reply:
            parent_ctx["aud_conversation"] = (
                history + [{"user": user_message, "reply": reply[:_MANAGE_REPLY_CAP]}]
            )[-AUD_MANAGE_HISTORY_TURNS:]

        # A drafted-but-unsubmitted segment means this run ended by asking the user to
        # approve terms it has just shown them. Declaring that as an elicitation is what
        # stops the orchestrator asking its own question in the same turn - core breaks the
        # loop after one ask, so their "yes" can only mean the thing they were shown.
        asked = bool(session.context.get("aud_custom_candidates"))

        # The reply already reached the user as forwarded prose. The orchestrator was not
        # told the outcome, so it must not state one.
        return ToolResult(
            success=True,
            summary=(
                "The audience agent asked the user a question (shown above) and is waiting "
                "on their answer. Say nothing further and ask nothing else this turn."
                if asked
                else "The audience agent replied to the user directly (shown above). You "
                "were not told what it changed — do not restate or claim any outcome; "
                "just continue."
            ),
            # elicit_field names the parameter the user's reply fills, so the orchestrator
            # sends it back HERE rather than acting on it - it was never told what was asked.
            data={"elicited": True, "elicit_field": "user_message"} if asked else None,
        )


def _current_audience(dump: dict, blueprints: dict | None = None) -> str:
    """The saved audience as the manage run sees it — what it is editing.

    Ancestry included: "Apartments" under Real Estate and under Employment are opposite
    audiences, and a bare label gives the model no way to tell an ambiguous one apart.
    """
    lines = ["CURRENT AUDIENCE"]
    for s in dump.get("signals") or []:
        where = " > ".join((s.get("path") or [])[:-1])
        mark = "EXCLUDED " if s.get("negative") else ""
        why = f" — {s['rationale']}" if s.get("rationale") else ""
        lines.append(
            f"- {mark}{s.get('label', '')} [{s.get('kind', '')}]"
            + (f" (under {where})" if where else "")
            + why
        )
        # A custom segment's rationale names only its first few terms, so "drop the invoicing
        # one" would be a guess. Listed in full instead.
        plan = (blueprints or {}).get(s.get("ref", ""))
        if plan:
            for field, label in (
                ("terms", "search terms"),
                ("urls", "websites"),
                ("apps", "apps"),
            ):
                items = plan.get(field) or []
                if not items:
                    continue
                shown = [i["keyword"] if isinstance(i, dict) else i for i in items]
                lines.append(f"    {label}: {', '.join(shown)}")

    demo = DemographicSpec.model_validate(dump.get("demographics") or {})
    if demo.is_empty:
        lines.append(
            "DEMOGRAPHICS: none — every age, gender, income and parental status."
        )
    else:
        ages = ", ".join(
            f"{r.min_age}-{r.max_age}" if r.max_age else f"{r.min_age}+"
            for r in demo.age_ranges
        )
        shown = {
            "age_ranges": f"age {ages}" if ages else "",
            "genders": "gender " + ", ".join(g.value for g in demo.genders)
            if demo.genders
            else "",
            "income_ranges": "income " + span_label(demo.income_ranges)
            if demo.income_ranges
            else "",
            "parental_statuses": "parental "
            + ", ".join(p.value for p in demo.parental_statuses)
            if demo.parental_statuses
            else "",
        }
        # Spelled out because submit_demographics REPLACES the whole spec: an edit made
        # without seeing this would silently switch the dropped users back on.
        lines.append(
            "DEMOGRAPHICS: "
            + "; ".join(
                text
                + ("" if demo.includes_undetermined(field) else " (unknown excluded)")
                for field, text in shown.items()
                if text
            )
        )
    return "\n".join(lines)


def _build_result(state: dict) -> AudienceTargetingResult:
    """Assemble the run's recorded state into the result.

    One dimension group holding every positive: segments inside a group are OR'd, and that
    union is the broad shape these campaigns want. Splitting into several groups ANDs them,
    which is a deliberate narrowing nothing asks for yet.
    """
    signals = [AudienceSignal(**s) for s in state.get("aud_segments") or []]
    demographics = DemographicSpec.model_validate(state.get("aud_demographics") or {})
    positives = [s.ref for s in signals if not s.negative]
    return AudienceTargetingResult(
        signals=signals,
        demographics=demographics,
        dimension_groups=[positives] if positives else [],
        meta={"country": state.get("aud_country", "")},
    )


def get_audience_agent() -> AudienceAgent:
    return AudienceAgent.get_instance()


def get_audience_manage_agent() -> AudienceAgent:
    return AudienceAgent.get_manage_instance()
