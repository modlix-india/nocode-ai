"""OptimizationAgent — campaign health diagnosis and recommendations.

Mirrors ProductAgent's structure exactly:
- Singleton via get_instance()
- BaseAgent subclass with name, tools, context_builder
- build_dynamic_context() renders per-turn state
- build_tool_context() exposes session state to tools
- run_headless() for scheduler path (no LLM, fixed sequence)

Design decisions mirrored from ProductAgent:
- ANALYST_PROVIDER = "anthropic" (server-side tools)
- Singleton pattern so one instance serves all requests
- _PassthroughEventStream pattern when called as a sub-agent

Key difference from ProductAgent:
- Has TWO execution modes: run() for chat, run_headless() for scheduler
- run_headless() calls tool execute functions directly, no LLM, no streaming
"""

from __future__ import annotations


import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter

from app.core.agent import BaseAgent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream
from app.core.tools.base import ToolResult
from app.agents.adzump.agents.optimization.context import build_optimization_context
from app.agents.adzump.agents.optimization.platform_registry import (
    SHARED_TOOLS,
    build_optimization_tool_map,
    is_platform_implemented,
    normalize_platform,
)
from app.agents.adzump.recommendations.models import (
    CampaignOverview,
    CampaignRecommendation,
    ConversionHealthReport,
    ConversionSignal,
)

logger = logging.getLogger(__name__)

OPTIMIZATION_MAX_TURNS = 15
OPTIMIZATION_MAX_TOKENS = 8192  # High token limit for analysis tools with long outputs


