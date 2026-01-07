"""Page Agent - Orchestrates sub-agents to generate/modify pages"""
import asyncio
import uuid
from typing import Dict, Any, Optional, List, Tuple
import logging

from app.config import settings

from app.agents.base import AgentInput, AgentOutput, KEEPALIVE_INTERVAL
from app.agents.layout import LayoutAgent
from app.agents.component import ComponentAgent
from app.agents.events import EventsAgent
from app.agents.styles import StylesAgent
from app.agents.animation import AnimationAgent
from app.agents.data import DataAgent
from app.agents.review import ReviewAgent
from app.agents.website_analyzer import WebsiteAnalyzerAgent
from app.utils.merge import merge_agent_outputs
from app.streaming.events import ProgressCallback

# Import from page_generation module
from app.agents.page_generation import (
    PageAgentMode,
    PageAgentOptions,
    DeviceScreenshots,
    RequestFile,
    RequestTheme,
    RequestFontPack,
    PageAgentRequest,
    AgentLogEntry,
    TokenUsageByAgent,
    TokenUsageSummary,
    ContextUsageInfo,
    PageAgentResponse,
    get_context_builder,
    get_request_detector,
    StyleOnlyExecutor,
    ImportModeExecutor,
    InspiredByModeExecutor,
    SessionManager,
)

logger = logging.getLogger(__name__)

# Re-export models for backward compatibility
__all__ = [
    "PageAgentMode",
    "PageAgentOptions",
    "DeviceScreenshots",
    "RequestFile",
    "RequestTheme",
    "RequestFontPack",
    "PageAgentRequest",
    "AgentLogEntry",
    "TokenUsageByAgent",
    "TokenUsageSummary",
    "ContextUsageInfo",
    "PageAgentResponse",
    "PageAgent",
]


