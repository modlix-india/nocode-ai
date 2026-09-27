"""LeadFormAgent - Multi-Conversational Agent for Meta Instant Forms."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.session import BaseSession
from app.core.streaming import AgentEventStream, pre_emit_agent_started
from app.agents.adzump.agents.leadform.context import BASE_GENERATE, BASE_MANAGE, Phase, phase_prompt
from app.agents.adzump.agents.leadform import tools, manage_tools
from app.agents.adzump.agents.leadform.utils import build_business_context
from app.agents.adzump.agents.leadform.subagent_event_stream import LeadFormEventStream

logger = logging.getLogger(__name__)

LEADFORM_PROVIDER = "deepseek"
LEADFORM_MODEL_TIER = "balanced"
LEADFORM_MAX_TURNS = 15
LEADFORM_MAX_TOKENS = 8192
_MAX_HISTORICAL_FORMS_IN_PROMPT = 5


class LeadFormAgent(BaseAgent):
    """The sub-agent responsible for drafting and editing Lead Generation Forms."""

    display_name = "Lead Form Strategist"

    def __init__(self, mode: str = "generate") -> None:
        if mode == "manage":
            name = "leadform_manage"
            t = manage_tools.ALL_TOOLS
            ctx = BaseContext(static_prefix=BASE_MANAGE)
        else:
            name = "leadform_generate"
            t = tools.ALL_TOOLS
            ctx = BaseContext(static_prefix=BASE_GENERATE)

        super().__init__(
            name=name,
            tools=t,
            context_builder=ctx,
            provider=LEADFORM_PROVIDER,
            model_tier=LEADFORM_MODEL_TIER,
            max_turns=LEADFORM_MAX_TURNS,
            max_tokens=LEADFORM_MAX_TOKENS,
        )

    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str | None:
        """Injects phase-specific guidance based on the current session state."""
        ctx = session.context

        # The current phase is tracked in session state. Defaults to STRATEGY.
        phase_str = ctx.get("lf_phase", Phase.STRATEGY.value)
        try:
            phase = Phase(phase_str)
        except ValueError:
            phase = Phase.STRATEGY

        # Auto-transition ANALYZE → RECOMMEND after one analysis turn.
        # The model sees the ANALYZE prompt this turn (history evaluation),
        # then the RECOMMEND prompt next turn (build instruction + priority rules).
        if phase == Phase.ANALYZE:
            ctx["lf_phase"] = Phase.RECOMMEND.value

        reminder = phase_prompt(phase)

        # Explicitly instruct the LLM to call update_form_recommendation when an image is pending
        if ctx.get("_pending_uploads"):
            user_msg = ctx.get("lf_user_message", "").lower()
            image_keywords = ["image", "photo", "background", "bg", "picture", "cover"]
            if any(kw in user_msg for kw in image_keywords):
                reminder += (
                    "\n\n## Pending Cover Image\n"
                    "The user has uploaded an image that is waiting to be applied as the "
                    "lead form background. Call `update_form_recommendation` now — the "
                    "system will automatically upload it to Meta and attach it to the draft. "
                    "You do not need any image URL or ID."
                )

        return reminder

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Injects business context and learned knowledge into the system prompt."""
        parts = [await super().build_dynamic_context(session)]
        
        ctx = session.context
        if "business_context" in ctx and ctx["business_context"]:
            parts.append(f"BusinessContext:\n{json.dumps(ctx['business_context'], separators=(',', ':'))}")
            
        if "advertiser_knowledge" in ctx and ctx["advertiser_knowledge"]:
            parts.append(f"Advertiser Knowledge (Historical Forms Analysis):\n{json.dumps(ctx['advertiser_knowledge'], separators=(',', ':'))}")

        raw_forms = ctx.get("historical_forms")
        if isinstance(raw_forms, list) and raw_forms:
            projected_forms = []
            for form in raw_forms[:_MAX_HISTORICAL_FORMS_IN_PROMPT]:
                if hasattr(form, "model_dump"):
                    form = form.model_dump()
                if isinstance(form, dict):
                    projected = {
                        "name": form.get("name", ""),
                        "is_higher_intent": form.get("is_higher_intent", False),
                        "questions": form.get("questions", []),
                    }
                    if "leads_count" in form:
                        projected["leads_count"] = form["leads_count"]
                    projected_forms.append(projected)
                else:
                    projected_forms.append(form)

            parts.append(
                f"Historical Forms (Raw):\n{json.dumps(projected_forms, separators=(',', ':'))}"
            )

        if "lead_form_draft" in ctx and ctx["lead_form_draft"]:
            parts.append(
                f"Current Lead Form Draft (read this before editing — "
                f"preserve every field the user did NOT ask to change):\n"
                f"{json.dumps(ctx['lead_form_draft'], separators=(',', ':'))}"
            )
            
        return "\n\n".join(filter(bool, parts))

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Expose the (sub-)session state and auth to the internal tools."""
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        if session.auth:
            ctx["auth"] = session.auth
        return ctx


_agent_cache: dict[str, LeadFormAgent] = {}


async def get_leadform_agent(mode: str) -> LeadFormAgent:
    """Shared LeadFormAgent singleton."""
    if mode not in _agent_cache:
        agent = LeadFormAgent(mode)
        await agent.context_builder.load()
        _agent_cache[mode] = agent
    return _agent_cache[mode]


# Maximum number of recent conversation messages preserved in sub-agent history to bound prompt token growth.
_MAX_SUBAGENT_MESSAGES: int = 6

_LEADFORM_CONTEXT_KEYS: tuple[str, ...] = (
    "product_data",
    "campaign_spec",
    "business_context",
    "lead_form_draft",
    "historical_forms",
    "advertiser_knowledge",
    "lead_form_published",
    "meta_lead_form_id",
    "_pending_uploads",
    "lf_phase",
    "awaiting_publish_confirmation",
)


async def run_leadform_session(
    user_message: str,
    parent_ctx: dict,
    stream: AgentEventStream | None,
    tool_use_id: str,
    auth_context: Any
) -> str:
    """Encapsulates the entire Lead Form sub-agent lifecycle."""
    session = BaseSession(agent_name="leadform_agent")
    draft = parent_ctx.get("lead_form_draft")
    mode = "MANAGE" if draft else "GENERATE"

    if mode == "MANAGE":
        session_id = parent_ctx.get("lf_manage_session_id")
        actual_session_id = await session.get_or_create(session_id or None, auth_context)
        parent_ctx["lf_manage_session_id"] = actual_session_id

        # Bound transcript length to prevent unbounded token growth
        if len(session.messages) > _MAX_SUBAGENT_MESSAGES:
            session.messages = session.messages[-_MAX_SUBAGENT_MESSAGES:]

        # Populate only relevant keys rather than duplicating the entire parent context
        for k in _LEADFORM_CONTEXT_KEYS:
            if k in parent_ctx:
                session.context[k] = parent_ctx[k]

        # Dedicated craft ID: do not inherit the parent orchestrator's adzump_<session_id> panel
        parent_craft = parent_ctx.get("craft_id", "")
        if parent_craft and not parent_craft.startswith("adzump_"):
            session.context["craft_id"] = parent_craft
        else:
            session.context.setdefault("craft_id", "leadform_craft")

        if "business_context" not in session.context:
            session.context["business_context"] = build_business_context(parent_ctx.get("product_data", {})).model_dump()
        session.context["lf_phase"] = Phase.MANAGE.value
        session.context["lf_user_message"] = user_message

        agent = await get_leadform_agent("manage")

    else:  # GENERATE
        session_id = parent_ctx.get("lf_gen_session_id")

        actual_session_id = await session.get_or_create(session_id or None, auth_context)
        parent_ctx["lf_gen_session_id"] = actual_session_id

        # Bound transcript length to prevent unbounded token growth
        if len(session.messages) > _MAX_SUBAGENT_MESSAGES:
            session.messages = session.messages[-_MAX_SUBAGENT_MESSAGES:]

        # Preserve restored context (historical forms, analysis, phase) while syncing latest parent inputs
        session.context["product_data"] = parent_ctx.get("product_data", {})
        session.context["campaign_spec"] = parent_ctx.get("campaign_spec", {})
        # Dedicated craft ID: do not inherit the parent orchestrator's adzump_<session_id> panel
        parent_craft = parent_ctx.get("craft_id", "")
        if parent_craft and not parent_craft.startswith("adzump_"):
            session.context["craft_id"] = parent_craft
        else:
            session.context["craft_id"] = "leadform_craft"
        if "business_context" not in session.context:
            session.context["business_context"] = build_business_context(session.context["product_data"]).model_dump()

        agent = await get_leadform_agent("generate")

    if stream is not None:
        await pre_emit_agent_started(
            stream, agent_id="leadform_agent", label="Lead Form Strategist",
            parent_tool_use_id=tool_use_id, context=parent_ctx,
        )
    wrapped = LeadFormEventStream(stream) if stream else AgentEventStream()

    status = "success"
    run_started = time.monotonic()
    try:
        await agent.run(user_message=user_message, session=session, event_stream=wrapped)
    except Exception:
        status = "failed"
        logger.exception("Leadform agent run failed")
    finally:
        if stream is not None:
            try:
                await stream.emit_agent_finished(
                    "leadform_agent", status=status,
                    duration_ms=int((time.monotonic() - run_started) * 1000),
                )
            except Exception as exc:
                logger.debug("Failed to emit leadform_agent finished event: %s", exc)

    keys_to_sync = [
        "lead_form_draft", 
        "advertiser_knowledge", 
        "historical_forms", 
        "lead_form_published",
        "meta_lead_form_id",
        "_pending_uploads",
        "awaiting_publish_confirmation",
    ]
    for key in keys_to_sync:
        if key in session.context:
            parent_ctx[key] = session.context[key]

    return status