class OptimizationAgent(BaseAgent):
    """Diagnoses campaign health and surfaces recommendations.

    Two execution modes:
    1. run()          — LLM-orchestrated, called from chat via tools/optimize.py
    2. run_headless() — Fixed sequence, called from ScheduledOptimizationRunner
    """

    display_name = "Optimization Analyst"
    _instance: "OptimizationAgent | None" = None

    def __init__(self) -> None:
        context = build_optimization_context()
        # Architectural coupling note: Direct private attribute mapping is used here as a
        # performance optimization. It pre-populates the context cache to avoid redundant
        # dynamic text rendering on every conversation turn since the system prompt is static.
        context._cached_static_text = context._static_prefix

        super().__init__(
            name="optimization_analyst",
            tools=[],  # self.tools unused — platform-specific map via overrides below
            context_builder=context,
            max_turns=OPTIMIZATION_MAX_TURNS,
            max_tokens=OPTIMIZATION_MAX_TOKENS,
        )

    @classmethod
    def get_instance(cls) -> "OptimizationAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("OptimizationAgent created")
        return cls._instance

    # Run — resolve platform once before LLM loop

    def get_anthropic_tools_for_session(
        self, session: BaseSession
    ) -> list[dict[str, Any]]:
        return self._get_filtered_tools(session)

    async def run(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: AgentEventStream,
        image_blocks: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> None:
        # AgentCard lifecycle (agent_started/finished) is owned by the spawner
        # (spawn_sub_agent), per BaseAgent.run's contract — we do not take a
        # parent_tool_use_id here. This override only normalizes the platform
        # before the LLM loop.
        platform = session.context.get("platform")
        if platform:
            session.context["platform"] = normalize_platform(platform)
        return await super().run(
            user_message=user_message,
            session=session,
            event_stream=event_stream,
            image_blocks=image_blocks,
            model_override=model_override,
        )

    def _get_platform_tool_map(self, session: BaseSession) -> dict[str, Any]:
        return build_optimization_tool_map(session.context.get("platform"))

    def _get_filtered_tools(self, session: BaseSession) -> list[dict[str, Any]]:
        """Expose only the shared + applicable platform tools for this run.
        Applicability (product mapping AND channel capability) comes from the
        provider — the SAME source the scheduler uses — so the LLM never sees a
        tool that can't run for this campaign's type."""
        platform = session.context.get("platform")
        if not is_platform_implemented(platform):
            return [t.to_anthropic_tool() for t in SHARED_TOOLS]

        tools = self._get_platform_tool_map(session)
        from app.agents.adzump.agents.optimization.platform_registry import get_provider

        provider = get_provider(platform)
        if provider:
            campaign_type = session.context.get("campaign_type", "UNKNOWN")
            has_mapping = bool(session.context.get("mapping_exists"))
            skips = dict(provider.applicable_tools_for_campaign(campaign_type, has_mapping))
            # Drop skipped platform tools; shared tools (absent from the map) pass.
            tools = {k: v for k, v in tools.items() if skips.get(k) is None}

        return [tool.to_anthropic_tool() for tool in tools.values()]

    async def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session: BaseSession,
        event_stream: AgentEventStream | None = None,
        tool_use_id: str = "",
    ) -> ToolResult:
        """Execute tools from a request-scoped platform map.

        BaseAgent looks up tools on ``self.tools``. OptimizationAgent is a
        singleton, so mutating ``self.tools`` per request is unsafe under
        concurrent Google/Meta runs.
        """
        tool = self._get_platform_tool_map(session).get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Unknown tool: {tool_name}")
        if not tool.execute:
            return ToolResult(
                success=False,
                error=f"Tool {tool_name} has no execute function",
            )

        context = self.build_tool_context(session)
        if event_stream:
            context["event_stream"] = event_stream
        if tool_use_id:
            context["tool_use_id"] = tool_use_id

        try:
            result = await tool.execute(tool_input, context)
            if not result.success:
                session.context.setdefault("_errors", []).append(
                    {
                        "tool": tool_name,
                        "error": result.error or "Unknown tool error",
                    }
                )
            return result
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            err_msg = f"Tool execution error: {type(e).__name__}: {e}"
            session.context.setdefault("_errors", []).append(
                {
                    "tool": tool_name,
                    "error": err_msg,
                }
            )
            return ToolResult(
                success=False,
                error=err_msg,
            )

    # Dynamic context — rendered every turn

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Render per-turn context for the LLM.

        Shows:
        - Which campaign is being analysed
        - Conversion signal status (from PR 1 — cheap, always present)
        - Last tool results if any
        - What the user asked
        """
        ctx = session.context
        campaign_id = ctx.get("active_campaign_id", "")
        account_id = ctx.get("account_id", "")
        platform = ctx.get("platform")

        lines = ["## Optimization Context"]

        if campaign_id:
            lines.append(f"Campaign: {campaign_id}")
        if platform:
            lines.append(f"Platform: {platform}")
        else:
            lines.append("Platform: None (Could not be resolved)")
        if account_id:
            lines.append(f"Account: {account_id}")

        # Campaign type + any analyses it rules out, so the LLM explains skips
        # naturally instead of staying silent about a hidden tool.
        campaign_type = ctx.get("campaign_type")
        if campaign_type and campaign_type != "UNKNOWN":
            lines.append(f"Campaign type: {campaign_type}")
            from app.agents.adzump.agents.optimization.platform_registry import (
                get_provider,
            )

            provider = get_provider(platform) if platform else None
            if provider:
                for _name, skip in provider.applicable_tools_for_campaign(
                    campaign_type, bool(ctx.get("mapping_exists"))
                ):
                    if skip is not None and skip.section is not None:
                        lines.append(f"- Not applicable for {campaign_type}: {skip.reason}")

        # Conversion signal — the PR 1 cheap signal, always shown
        signal_raw = ctx.get("conversion_signal")
        if signal_raw:
            try:
                signal = ConversionSignal(**signal_raw)
                lines.append(signal.to_context_line())
            except Exception:
                logger.info(
                    "dynamic_context: conversion_signal render failed", exc_info=True
                )

        # Last conversion health summary if tool was called this session
        health_raw = ctx.get("_last_conversion_health")
        if health_raw:
            try:
                report = ConversionHealthReport(**health_raw)
                lines.append(report.to_context_summary())
            except Exception:
                logger.info(
                    "dynamic_context: conversion_health render failed", exc_info=True
                )

        # Storage status
        has_stored = ctx.get("_has_stored_recommendations", False)
        if has_stored:
            stored_at = ctx.get("_stored_recommendations_at", "")
            lines.append(
                "Stored recommendations: available"
                + (f" (generated: {stored_at})" if stored_at else "")
            )
        else:
            lines.append("Stored recommendations: none — fresh analysis required")

        lines.append("")
        lines.append("## Instructions")
        if platform:
            lines.append(
                "Call get_recommendations first. "
                "Only call analysis tools if the user asks for a fresh analysis "
                "or stored recommendations are not available."
            )
        else:
            lines.append(
                "No active campaign platform has been resolved. "
                "Do not attempt to call any platform-specific diagnostic tools. "
                "Ask the user to clarify which campaign or platform they want to optimize."
            )

        if platform and not ctx.get("mapping_exists"):
            lines.append(
                "\nNote: Product-dependent analysis tools have been disabled because this campaign "
                "is not mapped to a product. Proceed with the remaining available diagnostic tools. "
                "Do not attempt to guess or hallucinate product details."
            )

        from app.agents.adzump.agents.optimization.platform_registry import (
            get_platform_instructions,
        )

        platform_instructions = get_platform_instructions(platform)
        if platform_instructions:
            lines.append(
                f"\n## Platform-Specific Rules\n{platform_instructions.strip()}"
            )

        return "\n".join(lines)

    # Tool context — what tools see in their context dict

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    # Headless execution — called by ScheduledOptimizationRunner

    async def run_headless(
        self,
        campaign_id: str,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        platform: str = "GOOGLE",
        product_id: str = "",
        product_name: str = "",
        campaign_type: str = "UNKNOWN",
        campaign_name: str = "",
        overview: CampaignOverview | None = None,
        auth: AuthContext = None,
    ) -> CampaignRecommendation | None:
        """Execute the full analysis pipeline without LLM orchestration.

        Called by ScheduledOptimizationRunner. Dispatches execution to the
        platform provider's registered scheduler tools in their defined sequence.

        All results are merged into a CampaignRecommendation and returned
        for storage. No streaming, no LLM calls.

        Returns None if all tools fail or return empty results — the scheduler
        skips storage for campaigns without actionable recommendations.
        """
        logger.info(
            "optimization_agent.run_headless: campaign=%s account=%s platform=%s",
            campaign_id,
            account_id,
            platform,
        )

        platform = normalize_platform(platform)

        tools = build_optimization_tool_map(platform)

        tool_ctx = {
            "headers": auth_headers,
            "client_code": client_code,
            "session_context": {
                "account_id": account_id,
                "login_customer_id": login_customer_id,
                "platform": platform,
                "active_campaign_id": campaign_id,
                "product_id": product_id,
                "campaign_name": campaign_name,
                "campaign_type": campaign_type,
            },
        }

        # In cron/headless execution paths, there is no active WebSocket user connection.
        # To avoid a telemetry and billing leak where LLM tokens consumed by underlying
        # sub-advisors (e.g. keyword seed expansion) are uncounted, we dynamically instantiate
        # a virtual BaseSession. We scope it using the campaign's active AuthContext,
        # allowing background cron campaigns to be correctly recorded and billed.
        if auth:
            import uuid

            virtual_session_id = f"sched_{campaign_id}_{uuid.uuid4().hex[:8]}"
            session = BaseSession(agent_name="optimization_agent")
            session.session_id = virtual_session_id
            session.auth = auth
            tool_ctx["_session"] = session

        from app.agents.adzump.agents.optimization.platform_registry import get_provider

        provider = get_provider(platform)
        if not provider:
            logger.warning(
                "run_headless: no provider registered for platform=%s", platform
            )
            return None

        # Check capabilities
        if not provider.capabilities.has_scheduler:
            logger.warning(
                "run_headless: platform=%s does not support scheduler execution",
                platform,
            )
            return None

        tool_results = {}
        failures = {}

        async def _execute_tool(tool_name: str):
            try:
                result = await tools[tool_name].execute(
                    {"campaign_id": campaign_id}, tool_ctx
                )
                return tool_name, result
            except Exception as exc:
                logger.exception(
                    "run_headless: %s failed campaign=%s",
                    tool_name,
                    campaign_id,
                )
                return tool_name, exc

        # Single source of truth for which tools apply (product mapping AND
        # channel capability). Skipped tools cost no API/LLM call.
        applicable = dict(
            provider.applicable_tools_for_campaign(campaign_type, bool(product_id))
        )
        skipped_analyses: list[dict] = []
        tools_to_run = []
        for t in provider.scheduler_tool_order:
            skip = applicable.get(t)
            if skip is not None:
                logger.info(
                    "run_headless: skipping %s for campaign=%s (%s)",
                    t, campaign_id, skip.reason,
                )
                # Channel-applicability skips become an honest SkippedAnalysis +
                # a synthetic result (so build_fields sees an empty section, not
                # an absent key). Setup skips (no section) just don't run.
                if skip.section is not None:
                    tool_results[t] = {
                        "success": True,
                        "data": {"not_applicable": True},
                        "error": None,
                    }
                    skipped_analyses.append({
                        "section": skip.section,
                        "campaign_type": campaign_type,
                        "reason": skip.reason,
                    })
                continue
            tools_to_run.append(t)

        results = []
        for tool_id in tools_to_run:
            _name, result = await _execute_tool(tool_id)
            results.append((_name, result))

        for tool_name, result in results:
            if isinstance(result, Exception):
                failures[tool_name] = f"{type(result).__name__}: {result}"
                tool_results[tool_name] = {"success": False, "error": str(result)}
                continue

            tool_results[tool_name] = {
                "success": result.success,
                "data": result.data,
                "error": result.error,
            }
            if not result.success:
                failures[tool_name] = result.error or "unknown error"
                logger.info(
                    "run_headless: %s failed/skipped campaign=%s error=%s",
                    tool_name,
                    campaign_id,
                    result.error,
                )

        # Ensure overview carries campaign_id so populate_fingerprints can fire
        if overview is None:
            from app.agents.adzump.recommendations.models import CampaignOverview
            overview = CampaignOverview(campaign_id=campaign_id)
        elif not overview.campaign_id:
            overview = overview.model_copy(update={"campaign_id": campaign_id})

        fields = provider.build_fields_from_headless_results(tool_results, overview)

        if not provider.has_actionable_recommendations(fields):
            logger.warning(
                "run_headless: all tools failed or generated empty results for campaign=%s failures=%s — skipping storage",
                campaign_id,
                failures,
            )
            return None

        rec_data = {
            "platform": platform,
            "parent_account_id": login_customer_id,
            "account_id": account_id,
            "product_id": product_id,
            "product_name": product_name,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name or campaign_id,
            "campaign_type": campaign_type,
            "skipped_analyses": skipped_analyses,
            "completed": False,
            "active": True,
            "source": "scheduler",
            "fields": fields,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        rec = TypeAdapter(CampaignRecommendation).validate_python(rec_data)

        logger.info(
            "run_headless done: campaign=%s platform=%s",
            campaign_id,
            platform,
        )

        return rec


def get_optimization_agent() -> OptimizationAgent:
    """Module-level accessor for the shared singleton."""
    return OptimizationAgent.get_instance()