class PageAgent:
    """
    Page Agent: Orchestrates sub-agents to generate or modify pages.

    Multi-Model Strategy:
    - Haiku (fast, cheap): Analysis phases, simple agents (Styles, Animation, Data)
    - Sonnet (balanced): Generation phases, complex agents (Review)

    Supports three modes:
    - CREATE: Generate new page from instruction
    - MODIFY: Modify existing page based on instruction
    - ENHANCE: Add features to existing page without changing structure

    Execution Phases:
    1. Foundation: Layout (analyze→generate) + Component (analyze→generate)
    2. Enhancement: Events (analyze→generate), Styles, Animation, Data
    3. Merge: Combine all outputs
    4. Review: Validate and improve
    """

    RATE_LIMIT_DELAY_HAIKU = 2
    RATE_LIMIT_DELAY_SONNET = 5

    def __init__(self):
        # Initialize all sub-agents
        self.layout_agent = LayoutAgent()
        self.component_agent = ComponentAgent()
        self.events_agent = EventsAgent()
        self.styles_agent = StylesAgent()
        self.animation_agent = AnimationAgent()
        self.data_agent = DataAgent()
        self.review_agent = ReviewAgent()
        self.website_analyzer = WebsiteAnalyzerAgent()

        # Initialize helpers
        self.context_builder = get_context_builder()
        self.detector = get_request_detector()
        self.session_manager = SessionManager()

        # Initialize executors
        self.style_only_executor = StyleOnlyExecutor(
            self.styles_agent, self.animation_agent, self.context_builder
        )
        self.import_executor = ImportModeExecutor()
        self.inspired_by_executor = InspiredByModeExecutor(
            self.layout_agent, self.component_agent, self.events_agent,
            self.styles_agent, self.animation_agent, self.data_agent
        )

    async def _sleep_with_keepalive(
        self,
        seconds: int,
        progress: Optional[ProgressCallback],
        message: str = "Waiting for rate limit..."
    ):
        """Sleep while sending keepalives to prevent connection timeout"""
        elapsed = 0
        while elapsed < seconds:
            sleep_time = min(KEEPALIVE_INTERVAL, seconds - elapsed)
            await asyncio.sleep(sleep_time)
            elapsed += sleep_time
            if progress and elapsed < seconds:
                await progress.keepalive(message)

    async def execute(
        self,
        request: PageAgentRequest,
        progress_callback: Optional[ProgressCallback] = None,
        auth_context: Optional[Dict[str, Any]] = None
    ) -> PageAgentResponse:
        """
        Main entry point for page generation/modification.

        Args:
            request: The page generation request
            progress_callback: Optional callback for progress updates
            auth_context: Optional auth context with clientCode, clientId, userId, appCode
        """
        request_id = str(uuid.uuid4())
        session_id: Optional[str] = None
        turn_number: Optional[int] = None
        token_usages: List[Dict[str, Any]] = []

        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)

        # Initialize session if tracking is enabled
        if settings.AI_TRACKING_ENABLED and auth_context:
            session_id, turn_number = await self.session_manager.initialize_session(
                request, auth_context, request_id
            )

        # Check for import mode
        detected_url = self.detector.detect_url_in_instruction(request.instruction)
        logger.info(f"[PageAgent] Detected URL: {detected_url}, sourceUrl: {request.sourceUrl}, mode: {request.options.mode}")

        if request.options.mode == PageAgentMode.IMPORT or request.sourceUrl or detected_url:
            if not request.sourceUrl and detected_url:
                request.sourceUrl = detected_url
                logger.info(f"Auto-detected URL for import: {detected_url}")

            is_exact_copy = self.detector.is_exact_copy_request(request.instruction)

            if is_exact_copy:
                logger.info("Exact copy requested - using direct extraction mode")
                return await self.import_executor.execute(request, progress_callback)
            else:
                logger.info("Inspired-by mode - using LLM agents with website reference")
                return await self.inspired_by_executor.execute(request, progress_callback)

        # Check for style-only modification
        is_style_only = self.detector.is_style_modification(request.instruction)

        if is_style_only and request.options.mode == PageAgentMode.MODIFY:
            return await self.style_only_executor.execute(request, progress_callback)

        # Determine which agents are needed
        agents_needed = self.detector.determine_agents_needed(request)

        await emit('status', f"Starting page generation in {request.options.mode.value} mode...")
        await emit('status', f"Agents to run: {', '.join(agents_needed)}")
        await emit('status', "Using multi-model strategy: Haiku for analysis, Sonnet for generation")

        agent_logs: Dict[str, AgentLogEntry] = {}
        context = self.context_builder.build_context(request)

        # ========== Phase 1: Foundation ==========
        await emit('phase', 'Foundation (Layout + Component)')

        foundation_input = AgentInput(
            user_request=request.instruction,
            context=context,
            previous_outputs={}
        )

        layout_result = None
        component_result = None

        if "layout" in agents_needed and (not request.options.preserveLayout or request.existingPage is None):
            name, layout_result = await self._run_agent_with_progress(
                self.layout_agent, "Layout", foundation_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(layout_result, self.layout_agent)
            if layout_result.token_usage:
                token_usages.append(layout_result.token_usage)
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_SONNET, progress_callback, "Waiting before Component..."
            )

        if "component" in agents_needed:
            # Component agent needs to see Layout output to know what containers exist
            component_input = AgentInput(
                user_request=request.instruction,
                context=context,
                previous_outputs={
                    "layout": layout_result.result if layout_result else {}
                }
            )
            name, component_result = await self._run_agent_with_progress(
                self.component_agent, "Component", component_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(component_result, self.component_agent)
            if component_result.token_usage:
                token_usages.append(component_result.token_usage)

        # ========== Phase 2: Enhancement ==========
        await emit('phase', 'Enhancement (Events, Styles, Animation, Data)')

        enhancement_input = AgentInput(
            user_request=request.instruction,
            context=context,
            previous_outputs={
                "layout": layout_result.result if layout_result else {},
                "component": component_result.result if component_result else {}
            }
        )

        outputs = {}
        if layout_result:
            outputs["layout"] = layout_result.result
        elif request.existingPage:
            outputs["layout"] = {"rootComponent": request.existingPage.get("rootComponent")}

        if component_result:
            outputs["component"] = component_result.result

        # Run enhancement agents sequentially
        if "events" in agents_needed and (not request.options.preserveEvents or request.existingPage is None):
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_SONNET, progress_callback, "Waiting before Events..."
            )
            name, events_result = await self._run_agent_with_progress(
                self.events_agent, "Events", enhancement_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(events_result, self.events_agent)
            outputs["events"] = events_result.result
            if events_result.token_usage:
                token_usages.append(events_result.token_usage)

        if "styles" in agents_needed and (not request.options.preserveStyles or request.existingPage is None):
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Styles..."
            )
            name, styles_result = await self._run_agent_with_progress(
                self.styles_agent, "Styles", enhancement_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(styles_result, self.styles_agent)
            outputs["styles"] = styles_result.result
            if styles_result.token_usage:
                token_usages.append(styles_result.token_usage)

        if "animation" in agents_needed:
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Animation..."
            )
            name, animation_result = await self._run_agent_with_progress(
                self.animation_agent, "Animation", enhancement_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(animation_result, self.animation_agent)
            outputs["animation"] = animation_result.result
            if animation_result.token_usage:
                token_usages.append(animation_result.token_usage)

        if "data" in agents_needed:
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Data..."
            )
            name, data_result = await self._run_agent_with_progress(
                self.data_agent, "Data", enhancement_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(data_result, self.data_agent)
            outputs["data"] = data_result.result
            if data_result.token_usage:
                token_usages.append(data_result.token_usage)

        # Preserve existing data if options set or agents were skipped
        if request.existingPage:
            self._preserve_existing_outputs(request, outputs, agents_needed)

        # ========== Phase 3: Merge ==========
        await emit('merging', 'Merging agent outputs...')
        merged_page = merge_agent_outputs(outputs, request.existingPage)

        # ========== Phase 4: Review ==========
        await self._sleep_with_keepalive(
            self.RATE_LIMIT_DELAY_SONNET, progress_callback, "Waiting before Review..."
        )
        await emit('phase', 'Review (Validation)')

        review_input = AgentInput(
            user_request=request.instruction,
            context={"merged_page": merged_page, "mode": request.options.mode.value},
            previous_outputs=outputs
        )

        _, review_result = await self._run_agent_with_progress(
            self.review_agent, "Review", review_input, progress_callback
        )
        agent_logs["review"] = self._format_agent_log(review_result, self.review_agent)
        if review_result.token_usage:
            token_usages.append(review_result.token_usage)

        final_page = review_result.result if review_result.success else merged_page

        # ========== Phase 5: Token Tracking ==========
        token_usage_summary = None
        context_usage_info = None

        if settings.AI_TRACKING_ENABLED and auth_context and session_id:
            token_usage_summary, context_usage_info = await self.session_manager.record_token_usage(
                session_id=session_id,
                request_id=request_id,
                auth_context=auth_context,
                token_usages=token_usages,
                instruction=request.instruction,
                final_page=final_page
            )

        response = PageAgentResponse(
            success=all(log.status == "success" for log in agent_logs.values()),
            page=final_page,
            agentLogs=agent_logs,
            sessionId=session_id,
            turnNumber=turn_number,
            tokenUsage=token_usage_summary,
            contextUsage=context_usage_info
        )

        return response

    async def _run_agent_with_progress(
        self,
        agent,
        name: str,
        input: AgentInput,
        progress: Optional[ProgressCallback]
    ) -> Tuple[str, AgentOutput]:
        """Run an agent with progress reporting"""
        if progress:
            model_name = getattr(agent, 'model', 'unknown').split('/')[-1]
            await progress.agent_start(name, f"Starting {name} (using {model_name.split('-')[1] if '-' in model_name else model_name})...")

        result = await agent.execute(input, progress)

        if progress:
            await progress.agent_complete(
                name,
                result.success,
                f"{name} {'completed' if result.success else 'failed'}"
            )

        return (name, result)

    def _format_agent_log(self, result: AgentOutput, agent=None) -> AgentLogEntry:
        """Format agent output as log entry"""
        model = getattr(agent, 'model', None) if agent else None
        return AgentLogEntry(
            status="success" if result.success else "error",
            reasoning=result.reasoning,
            errors=result.errors,
            model=model
        )

    def _preserve_existing_outputs(
        self,
        request: PageAgentRequest,
        outputs: Dict[str, Any],
        agents_needed: List[str]
    ):
        """Preserve existing page data when agents are skipped or preserve options are set."""
        if request.options.preserveEvents and "events" not in outputs:
            outputs["events"] = {"eventFunctions": request.existingPage.get("eventFunctions", {})}
        elif "events" not in outputs and "events" not in agents_needed:
            outputs["events"] = {"eventFunctions": request.existingPage.get("eventFunctions", {})}

        if request.options.preserveStyles and "styles" not in outputs:
            outputs["styles"] = {"componentStyles": {}}
        elif "styles" not in outputs and "styles" not in agents_needed:
            existing_styles = {}
            comp_def = request.existingPage.get("componentDefinition", {})
            for comp_key, comp in comp_def.items():
                if comp.get("styleProperties"):
                    existing_styles[comp_key] = {"rootStyle": comp.get("styleProperties", {})}
            outputs["styles"] = {"componentStyles": existing_styles}

        if "layout" not in outputs and "layout" not in agents_needed:
            outputs["layout"] = {
                "rootComponent": request.existingPage.get("rootComponent"),
                "componentDefinition": request.existingPage.get("componentDefinition", {})
            }

        if "component" not in outputs and "component" not in agents_needed:
            outputs["component"] = {
                "components": request.existingPage.get("componentDefinition", {})
            }

        if "animation" not in outputs and "animation" not in agents_needed:
            outputs["animation"] = {"componentAnimations": {}}

        if "data" not in outputs and "data" not in agents_needed:
            outputs["data"] = {
                "storeInitialization": request.existingPage.get("properties", {}).get("storeInitialization", {})
            }
