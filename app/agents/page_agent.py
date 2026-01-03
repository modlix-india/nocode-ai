"""Page Agent - Orchestrates sub-agents to generate/modify pages"""
import asyncio
import json
import re
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
from pydantic import BaseModel
from enum import Enum
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

logger = logging.getLogger(__name__)


class PageAgentMode(str, Enum):
    CREATE = "create"      # Generate new page from scratch
    MODIFY = "modify"      # Modify specific aspects of existing page
    ENHANCE = "enhance"    # Add features to existing page
    IMPORT = "import"      # Import from external website URL


class PageAgentOptions(BaseModel):
    """Options for page generation"""
    mode: PageAgentMode = PageAgentMode.CREATE
    preserveEvents: bool = False   # Keep existing events when modifying
    preserveStyles: bool = False   # Keep existing styles when modifying
    preserveLayout: bool = False   # Keep existing layout when modifying


class DeviceScreenshots(BaseModel):
    """Screenshots from different device viewports"""
    desktop: Optional[str] = None  # Base64 encoded desktop viewport screenshot
    tablet: Optional[str] = None   # Base64 encoded tablet viewport screenshot
    mobile: Optional[str] = None   # Base64 encoded mobile viewport screenshot


class RequestFile(BaseModel):
    """File data for AI requests"""
    name: str
    type: str
    content: str  # Base64 encoded file content


class RequestTheme(BaseModel):
    """Theme information for AI requests"""
    themeName: str


class RequestFontPack(BaseModel):
    """Font pack information"""
    name: str
    code: str  # HTML link tag code for font loading


class PageAgentRequest(BaseModel):
    """Request to generate or modify a page"""
    instruction: str
    page: Optional[Dict[str, Any]] = None  # Existing page definition
    selectedComponentKey: Optional[str] = None  # Component to focus on
    componentScreenshot: Optional[str] = None  # Base64 image for visual feedback (specific component)
    deviceScreenshots: Optional[DeviceScreenshots] = None  # Screenshots from all device viewports
    file: Optional[RequestFile] = None  # Uploaded file (non-image) as base64
    theme: Optional[RequestTheme] = None  # Theme information
    iconPacks: Optional[List[str]] = None  # List of available icon pack names
    fontPacks: Optional[List[RequestFontPack]] = None  # List of available font packs with names and loading codes
    sourceUrl: Optional[str] = None  # URL to import from (for IMPORT mode)
    clientCode: Optional[str] = None  # Client code for file uploads (passed from auth)
    options: PageAgentOptions = PageAgentOptions()

    # Session tracking fields
    sessionId: Optional[str] = None  # Existing session to continue (for multi-turn context)
    newSession: bool = False  # Force creation of new session even if sessionId provided
    pageName: Optional[str] = None  # Page name for session ID generation

    # Alias for backward compatibility
    @property
    def existingPage(self) -> Optional[Dict[str, Any]]:
        return self.page


class AgentLogEntry(BaseModel):
    """Log entry for agent execution"""
    status: str  # "success" | "failed" | "running"
    reasoning: Optional[str] = None
    errors: List[str] = []
    model: Optional[str] = None  # Track which model was used
    error: Optional[str] = None  # Single error message
    
    class Config:
        extra = "ignore"  # Ignore extra fields like startedAt/finishedAt


class TokenUsageByAgent(BaseModel):
    """Token usage for a single agent"""
    inputTokens: int = 0
    outputTokens: int = 0
    cacheReadTokens: int = 0
    cacheCreationTokens: int = 0
    model: Optional[str] = None
    latencyMs: Optional[int] = None


class TokenUsageSummary(BaseModel):
    """Aggregated token usage for a request"""
    totalInputTokens: int = 0
    totalOutputTokens: int = 0
    totalCacheReadTokens: int = 0
    totalCacheCreationTokens: int = 0
    byAgent: Dict[str, TokenUsageByAgent] = {}


class ContextUsageInfo(BaseModel):
    """Context usage information for a session"""
    used: int = 0  # Total tokens used in session so far
    limit: int = 184000  # Model's context limit
    percentage: float = 0.0  # Percentage used
    turnsInContext: int = 0  # How many turns are in active context
    warning: Optional[str] = None  # "approaching_limit" when > 80%


class PageAgentResponse(BaseModel):
    """Response from page generation"""
    success: bool
    page: Dict[str, Any]
    agentLogs: Dict[str, AgentLogEntry]
    sessionId: Optional[str] = None  # Session ID for multi-turn context
    turnNumber: Optional[int] = None  # Which turn in conversation
    tokenUsage: Optional[TokenUsageSummary] = None  # Token usage for this request
    contextUsage: Optional[ContextUsageInfo] = None  # Session context status


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
    
    # Rate limit buffer between agent calls (seconds)
    # Haiku calls are faster so need less delay
    RATE_LIMIT_DELAY_HAIKU = 2   # Between Haiku calls
    RATE_LIMIT_DELAY_SONNET = 5  # Between Sonnet calls
    
    def __init__(self):
        # Initialize all sub-agents (now with multi-model support)
        self.layout_agent = LayoutAgent()
        self.component_agent = ComponentAgent()
        self.events_agent = EventsAgent()
        self.styles_agent = StylesAgent()       # Uses Haiku
        self.animation_agent = AnimationAgent() # Uses Haiku
        self.data_agent = DataAgent()           # Uses Haiku
        self.review_agent = ReviewAgent()       # Uses Sonnet
        self.website_analyzer = WebsiteAnalyzerAgent()  # For import mode
    
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
        # Generate unique request ID for tracking
        request_id = str(uuid.uuid4())

        # Initialize tracking variables
        session_id: Optional[str] = None
        turn_number: Optional[int] = None
        token_usages: List[Dict[str, Any]] = []  # Collect token usage from all agents

        # Helper to emit progress
        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)

        # Initialize session if tracking is enabled and auth context is provided
        if settings.AI_TRACKING_ENABLED and auth_context:
            session_id, turn_number = await self._initialize_session(
                request, auth_context, request_id
            )
        
        # Check for import mode (either explicit mode, sourceUrl provided, or URL detected in instruction)
        detected_url = self._detect_url_in_instruction(request.instruction)
        
        if request.options.mode == PageAgentMode.IMPORT or request.sourceUrl or detected_url:
            # If URL detected in instruction but not in sourceUrl, use the detected one
            if not request.sourceUrl and detected_url:
                request.sourceUrl = detected_url
                logger.info(f"Auto-detected URL for import: {detected_url}")
            
            # Check if user wants EXACT copy or just inspiration
            is_exact_copy = self._is_exact_copy_request(request.instruction)
            
            if is_exact_copy:
                # Exact copy: Use 1:1 extraction with all CSS
                logger.info("Exact copy requested - using direct extraction mode")
                return await self._execute_import_mode(request, progress_callback)
            else:
                # Inspired-by: Use LLM agents with website as context
                logger.info("Inspired-by mode - using LLM agents with website reference")
                return await self._execute_inspired_by_mode(request, progress_callback)
        
        # Detect if this is a simple style-only modification
        # Skip unnecessary agents to reduce time and rate limit issues
        is_style_only = self._is_style_modification(request.instruction)
        
        if is_style_only and request.options.mode == PageAgentMode.MODIFY:
            return await self._execute_style_only(request, progress_callback)
        
        # Determine which agents are needed based on the request
        agents_needed = self._determine_agents_needed(request)
        
        await emit('status', f"Starting page generation in {request.options.mode.value} mode...")
        await emit('status', f"Agents to run: {', '.join(agents_needed)}")
        await emit('status', "Using multi-model strategy: Haiku for analysis, Sonnet for generation")
        
        agent_logs: Dict[str, AgentLogEntry] = {}
        context = self._build_context(request)
        
        # ========== Phase 1: Foundation ==========
        await emit('phase', 'Foundation (Layout + Component)')
        
        foundation_input = AgentInput(
            user_request=request.instruction,
            context=context,
            previous_outputs={}
        )
        
        # Run Layout agent (two-phase internally: Haiku analyze → Sonnet generate)
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

        # Run Component agent (two-phase internally: Haiku analyze → Sonnet generate)
        if "component" in agents_needed:
            name, component_result = await self._run_agent_with_progress(
                self.component_agent, "Component", foundation_input, progress_callback
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
        
        # Run enhancement agents SEQUENTIALLY to avoid rate limiting
        outputs = {}
        if layout_result:
            outputs["layout"] = layout_result.result
        elif request.existingPage:
            outputs["layout"] = {"rootComponent": request.existingPage.get("rootComponent")}
        
        if component_result:
            outputs["component"] = component_result.result
        
        # Events agent (two-phase: Haiku analyze → Sonnet generate with batching)
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

        # Styles agent (Haiku - fast)
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

        # Animation agent (Haiku - fast)
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

        # Data agent (Haiku - fast)
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
            if request.options.preserveEvents and "events" not in outputs:
                outputs["events"] = {"eventFunctions": request.existingPage.get("eventFunctions", {})}
            elif "events" not in outputs and "events" not in agents_needed:
                # Agent was skipped, preserve existing events
                outputs["events"] = {"eventFunctions": request.existingPage.get("eventFunctions", {})}
            
            if request.options.preserveStyles and "styles" not in outputs:
                outputs["styles"] = {"componentStyles": {}}  # Use existing styles
            elif "styles" not in outputs and "styles" not in agents_needed:
                # Agent was skipped, preserve existing styles
                existing_styles = {}
                comp_def = request.existingPage.get("componentDefinition", {})
                for comp_key, comp in comp_def.items():
                    if comp.get("styleProperties"):
                        existing_styles[comp_key] = {"rootStyle": comp.get("styleProperties", {})}
                outputs["styles"] = {"componentStyles": existing_styles}
            
            # Preserve layout if skipped
            if "layout" not in outputs and "layout" not in agents_needed:
                outputs["layout"] = {
                    "rootComponent": request.existingPage.get("rootComponent"),
                    "componentDefinition": request.existingPage.get("componentDefinition", {})
                }
            
            # Preserve component if skipped
            if "component" not in outputs and "component" not in agents_needed:
                outputs["component"] = {
                    "components": request.existingPage.get("componentDefinition", {})
                }
            
            # Preserve animation if skipped
            if "animation" not in outputs and "animation" not in agents_needed:
                outputs["animation"] = {"componentAnimations": {}}
            
            # Preserve data if skipped
            if "data" not in outputs and "data" not in agents_needed:
                outputs["data"] = {
                    "storeInitialization": request.existingPage.get("properties", {}).get("storeInitialization", {})
                }
        
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

        # Build response
        final_page = review_result.result if review_result.success else merged_page

        # ========== Phase 5: Token Tracking ==========
        # Record token usage and build usage summary
        token_usage_summary = None
        context_usage_info = None

        if settings.AI_TRACKING_ENABLED and auth_context and session_id:
            token_usage_summary, context_usage_info = await self._record_token_usage(
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
            # Show which model is being used
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
    
    def _build_context(self, request: PageAgentRequest) -> Dict[str, Any]:
        """Build context for agents based on request"""
        context = {
            "mode": request.options.mode.value,
            "hasExistingPage": request.existingPage is not None,
            "preserveEvents": request.options.preserveEvents,
            "preserveStyles": request.options.preserveStyles,
            "preserveLayout": request.options.preserveLayout
        }

        if request.existingPage:
            # For modify mode with selected component, only send relevant parts
            # This drastically reduces token count (from ~10000 to ~1000 tokens)
            if request.selectedComponentKey and request.options.mode == PageAgentMode.MODIFY:
                context["existingPage"] = self._extract_relevant_context(
                    request.existingPage,
                    request.selectedComponentKey
                )
            else:
                context["existingPage"] = request.existingPage
            context["existingComponents"] = self._extract_component_keys(request.existingPage)

        # Add selected component context for targeted modifications
        if request.selectedComponentKey:
            context["selectedComponentKey"] = request.selectedComponentKey
            # Extract the selected component definition if it exists
            if request.existingPage:
                comp_def = request.existingPage.get("componentDefinition", {})
                if request.selectedComponentKey in comp_def:
                    context["selectedComponent"] = comp_def[request.selectedComponentKey]

        # Add component screenshot for visual feedback (specific component capture)
        if request.componentScreenshot:
            context["componentScreenshot"] = request.componentScreenshot
            context["hasVisualFeedback"] = True

        # Add device screenshots for full page visual context
        # These help the AI understand the current state of the page across viewports
        if request.deviceScreenshots:
            device_shots = {}
            if request.deviceScreenshots.desktop:
                device_shots["desktop"] = request.deviceScreenshots.desktop
            if request.deviceScreenshots.tablet:
                device_shots["tablet"] = request.deviceScreenshots.tablet
            if request.deviceScreenshots.mobile:
                device_shots["mobile"] = request.deviceScreenshots.mobile

            if device_shots:
                context["deviceScreenshots"] = device_shots
                context["hasDeviceScreenshots"] = True
                logger.info(f"Device screenshots included: {list(device_shots.keys())}")

        # Add uploaded file for processing
        if request.file:
            context["uploadedFile"] = {
                "name": request.file.name,
                "type": request.file.type,
                "content": request.file.content,
            }
            context["hasUploadedFile"] = True
            logger.info(f"Uploaded file included: {request.file.name} ({request.file.type})")

        # Add theme information
        if request.theme:
            context["theme"] = {
                "themeName": request.theme.themeName,
            }
            context["hasTheme"] = True
            context["themeInstructions"] = (
                "The application uses a theme system. Theme values are accessed via 'Theme.<propertyName>' syntax. "
                "When setting styles, prefer using theme values (e.g., 'Theme.primaryColor', 'Theme.textColor') "
                "instead of hardcoded values when appropriate. "
                "You can ignore theme values if the user explicitly requests specific colors or values. "
                "Theme values provide consistency across the application."
            )
            logger.info(f"Theme included: {request.theme.themeName}")

        # Add available icon packs
        if request.iconPacks:
            context["availableIconPacks"] = request.iconPacks
            context["hasIconPacks"] = True
            logger.info(f"Available icon packs included: {len(request.iconPacks)} packs")

        # Add available font packs
        if request.fontPacks:
            # Extract font names from font packs
            fontNames = [pack.name for pack in request.fontPacks]
            context["availableFontPacks"] = [
                {"name": pack.name, "code": pack.code} for pack in request.fontPacks
            ]
            context["availableFonts"] = fontNames  # For backward compatibility
            context["hasFontPacks"] = True
            context["fontInstructions"] = (
                "When using fonts in styles, prefer fonts from the availableFontPacks list. "
                "Each font pack has a name and a code (HTML link tag) that needs to be added to the app definition for the font to load. "
                "If a required font is not in the list, you can suggest adding it to the app definition with the appropriate font pack code. "
                "Suggest font additions in your reasoning or as a separate recommendation."
            )
            logger.info(f"Available font packs included: {len(request.fontPacks)} packs")

        return context
    
    def _extract_relevant_context(self, page: Dict, selected_key: str) -> Dict:
        """
        Extract only relevant parts of the page for modification.
        Returns a minimal page structure with:
        - Selected component and its children
        - Parent chain to root
        - eventFunctions that reference selected component
        """
        comp_def = page.get("componentDefinition", {})
        
        if selected_key not in comp_def:
            return page  # Fallback to full page if component not found
        
        # Find the selected component and its children
        relevant_keys = {selected_key}
        
        # Add all children recursively
        def collect_children(key):
            comp = comp_def.get(key, {})
            for child_key in comp.get("children", {}).keys():
                if child_key in comp_def:
                    relevant_keys.add(child_key)
                    collect_children(child_key)
        
        collect_children(selected_key)
        
        # Find parent chain
        def find_parent(target_key):
            for key, comp in comp_def.items():
                if target_key in comp.get("children", {}):
                    return key
            return None
        
        parent = find_parent(selected_key)
        while parent:
            relevant_keys.add(parent)
            parent = find_parent(parent)
        
        # Build minimal component definition
        minimal_comp_def = {k: comp_def[k] for k in relevant_keys if k in comp_def}
        
        # Build minimal page with just relevant parts
        return {
            "name": page.get("name"),
            "rootComponent": page.get("rootComponent"),
            "componentDefinition": minimal_comp_def,
            # Only include eventFunctions if small enough
            "eventFunctions": page.get("eventFunctions", {}) if len(str(page.get("eventFunctions", {}))) < 2000 else {},
            "_note": f"Truncated page context focusing on '{selected_key}' and its hierarchy"
        }
    
    def _extract_component_keys(self, page: Dict) -> List[str]:
        """Extract all component keys from existing page"""
        keys = []
        
        def traverse(component):
            if isinstance(component, dict):
                if "key" in component:
                    keys.append(component["key"])
                for child in component.get("children", {}).values():
                    traverse(child)
        
        traverse(page.get("rootComponent", {}))
        return keys
    
    def _format_agent_log(self, result: AgentOutput, agent=None) -> AgentLogEntry:
        """Format agent output as log entry"""
        model = getattr(agent, 'model', None) if agent else None
        return AgentLogEntry(
            status="success" if result.success else "error",
            reasoning=result.reasoning,
            errors=result.errors,
            model=model
        )

    async def _initialize_session(
        self,
        request: PageAgentRequest,
        auth_context: Dict[str, Any],
        request_id: str
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        Initialize or retrieve a session for tracking.

        Returns:
            Tuple of (session_id, turn_number)
        """
        from app.services.session_manager import get_session_manager

        session_manager = get_session_manager()

        # Check if we should use an existing session or create a new one
        if request.sessionId and not request.newSession:
            # Try to use existing session
            existing_session = await session_manager.get_session(request.sessionId)
            if existing_session:
                # Increment turn count and get new turn number
                turn_number = await session_manager.increment_turn_count(
                    request.sessionId,
                    auth_context.get("userId")
                )
                logger.info(f"Continuing session {request.sessionId}, turn {turn_number}")
                return request.sessionId, turn_number

        # Create new session
        session = await session_manager.create_session(
            client_code=auth_context.get("clientCode", ""),
            client_id=auth_context.get("clientId", 0),
            user_id=auth_context.get("userId", 0),
            object_name=request.pageName,
            agent_name="PageAgent",
            app_code=auth_context.get("appCode")
        )

        if session:
            logger.info(f"Created new session: {session.session_id}")
            return session.session_id, 1  # First turn
        else:
            logger.warning("Failed to create session, tracking disabled for this request")
            return None, None

    async def _record_token_usage(
        self,
        session_id: str,
        request_id: str,
        auth_context: Dict[str, Any],
        token_usages: List[Dict[str, Any]],
        instruction: str,
        final_page: Dict[str, Any]
    ) -> Tuple[Optional[TokenUsageSummary], Optional[ContextUsageInfo]]:
        """
        Record token usage to database and return usage summaries.

        Returns:
            Tuple of (TokenUsageSummary, ContextUsageInfo)
        """
        from app.services.token_tracker import get_token_tracker
        from app.services.context_manager import get_context_manager
        from app.services.session_manager import get_session_manager
        from app.db.models import AiTokenUsageCreate

        token_tracker = get_token_tracker()
        context_manager = get_context_manager()
        session_manager = get_session_manager()

        # Build token usage records for database
        usage_records = []
        by_agent: Dict[str, TokenUsageByAgent] = {}

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_creation = 0

        for usage in token_usages:
            agent_type = usage.get("agent_type", "Unknown")
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_tokens", 0)
            cache_creation = usage.get("cache_creation_tokens", 0)

            # Aggregate totals
            total_input += input_tokens
            total_output += output_tokens
            total_cache_read += cache_read
            total_cache_creation += cache_creation

            # Build by-agent breakdown
            by_agent[agent_type] = TokenUsageByAgent(
                inputTokens=input_tokens,
                outputTokens=output_tokens,
                cacheReadTokens=cache_read,
                cacheCreationTokens=cache_creation,
                model=usage.get("model"),
                latencyMs=usage.get("latency_ms")
            )

            # Create database record
            usage_records.append(AiTokenUsageCreate(
                session_id=session_id,
                request_id=request_id,
                client_code=auth_context.get("clientCode", ""),
                client_id=auth_context.get("clientId", 0),
                user_id=auth_context.get("userId", 0),
                agent_type=agent_type,
                model=usage.get("model", "unknown"),
                llm_provider=settings.LLM_PROVIDER,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                latency_ms=usage.get("latency_ms"),
                success=usage.get("success", True),
                error_message=usage.get("error_message")
            ))

        # Record usage to database in batch
        if usage_records:
            await token_tracker.record_usage_batch(usage_records, update_session=True)

        # Add conversation turn to history
        session = await session_manager.get_session(session_id)
        if session:
            # Create a summary of what was generated
            summary = f"Generated page with {len(final_page.get('componentDefinition', {}))} components"

            await context_manager.add_turn(
                session_id=session_id,
                request_id=request_id,
                turn_number=session.turn_count,
                user_instruction=instruction,
                assistant_summary=summary,
                page_snapshot=json.dumps(final_page) if final_page else None
            )

        # Build token usage summary
        token_usage_summary = TokenUsageSummary(
            totalInputTokens=total_input,
            totalOutputTokens=total_output,
            totalCacheReadTokens=total_cache_read,
            totalCacheCreationTokens=total_cache_creation,
            byAgent=by_agent
        )

        # Get context usage info
        context_usage_info = None
        if session:
            context_usage = await context_manager.get_context_usage(session_id)
            if context_usage:
                # ContextUsage model uses 'used' and 'limit' fields
                context_usage_info = ContextUsageInfo(
                    used=context_usage.used,
                    limit=context_usage.limit,
                    percentage=context_usage.percentage,
                    turnsInContext=context_usage.turns_in_context,
                    warning=context_usage.warning
                )

        return token_usage_summary, context_usage_info

    def _determine_agents_needed(self, request: PageAgentRequest) -> List[str]:
        """
        Determine which agents are needed based on the request.
        Returns a list of agent names to run.

        IMPORTANT: Styles agent always runs as it's core to any modification.
        """
        instruction_lower = request.instruction.lower()
        mode = request.options.mode

        # Always need these for CREATE mode
        if mode == PageAgentMode.CREATE:
            return ["layout", "component", "events", "styles", "animation", "data", "review"]

        # For MODIFY mode, analyze what's needed
        agents = []

        # Layout keywords
        layout_keywords = ['layout', 'structure', 'arrange', 'organize', 'grid', 'row', 'column',
                          'header', 'footer', 'sidebar', 'section', 'container']
        if any(kw in instruction_lower for kw in layout_keywords) and not request.options.preserveLayout:
            agents.append("layout")

        # Component keywords
        component_keywords = ['component', 'button', 'input', 'textbox', 'form', 'add', 'create',
                             'remove', 'delete', 'element', 'field', 'label']
        if any(kw in instruction_lower for kw in component_keywords):
            agents.append("component")

        # Events keywords
        events_keywords = ['click', 'event', 'action', 'function', 'handler', 'onclick', 'trigger',
                          'navigate', 'submit', 'send', 'fetch', 'api', 'call', 'interaction']
        if any(kw in instruction_lower for kw in events_keywords) and not request.options.preserveEvents:
            agents.append("events")

        # Animation keywords
        animation_keywords = ['animate', 'animation', 'transition', 'fade', 'slide', 'hover',
                             'effect', 'motion', 'smooth', 'bounce', 'pulse']
        if any(kw in instruction_lower for kw in animation_keywords):
            agents.append("animation")

        # Data keywords
        data_keywords = ['data', 'bind', 'store', 'value', 'state', 'variable', 'binding',
                        'form data', 'save', 'load', 'fetch', 'api']
        if any(kw in instruction_lower for kw in data_keywords):
            agents.append("data")

        # If no specific agents detected, run component + styles (safe default for modifications)
        if not agents:
            logger.info("No specific agents detected, running component + styles")
            agents = ["component"]

        # ALWAYS include styles - it's core to any modification
        if "styles" not in agents and not request.options.preserveStyles:
            agents.append("styles")

        # Always include review for validation
        agents.append("review")

        logger.info(f"Determined agents needed: {agents}")
        return agents
    
    def _is_exact_copy_request(self, instruction: str) -> bool:
        """
        Detect if the user wants an EXACT copy of the website.
        
        Returns True only for explicit "exact copy" requests.
        For "inspired by", "similar to", "looking like but different" → returns False.
        """
        instruction_lower = instruction.lower()
        
        # Keywords that indicate user does NOT want exact copy
        not_exact_keywords = [
            "not exact", "don't copy", "do not copy", "don't replicate", "not replicate",
            "similar to", "looking like", "inspired by", "like this but", "something like",
            "based on", "use as reference", "for inspiration", "take inspiration",
            "not the same", "different from", "my own", "customize", "personalize",
            "change the", "modify", "adapt", "similar style", "similar design",
            "don't use exact", "not one to one", "not 1:1", "not 1 to 1",
            "but different", "but not same", "but unique", "but custom",
            "just the style", "just the layout", "only the design", "only the look",
            "use their style", "copy the style", "match the vibe", "same vibe",
            "about me", "about us", "my details", "my info", "my content"
        ]
        
        # Check if any "not exact" keyword is present
        for keyword in not_exact_keywords:
            if keyword in instruction_lower:
                logger.info(f"Detected 'inspired-by' intent due to: '{keyword}'")
                return False
        
        # Keywords that indicate user DOES want exact copy
        exact_keywords = [
            "exact copy", "exact replica", "exact same", "exactly like", "exactly the same",
            "clone", "replicate exactly", "copy exactly", "1:1 copy", "one to one",
            "carbon copy", "duplicate", "mirror", "identical", "pixel perfect",
            "same as", "copy this", "import this", "recreate this exactly"
        ]
        
        # Check if any "exact" keyword is present
        for keyword in exact_keywords:
            if keyword in instruction_lower:
                logger.info(f"Detected 'exact copy' intent due to: '{keyword}'")
                return True
        
        # Default: If just a URL is given without context, assume inspired-by (safer default)
        logger.info("No explicit copy intent detected, defaulting to 'inspired-by' mode")
        return False
    
    def _wants_dark_theme(self, instruction: str) -> bool:
        """
        Detect if the user wants a dark theme for their page.
        
        Defaults to light theme unless user explicitly asks for dark.
        """
        instruction_lower = instruction.lower()
        
        dark_keywords = [
            "dark theme", "dark mode", "dark background", "dark design",
            "dark color", "black background", "dark style", "night mode",
            "dark ui", "dark look", "keep dark", "same dark", "dark palette"
        ]
        
        for keyword in dark_keywords:
            if keyword in instruction_lower:
                logger.info(f"User wants dark theme due to: '{keyword}'")
                return True
        
        # Default to light theme
        return False
    
    def _detect_url_in_instruction(self, instruction: str) -> Optional[str]:
        """
        Detect if the instruction contains a URL that suggests website import.
        
        Returns the best URL for import (prioritizes design reference URLs over social media).
        Ignores localhost and internal URLs.
        """
        # Pattern to match HTTP/HTTPS URLs
        url_pattern = r'https?://[^\s<>"\')\]]+(?:\.[^\s<>"\')\]]+)+'
        
        matches = re.findall(url_pattern, instruction, re.IGNORECASE)
        
        valid_urls = []
        social_media_urls = []
        
        for url in matches:
            # Clean up trailing punctuation
            url = url.rstrip('.,;:!?')
            
            # Skip localhost and internal URLs
            if any(skip in url.lower() for skip in ['localhost', '127.0.0.1', '0.0.0.0']):
                continue
            
            # Skip common non-webpage URLs
            if any(skip in url.lower() for skip in ['.pdf', '.zip', '.tar', '.gz', '.exe', '.dmg']):
                continue
            
            # Categorize: social media URLs are lower priority for import
            # (LinkedIn, Twitter, Facebook, etc. - usually for data, not design)
            if any(social in url.lower() for social in ['linkedin.com', 'twitter.com', 'facebook.com', 'instagram.com', 'github.com']):
                social_media_urls.append(url)
            else:
                valid_urls.append(url)
        
        # Prefer non-social URLs as design references
        if valid_urls:
            logger.info(f"Detected design URL for import: {valid_urls[0]}")
            return valid_urls[0]
        elif social_media_urls:
            logger.info(f"Detected social media URL for import (may require auth): {social_media_urls[0]}")
            return social_media_urls[0]
        
        return None
    
    def _is_style_modification(self, instruction: str) -> bool:
        """
        Detect if the instruction is primarily about styling.
        Returns True for instructions that only need Styles/Animation agents.
        
        Very conservative - only returns True for clear style-only requests.
        When in doubt, run full pipeline.
        """
        instruction_lower = instruction.lower()
        
        # Keywords that indicate style-only changes
        style_keywords = [
            'color', 'background', 'font', 'size', 'padding', 'margin',
            'border', 'shadow', 'prominent', 'bigger', 'smaller', 'larger',
            'bold', 'italic', 'opacity', 'transparent', 'dark', 'light',
            'bright', 'muted', 'spacing', 'align', 'rounded',
            'gradient', 'hover', 'fade', 'slide',
            'appearance', 'visual', 'theme',
            'highlight', 'emphasize', 'stand out', 'pop', 'subtle'
        ]
        
        # Keywords that indicate structural/functional changes (NOT style-only)
        # This list should be comprehensive to avoid false positives
        structural_keywords = [
            # Component creation/modification
            'add', 'remove', 'delete', 'create', 'insert', 'move', 'build', 'make',
            # Component types
            'button', 'form', 'input', 'textbox', 'dropdown', 'checkbox', 'radio',
            'text', 'image', 'icon', 'link', 'grid', 'layout', 'table', 'list',
            # Structural
            'component', 'element', 'section', 'page', 'container', 'wrapper',
            'header', 'footer', 'sidebar', 'navbar', 'menu',
            # Functional
            'click', 'event', 'action', 'function', 'handler', 'trigger',
            'navigate', 'submit', 'send', 'fetch', 'api', 'call',
            'data', 'bind', 'store', 'value', 'state',
            # Common request patterns
            'calculator', 'login', 'signup', 'register', 'counter', 'todo',
            'with all', 'that are required', 'need to', 'should have',
            # Verbs that imply creation
            'generate', 'implement', 'develop', 'setup', 'configure'
        ]
        
        has_style_keyword = any(kw in instruction_lower for kw in style_keywords)
        has_structural_keyword = any(kw in instruction_lower for kw in structural_keywords)
        
        # Style-only if has style keywords but NO structural keywords
        # Also check instruction length - short instructions are more likely style-only
        is_short_instruction = len(instruction_lower.split()) <= 8
        
        # Only use fast mode for:
        # 1. Short instructions (<=8 words)
        # 2. WITH style keywords
        # 3. WITHOUT structural keywords
        if has_structural_keyword:
            logger.debug(f"[_is_style_modification] Found structural keyword, running full pipeline")
            return False
        
        if has_style_keyword and is_short_instruction:
            logger.info(f"[_is_style_modification] Detected style-only: short instruction with style keywords")
            return True
        
        # When in doubt, run full pipeline
        logger.debug(f"[_is_style_modification] Uncertain, running full pipeline")
        return False
    
    async def _execute_style_only(
        self,
        request: PageAgentRequest,
        progress_callback: Optional[ProgressCallback] = None
    ) -> PageAgentResponse:
        """
        Fast path for style-only modifications.
        Only runs Styles and Animation agents (both use Haiku - fast and cheap).
        """
        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)
        
        await emit('status', "Detected style modification - using fast mode (Haiku only)...")
        
        agent_logs: Dict[str, AgentLogEntry] = {}
        context = self._build_context(request)
        outputs = {}
        
        # Preserve existing layout and components
        if request.existingPage:
            outputs["layout"] = {"rootComponent": request.existingPage.get("rootComponent")}
        
        enhancement_input = AgentInput(
            user_request=request.instruction,
            context=context,
            previous_outputs={}
        )
        
        # ========== Only run Styles + Animation (both Haiku) ==========
        await emit('phase', 'Styling (Fast Mode)')
        
        # Styles agent (Haiku - fast)
        name, styles_result = await self._run_agent_with_progress(
            self.styles_agent, "Styles", enhancement_input, progress_callback
        )
        agent_logs[name.lower()] = self._format_agent_log(styles_result, self.styles_agent)
        outputs["styles"] = styles_result.result
        
        # Animation agent (Haiku - fast, shorter delay)
        await self._sleep_with_keepalive(
            self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Animation..."
        )
        name, animation_result = await self._run_agent_with_progress(
            self.animation_agent, "Animation", enhancement_input, progress_callback
        )
        agent_logs[name.lower()] = self._format_agent_log(animation_result, self.animation_agent)
        outputs["animation"] = animation_result.result
        
        # ========== Merge ==========
        await emit('merging', 'Applying style changes...')
        
        # Debug: Log what agents returned
        logger.info(f"[FastMode] Styles output keys: {list(outputs.get('styles', {}).keys())}")
        if 'componentStyles' in outputs.get('styles', {}):
            logger.info(f"[FastMode] componentStyles targets: {list(outputs['styles']['componentStyles'].keys())}")
        else:
            logger.warning(f"[FastMode] No componentStyles in styles output: {outputs.get('styles', {})}")
        
        logger.info(f"[FastMode] Animation output keys: {list(outputs.get('animation', {}).keys())}")
        
        merged_page = merge_agent_outputs(outputs, request.existingPage)
        
        # Skip review for style-only changes to save time
        return PageAgentResponse(
            success=all(log.status == "success" for log in agent_logs.values()),
            page=merged_page,
            agentLogs=agent_logs
        )
    
    async def _execute_inspired_by_mode(
        self,
        request: PageAgentRequest,
        progress_callback: Optional[ProgressCallback] = None
    ) -> PageAgentResponse:
        """
        Inspired-by mode: Use a website as REFERENCE but generate unique content.
        
        This uses the normal LLM agent flow (layout, components, styles) but provides
        a screenshot of the reference website as visual inspiration.
        
        Key difference from import mode:
        - Import mode: 1:1 extraction with exact CSS
        - Inspired-by mode: LLM generates content based on user's request, using website as style reference
        """
        from app.services.website_extractor import get_website_extractor
        
        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)
        
        await emit('status', "Inspired-by mode: Using reference website for style inspiration...")
        
        agent_logs: Dict[str, AgentLogEntry] = {}
        source_url = request.sourceUrl
        
        try:
            # Take a screenshot of the reference website for visual context
            extractor = get_website_extractor()
            await emit('status', f"Capturing reference screenshot from {source_url}...")
            
            try:
                visual_data = await extractor.extract(source_url)
                screenshot_base64 = visual_data.screenshot if visual_data else None
                
                # Detect if user wants dark theme (default to light for personal portfolios)
                user_wants_dark = self._wants_dark_theme(request.instruction)
                
                # Extract color palette from the website
                extracted_colors = self._extract_color_palette(visual_data) if visual_data else []
                
                # For inspired-by mode, default to light theme unless explicitly requested
                if not user_wants_dark:
                    # Override with a clean light theme palette
                    style_hints = {
                        "referenceUrl": source_url,
                        "hasScreenshot": screenshot_base64 is not None,
                        "rootStyles": {},  # Don't copy dark root styles
                        "colorPalette": extracted_colors,
                        "theme": "light",
                        "backgroundColor": "#ffffff",
                        "textColor": "#1a1a1a",
                        "overrideTheme": True  # Signal to use our theme, not extracted
                    }
                else:
                    style_hints = {
                        "referenceUrl": source_url,
                        "hasScreenshot": screenshot_base64 is not None,
                        "rootStyles": visual_data.root_styles if visual_data else {},
                        "colorPalette": extracted_colors,
                        "theme": "dark"
                    }
                
                await emit('status', "Reference captured. Generating your page with similar style...")
                
            except Exception as e:
                logger.warning(f"Could not capture reference website: {e}")
                screenshot_base64 = None
                style_hints = {"referenceUrl": source_url, "error": str(e)}
                await emit('status', "Could not capture reference, proceeding with text description...")
            
            finally:
                await extractor.close()
            
            # Add the reference context to the request
            enhanced_instruction = f"""
{request.instruction}

STYLE REFERENCE: The user provided {source_url} as a style reference.
Use this as INSPIRATION for the visual design (colors, layout, typography) but do NOT copy the content.
Generate UNIQUE content based on the user's request about "{request.instruction.split()[0:10]}...".
The user wants a SIMILAR LOOK, not an exact copy.
"""
            
            # Create modified request (without context - context goes to AgentInput)
            modified_request = PageAgentRequest(
                instruction=enhanced_instruction,
                existingPage=request.existingPage,
                options=request.options,
                sourceUrl=None  # Clear sourceUrl to prevent re-triggering import mode
            )
            
            # Build context for agent inputs
            agent_context = {}
            if screenshot_base64:
                agent_context = {
                    "referenceScreenshot": screenshot_base64,
                    "styleHints": style_hints
                }
            
            # Run normal agent flow with the enhanced context
            await emit('status', "Running layout agent with style reference...")
            
            # Use the standard execution flow
            agents_needed = self._determine_agents_needed(modified_request)
            outputs: Dict[str, Any] = {}
            
            # Layout Agent
            if "layout" in agents_needed:
                await emit('status', "Layout agent analyzing structure...")
                try:
                    layout_result = await self.layout_agent.execute(
                        AgentInput(user_request=enhanced_instruction, context=agent_context)
                    )
                    outputs["layout"] = layout_result.result
                    agent_logs["layout"] = AgentLogEntry(
                        status="success" if layout_result.success else "failed",
                        reasoning=layout_result.reasoning,
                        errors=layout_result.errors
                    )
                except Exception as e:
                    agent_logs["layout"] = AgentLogEntry(status="failed", error=str(e))
            
            # Component Agent
            if "component" in agents_needed:
                await emit('status', "Component agent creating structure...")
                try:
                    comp_result = await self.component_agent.execute(
                        AgentInput(
                            user_request=enhanced_instruction,
                            context=agent_context,
                            previous_outputs={"layout": outputs.get("layout", {})}
                        )
                    )
                    outputs["component"] = comp_result.result
                    agent_logs["component"] = AgentLogEntry(
                        status="success" if comp_result.success else "failed",
                        reasoning=comp_result.reasoning,
                        errors=comp_result.errors
                    )
                except Exception as e:
                    agent_logs["component"] = AgentLogEntry(status="failed", error=str(e))
            
            # Styles Agent - pass the style hints
            if "styles" in agents_needed:
                await emit('status', "Styles agent applying reference styling...")
                try:
                    style_context = dict(agent_context)  # Copy the context
                    style_context["styleHints"] = style_hints
                    style_context["importMode"] = False  # Not import mode
                    
                    style_result = await self.styles_agent.execute(
                        AgentInput(
                            user_request=enhanced_instruction,
                            context=style_context,
                            previous_outputs={
                                "layout": outputs.get("layout", {}),
                                "component": outputs.get("component", {})
                            }
                        )
                    )
                    outputs["styles"] = style_result.result
                    agent_logs["styles"] = AgentLogEntry(
                        status="success" if style_result.success else "failed",
                        reasoning=style_result.reasoning,
                        errors=style_result.errors
                    )
                except Exception as e:
                    agent_logs["styles"] = AgentLogEntry(status="failed", error=str(e))
            
            # Events, Animation, Data agents as needed
            for agent_name in ["events", "animation", "data"]:
                if agent_name in agents_needed:
                    await emit('status', f"Running {agent_name} agent...")
                    agent = getattr(self, f"{agent_name}_agent")
                    try:
                        result = await agent.execute(
                            AgentInput(
                                user_request=enhanced_instruction,
                                context=agent_context,
                                previous_outputs=outputs
                            )
                        )
                        outputs[agent_name] = result.result
                        agent_logs[agent_name] = AgentLogEntry(
                            status="success" if result.success else "failed",
                            reasoning=result.reasoning,
                            errors=result.errors
                        )
                    except Exception as e:
                        agent_logs[agent_name] = AgentLogEntry(status="failed", error=str(e))
            
            # Merge outputs
            from app.utils.merge import merge_agent_outputs
            merged_page = merge_agent_outputs(outputs, request.existingPage)
            
            await emit('status', "Inspired-by page generation complete!")
            
            return PageAgentResponse(
                success=all(log.status == "success" for log in agent_logs.values()),
                page=merged_page,
                agentLogs=agent_logs
            )
            
        except Exception as e:
            logger.error(f"Inspired-by mode failed: {e}", exc_info=True)
            await emit('status', f"Error: {str(e)}")
            return PageAgentResponse(
                success=False,
                page={},
                agentLogs={"error": AgentLogEntry(status="failed", error=str(e))}
            )
    
    def _extract_color_palette(self, visual_data) -> List[str]:
        """Extract dominant colors from the visual data for style reference."""
        colors = set()
        
        # Get colors from root styles
        if visual_data and visual_data.root_styles:
            for key, value in visual_data.root_styles.items():
                if isinstance(value, str) and ('rgb' in value.lower() or '#' in value):
                    colors.add(value)
        
        # Get colors from first few elements
        if visual_data and visual_data.elements:
            for elem in visual_data.elements[:5]:
                if hasattr(elem, 'styles') and elem.styles:
                    desktop = elem.styles.get('desktop', {})
                    for key in ['backgroundColor', 'color', 'borderColor']:
                        if key in desktop and desktop[key]:
                            colors.add(desktop[key])
        
        return list(colors)[:10]  # Return top 10 colors
    
    async def _execute_import_mode(
        self,
        request: PageAgentRequest,
        progress_callback: Optional[ProgressCallback] = None
    ) -> PageAgentResponse:
        """
        Import mode: Extract and convert a website URL to Nocode page (EXACT COPY).
        
        Uses multi-viewport visual extraction:
        1. Extract visual data at desktop, tablet, and mobile viewports
        2. Upload images to files service
        3. Direct 1:1 conversion of HTML elements to Nocode components
        4. Apply ALL extracted CSS values
        
        This is for EXACT replication only. For inspiration, use _execute_inspired_by_mode.
        """
        from app.services.website_extractor import get_website_extractor, VisualData
        from app.services.image_uploader import get_image_uploader
        from app.config import settings
        
        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)
        
        if not request.sourceUrl:
            raise ValueError("sourceUrl is required for import mode")
        
        await emit('status', f"Starting website import from {request.sourceUrl}...")
        
        agent_logs: Dict[str, AgentLogEntry] = {}
        
        # ========== Phase 1: Multi-Viewport Extraction ==========
        await emit('phase', 'Visual Extraction')
        await emit('status', "Extracting visual data at multiple viewport sizes...")
        
        try:
            extractor = get_website_extractor()
            visual_data: VisualData = await extractor.extract(request.sourceUrl)
            
            await emit('status', f"Extracted: {len(visual_data.elements)} elements, {len(visual_data.images)} images")
            
            agent_logs["extraction"] = AgentLogEntry(
                status="success",
                reasoning=f"Extracted {len(visual_data.elements)} elements at 3 viewports"
            )
            
        except Exception as e:
            logger.error(f"Website extraction failed: {e}")
            return PageAgentResponse(
                success=False,
                page={},
                agentLogs={
                    "extraction": AgentLogEntry(
                        status="error",
                        errors=[f"Failed to extract website: {str(e)}"]
                    )
                }
            )
        
        # ========== Phase 2: Image Upload ==========
        uploaded_images = {}
        client_code = request.clientCode
        
        if client_code and visual_data.images:
            await emit('phase', 'Image Upload')
            await emit('status', f"Uploading {len(visual_data.images)} images...")
            
            try:
                uploader = get_image_uploader()
                
                for i, img in enumerate(visual_data.images):
                    try:
                        await emit('status', f"Uploading image {i+1}/{len(visual_data.images)}...")
                        new_url = await uploader.download_and_upload(img.url, client_code)
                        uploaded_images[img.url] = new_url
                    except Exception as e:
                        logger.warning(f"Failed to upload image {img.url}: {e}")
                        uploaded_images[img.url] = settings.PLACEHOLDER_IMAGE_PATH
                
                successful = sum(1 for v in uploaded_images.values() if v != settings.PLACEHOLDER_IMAGE_PATH)
                await emit('status', f"Uploaded {successful}/{len(visual_data.images)} images")
                
                agent_logs["image_upload"] = AgentLogEntry(
                    status="success",
                    reasoning=f"Uploaded {successful} images"
                )
                
            except Exception as e:
                logger.error(f"Image upload phase failed: {e}")
                agent_logs["image_upload"] = AgentLogEntry(
                    status="error",
                    errors=[str(e)]
                )
        
        # ========== Phase 3: Direct Conversion (No LLM) ==========
        await emit('phase', 'Direct Conversion')
        await emit('status', "Converting elements to Nocode components...")
        
        try:
            page_def = self._convert_visual_to_nocode(
                visual_data,
                uploaded_images
            )
            
            component_count = len(page_def.get("componentDefinition", {}))
            await emit('status', f"Created {component_count} components")
            
            agent_logs["conversion"] = AgentLogEntry(
                status="success",
                reasoning=f"Directly converted {len(visual_data.elements)} elements to {component_count} components"
            )
            
        except Exception as e:
            logger.error(f"Direct conversion failed: {e}")
            return PageAgentResponse(
                success=False,
                page={},
                agentLogs={
                    **agent_logs,
                    "conversion": AgentLogEntry(
                        status="error",
                        errors=[f"Failed to convert: {str(e)}"]
                    )
                }
            )
        
        # ========== Phase 5: Finalization ==========
        await emit('phase', 'Finalization')
        
        # Preserve existing page name - don't overwrite with website title
        # Only set name if there's no existing page
        if request.existingPage and request.existingPage.get("name"):
            page_def["name"] = request.existingPage["name"]
        # Otherwise, don't set name at all - let the API handle it
        
        if "properties" not in page_def:
            page_def["properties"] = {}
        
        logger.info(f"Import complete: {component_count} components from {request.sourceUrl}")
        
        await emit('status', 'Import complete!')
        
        return PageAgentResponse(
            success=all(log.status == "success" for log in agent_logs.values()),
            page=page_def,
            agentLogs=agent_logs
        )
    
    def _convert_visual_to_nocode(
        self,
        visual_data,
        uploaded_images: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Directly convert VisualElement tree to Nocode componentDefinition.
        No LLM involved - pure 1:1 mapping of elements to components.
        
        Key features:
        - Adds displayOrder to each component for correct ordering
        - Properly maps image URLs to uploaded versions
        - Maintains full element tree structure
        """
        component_def = {}
        root_children = {}
        
        # Create root component
        root_key = "pageRoot"
        component_def[root_key] = {
            "key": root_key,
            "name": "Page Root",
            "type": "Grid",
            "displayOrder": 0,
            "properties": {},
            "styleProperties": self._create_responsive_styles(
                visual_data.root_styles,
                default_styles={
                    "minHeight": "100vh",
                    "display": "flex",
                    "flexDirection": "column",
                    "margin": "0",
                    "padding": "0"
                }
            ),
            "override": False,
            "children": root_children
        }
        
        # Counter for unique keys and display order
        key_counter = [0]
        
        # Image tracking for debugging
        images_converted = []
        
        def generate_key(base: str) -> str:
            key_counter[0] += 1
            clean = re.sub(r'[^a-zA-Z0-9]', '', base)[:20]
            return f"{clean}_{key_counter[0]}"
        
        def convert_element(elem, parent_children: Dict, display_order: int) -> None:
            """Recursively convert a VisualElement to Nocode component(s)"""
            tag = elem.tag.lower()
            has_children = len(elem.children) > 0

            # Check if children are text-only (for button handling)
            # Text-only means all children are text elements (span, strong, em, b, i, p, etc.)
            TEXT_TAGS = {'span', 'strong', 'em', 'b', 'i', 'p', 'text', 'label'}
            children_are_text_only = all(
                child.tag.lower() in TEXT_TAGS for child in elem.children
            ) if has_children else True

            # Determine component type - <a> with children uses Grid with linkPath
            # <button> with complex children uses Grid
            comp_type = self._tag_to_component_type(tag, has_children, children_are_text_only)

            # Generate unique key
            elem_key = generate_key(elem.id or tag)

            # Build properties based on type
            properties = {}

            # Track if we need to add a text child for <a> elements
            needs_text_child = False
            text_for_child = ""

            # Handle <a> with children -> Grid with linkPath
            if tag == "a" and has_children:
                href = elem.attributes.get("href", "")
                if href:
                    properties["linkPath"] = {"value": href}
                # If <a> has direct text content, we need to add it as a Text child
                if elem.text and elem.text.strip():
                    needs_text_child = True
                    text_for_child = elem.text.strip()
            elif tag == "a" and not has_children:
                # <a> without children -> Link component
                # This case is handled by comp_type = "Link"
                pass

            if comp_type == "Text":
                properties["text"] = {"value": elem.text or ""}
            elif comp_type == "Button":
                properties["label"] = {"value": elem.text or "Button"}
                # Use neutral design to avoid default green/teal color
                # _text design + _secondary color = transparent background
                properties["designType"] = {"value": "_text"}
                properties["colorScheme"] = {"value": "_secondary"}
            elif comp_type == "Link":
                properties["label"] = {"value": elem.text or ""}
                properties["linkPath"] = {"value": elem.attributes.get("href", "")}
            elif comp_type == "Image":
                original_src = elem.image_url or elem.attributes.get("src", "")

                # Handle SVG data URIs (inline SVGs converted to base64)
                if original_src.startswith("data:image/svg+xml"):
                    # Use the data URI directly - no upload needed
                    src = original_src
                    logger.info(f"Inline SVG as data URI: {elem_key}")
                else:
                    # Map to uploaded URL or use original
                    src = uploaded_images.get(original_src, original_src)

                properties["src"] = {"value": src}
                properties["alt"] = {"value": elem.attributes.get("alt", "")}
                # Track for logging
                images_converted.append({
                    "key": elem_key,
                    "original": original_src[:50] if original_src else "EMPTY",
                    "mapped_to": src[:50] if src else "EMPTY"
                })
                logger.info(f"Image component: {elem_key}, src: {src[:60] if src else 'EMPTY'}")
            elif comp_type == "TextBox":
                properties["placeholder"] = {"value": elem.attributes.get("placeholder", "")}

            # Build responsive styles with sub-component support
            style_props = self._build_element_styles(elem, comp_type)

            # Create component with displayOrder and override flag
            component = {
                "key": elem_key,
                "name": elem_key.replace("_", " ").title(),
                "type": comp_type,
                "displayOrder": display_order,
                "properties": properties,
                "styleProperties": style_props,
                "override": False,
                "children": {}
            }

            parent_children[elem_key] = True
            component_def[elem_key] = component

            # Track child display order
            child_display_order = 0

            # For <a> with text content, add a Text child first (before other children)
            if needs_text_child and text_for_child:
                text_child_key = generate_key(f"{elem_key}_text")
                text_child = {
                    "key": text_child_key,
                    "name": f"{elem_key.replace('_', ' ').title()} Text",
                    "type": "Text",
                    "displayOrder": child_display_order,
                    "properties": {
                        "text": {"value": text_for_child}
                    },
                    "styleProperties": {},  # Inherit styles from parent or use defaults
                    "override": False,
                    "children": {}
                }
                component["children"][text_child_key] = True
                component_def[text_child_key] = text_child
                child_display_order += 1
                logger.info(f"Added Text child '{text_child_key}' to <a> element '{elem_key}' with text: {text_for_child[:30]}...")

            # Convert children recursively with incrementing display order
            for child in elem.children:
                convert_element(child, component["children"], child_display_order)
                child_display_order += 1
        
        # Convert all top-level elements with display order
        for idx, elem in enumerate(visual_data.elements):
            convert_element(elem, root_children, idx)
        
        # Log summary
        logger.info(f"Converted {len(component_def)} components, {len(images_converted)} images")
        for img in images_converted[:5]:
            logger.info(f"  Image: {img['key']} -> {img['mapped_to']}")
        if len(images_converted) > 5:
            logger.info(f"  ... and {len(images_converted) - 5} more images")
        
        return {
            "rootComponent": root_key,
            "componentDefinition": component_def
        }
    
    def _tag_to_component_type(self, tag: str, has_children: bool = False, children_are_text_only: bool = True) -> str:
        """
        Map HTML tag to Nocode component type - simple 1:1 mapping.

        Special cases:
        - <a> with children uses Grid with linkPath (generates <a><div>...</div></a>)
        - <button> with complex (non-text) children uses Grid with label property
        """
        tag_lower = tag.lower()

        # <a> with children -> Grid with linkPath property
        if tag_lower == "a" and has_children:
            return "Grid"  # Will add linkPath property

        # <button> with complex children (not just text) -> Grid
        # Button component can only have a label, not nested elements
        if tag_lower == "button" and has_children and not children_are_text_only:
            return "Grid"  # Will still use styles from button

        tag_map = {
            # Text elements
            "h1": "Text", "h2": "Text", "h3": "Text", "h4": "Text", "h5": "Text", "h6": "Text",
            "p": "Text", "span": "Text", "label": "Text", "li": "Text",
            "strong": "Text", "b": "Text", "em": "Text", "i": "Text",
            # Interactive
            "button": "Button",
            "a": "Link",  # <a> without children
            # Media
            "img": "Image",
            "svg": "Image",  # SVG as Image with data URI
            "path": "Grid",  # SVG path child (usually inside svg)
            # Form - input uses TextBox, textarea uses TextArea
            "input": "TextBox",
            "textarea": "TextArea",
            "select": "Dropdown",
            # Containers - everything else is Grid
            "div": "Grid", "section": "Grid", "article": "Grid", "main": "Grid",
            "header": "Grid", "footer": "Grid", "nav": "Grid", "aside": "Grid",
            "form": "Grid", "ul": "Grid", "ol": "Grid", "figure": "Grid",
        }
        return tag_map.get(tag_lower, "Grid")
    
    def _build_element_styles(self, elem, comp_type: str) -> Dict[str, Any]:
        """
        Build responsive styles from extracted CSS at all viewports.

        For Image components: uses `image-` prefix for img element styles.
        Container styles (position, margin, etc.) go without prefix.
        Image-specific styles (width, height, objectFit) need `image-` prefix.

        IMPORTANT: styleProperties uses unique IDs as keys, not fixed names.
        """
        style_props = {}

        # Get styles for all viewports
        desktop = elem.styles.get("desktop", {})
        tablet = elem.styles.get("tablet", {})
        mobile = elem.styles.get("mobile", {})

        # Properties that apply to the container (for all component types)
        # For Image components, width/height control the container size (NOT image- prefixed)
        CONTAINER_PROPS = {
            "position", "top", "right", "bottom", "left", "zIndex",
            "margin", "marginTop", "marginRight", "marginBottom", "marginLeft",
            "display", "opacity", "transform", "visibility",
            # Sizing props stay on container for all types including Image
            "width", "height", "maxWidth", "maxHeight", "minWidth", "minHeight"
        }

        # Properties that need image- prefix for Image components (actual img element styling)
        # These control how the <img> element renders INSIDE the container
        IMAGE_ELEMENT_PROPS = {
            "objectFit", "objectPosition",
            "borderRadius", "borderTopLeftRadius", "borderTopRightRadius",
            "borderBottomLeftRadius", "borderBottomRightRadius"
        }

        # Build root styles from extracted desktop CSS
        root_styles = {}
        is_absolute_fill = False  # Track if image has position:absolute with inset:0

        # For Grid components, add default/reset properties to override Nocode's defaults
        # Nocode Grid has these defaults that we need to reset/override:
        #   - _SINGLECOLUMNLAYOUT sets flex-direction: column
        #   - .comp sets position: relative
        #   - gapBetween defaults to 5px
        if comp_type == "Grid":
            # Properties to always include with defaults to reset Nocode's styles
            GRID_RESET_PROPS = {
                "flexDirection": "row",      # Reset: Nocode layout classes set column
                "flexWrap": "nowrap",        # Reset: Default flex wrap
                "gap": "0px",                # Reset: Nocode gapBetween defaults to 5px
                "position": "static",        # Reset: .comp sets position: relative
            }
            for prop, default_val in GRID_RESET_PROPS.items():
                css_prop = self._nocode_to_css_prop(prop)
                extracted_val = desktop.get(css_prop) or desktop.get(prop)
                # Use extracted value if present, otherwise use default to reset
                if extracted_val:
                    root_styles[prop] = {"value": self._process_css_value(extracted_val)}
                else:
                    # Always add reset value for Grid to override Nocode defaults
                    root_styles[prop] = {"value": default_val}

        for prop, value in desktop.items():
            if not value:
                continue

            nocode_prop = self._css_to_nocode_prop(prop)
            processed = self._process_css_value(value)
            if not processed:
                continue

            # For Image components, prefix image-specific properties
            if comp_type == "Image" and nocode_prop in IMAGE_ELEMENT_PROPS:
                root_styles[f"image-{nocode_prop}"] = {"value": processed}
            elif comp_type == "Image" and nocode_prop not in CONTAINER_PROPS:
                # Skip non-container props that aren't image-specific for Image components
                continue
            else:
                root_styles[nocode_prop] = {"value": processed}

        # For Image components with position:absolute and inset:0 (all edges = 0),
        # add image-width:100% and image-height:100% to make img fill container
        if comp_type == "Image":
            pos = root_styles.get("position", {}).get("value", "")
            top = root_styles.get("top", {}).get("value", "")
            left = root_styles.get("left", {}).get("value", "")
            right = root_styles.get("right", {}).get("value", "")
            bottom = root_styles.get("bottom", {}).get("value", "")

            # Check if it's an absolute fill pattern (all edges are 0)
            if pos == "absolute":
                edges_are_zero = all(
                    e in ("0", "0px", "0%") for e in [top, left, right, bottom] if e
                )
                if edges_are_zero and top and left:  # At least top/left are set
                    # Make the img element fill its container
                    root_styles["image-width"] = {"value": "100%"}
                    root_styles["image-height"] = {"value": "100%"}

        # Build resolutions dict with ALL (desktop) styles
        resolutions = {"ALL": root_styles} if root_styles else {}

        # Build tablet styles (only properties that differ from desktop)
        # TABLET_LANDSCAPE_SCREEN_SMALL = maxWidth: 1024px (tablet and smaller)
        tablet_diff = self._build_diff_styles_for_type(desktop, tablet, comp_type, CONTAINER_PROPS, IMAGE_ELEMENT_PROPS)
        if tablet_diff:
            resolutions["TABLET_LANDSCAPE_SCREEN_SMALL"] = tablet_diff

        # Build mobile styles (only properties that differ from desktop)
        # MOBILE_LANDSCAPE_SCREEN_SMALL = maxWidth: 640px (mobile and smaller)
        mobile_diff = self._build_diff_styles_for_type(desktop, mobile, comp_type, CONTAINER_PROPS, IMAGE_ELEMENT_PROPS)
        if mobile_diff:
            resolutions["MOBILE_LANDSCAPE_SCREEN_SMALL"] = mobile_diff

        # Generate a unique style key (use element ID or generate one)
        style_key = self._generate_style_key(elem.id)

        if resolutions:
            style_props[style_key] = {"resolutions": resolutions}

        return style_props

    def _build_diff_styles_for_type(
        self,
        base: Dict[str, str],
        current: Dict[str, str],
        comp_type: str,
        container_props: set,
        image_props: set
    ) -> Dict[str, Any]:
        """Build diff styles with proper prefix handling for component type."""
        diff_styles = {}

        for prop, value in current.items():
            if value and value != base.get(prop):
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed and processed.lower() not in {"initial", "inherit", "unset"}:
                    # For Image components, prefix image-specific properties
                    if comp_type == "Image" and nocode_prop in image_props:
                        diff_styles[f"image-{nocode_prop}"] = {"value": processed}
                    elif comp_type == "Image" and nocode_prop not in container_props:
                        continue
                    else:
                        diff_styles[nocode_prop] = {"value": processed}

        return diff_styles

    def _generate_style_key(self, element_id: str) -> str:
        """Generate a unique style key for a component."""
        import hashlib
        # Create a short unique key based on element ID
        if element_id:
            # Use first 22 chars of base64-encoded hash for uniqueness
            hash_bytes = hashlib.md5(element_id.encode()).digest()
            import base64
            return base64.urlsafe_b64encode(hash_bytes)[:22].decode()
        else:
            # Fallback to random UUID-like key
            return str(uuid.uuid4()).replace("-", "")[:22]
    
    def _build_resolution_styles(
        self, 
        css_styles: Dict[str, str], 
        include_resets: bool = False,
        comp_type: str = "Grid"
    ) -> Dict[str, Any]:
        """
        Simple 1:1 mapping: Reset then apply ALL extracted styles.
        No filtering, no optimization.
        """
        nocode_styles = {}
        
        # Step 1: Resets
        if include_resets:
            nocode_styles.update({
                "margin": {"value": "0"},
                "padding": {"value": "0"},
                "border": {"value": "none"},
                "boxSizing": {"value": "border-box"},
                "background": {"value": "transparent"},
            })
        
        # Step 2: Apply ALL extracted styles (filter only dangerous values)
        for prop, value in css_styles.items():
            if not value:
                continue
            
            nocode_prop = self._css_to_nocode_prop(prop)
            processed = self._process_css_value(value)
            if processed:
                nocode_styles[nocode_prop] = {"value": processed}
        
        return nocode_styles
    
    def _build_diff_styles(
        self, 
        base: Dict[str, str], 
        current: Dict[str, str]
    ) -> Dict[str, Any]:
        """Build styles that differ from base (for responsive breakpoints)."""
        diff_styles = {}
        
        for prop, value in current.items():
            # Only include if value differs from base
            if value and value != base.get(prop):
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed and processed.lower() not in {"initial", "inherit", "unset"}:
                    diff_styles[nocode_prop] = {"value": processed}
        
        return diff_styles
    
    def _extract_typography_styles(self, css_styles: Dict[str, str]) -> Dict[str, Any]:
        """Extract typography-related styles for text sub-component."""
        typography_props = {
            "fontSize", "fontWeight", "fontFamily", "fontStyle",
            "lineHeight", "letterSpacing", "textAlign", "textDecoration",
            "textTransform", "color"
        }
        
        result = {}
        for prop, value in css_styles.items():
            if not value:
                continue
            nocode_prop = self._css_to_nocode_prop(prop)
            if nocode_prop in typography_props:
                processed = self._process_css_value(value)
                if processed and processed.lower() not in {"initial", "inherit", "unset"}:
                    result[nocode_prop] = {"value": processed}
        
        return result
    
    def _serialize_elements(self, elements: List) -> List[Dict[str, Any]]:
        """Serialize VisualElement objects to dicts for JSON."""
        result = []
        for elem in elements:
            result.append({
                "id": elem.id,
                "tag": elem.tag,
                "text": elem.text,
                "imageUrl": elem.image_url,
                "styles": elem.styles,
                "bounds": elem.bounds,
                "attributes": elem.attributes,
                "children": self._serialize_elements(elem.children)
            })
        return result
    
    def _build_page_from_analysis(
        self,
        analysis: Dict[str, Any],
        visual_data,
        uploaded_images: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Build Nocode page definition from LLM analysis and visual data.
        
        The analysis provides structure/hierarchy, and visual data provides
        exact styles at each viewport.
        """
        # Get components from analysis
        components = analysis.get("components", [])
        root_key = analysis.get("rootComponent", "pageRoot")
        
        component_def = {}
        root_children = {}
        
        # Create root component with responsive styles
        component_def[root_key] = {
            "key": root_key,
            "name": "Page Root",
            "type": "Grid",
            "properties": {},
            "styleProperties": self._create_responsive_styles(
                visual_data.root_styles,
                default_styles={
                    "minHeight": "100vh",
                    "display": "flex",
                    "flexDirection": "column",
                    "margin": "0",
                    "padding": "0"
                }
            ),
            "children": root_children
        }
        
        # Add each component from analysis
        for comp in components:
            key = comp.get("key", f"comp_{len(component_def)}")
            comp_type = comp.get("type", "Grid")
            
            # Replace image URLs with uploaded versions
            properties = comp.get("properties", {})
            if comp_type == "Image" and "src" in properties:
                original_src = properties["src"].get("value", "")
                if original_src in uploaded_images:
                    properties["src"]["value"] = uploaded_images[original_src]
            
            component_def[key] = {
                "key": key,
                "name": comp.get("name", key),
                "type": comp_type,
                "properties": properties,
                "styleProperties": comp.get("styleProperties", {}),
                "children": comp.get("children", {})
            }
            
            # Add to root if it's a top-level component
            if comp.get("isTopLevel", False) or comp.get("parent") == root_key:
                root_children[key] = True
        
        return {
            "rootComponent": root_key,
            "componentDefinition": component_def
        }
    
    def _create_responsive_styles(
        self,
        viewport_styles: Dict[str, Dict[str, str]],
        default_styles: Dict[str, str] = None
    ) -> Dict[str, Any]:
        """
        Create Nocode styleProperties with responsive resolutions from viewport styles.
        Uses a unique style key instead of hardcoded 'rootStyle'.
        """
        resolutions = {}

        # Desktop -> ALL
        desktop_styles = viewport_styles.get("desktop", {})
        all_styles = {}

        # Add defaults first
        if default_styles:
            for prop, value in default_styles.items():
                all_styles[prop] = {"value": value}

        # Add desktop styles - pass through ALL extracted styles
        for prop, value in desktop_styles.items():
            if value and prop != "theme":
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed:
                    all_styles[nocode_prop] = {"value": processed}

        resolutions["ALL"] = all_styles

        # Tablet -> TABLET_LANDSCAPE_SCREEN_SMALL (maxWidth: 1024px)
        tablet_styles = viewport_styles.get("tablet", {})
        tablet_resolutions = {}
        for prop, value in tablet_styles.items():
            # Only include if different from desktop (responsive diff)
            if value and value != desktop_styles.get(prop) and prop != "theme":
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed:
                    tablet_resolutions[nocode_prop] = {"value": processed}

        if tablet_resolutions:
            resolutions["TABLET_LANDSCAPE_SCREEN_SMALL"] = tablet_resolutions

        # Mobile -> MOBILE_LANDSCAPE_SCREEN_SMALL (maxWidth: 640px)
        mobile_styles = viewport_styles.get("mobile", {})
        mobile_resolutions = {}
        for prop, value in mobile_styles.items():
            # Only include if different from desktop (responsive diff)
            if value and value != desktop_styles.get(prop) and prop != "theme":
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed:
                    mobile_resolutions[nocode_prop] = {"value": processed}

        if mobile_resolutions:
            resolutions["MOBILE_LANDSCAPE_SCREEN_SMALL"] = mobile_resolutions

        # Generate a unique style key for this component
        style_key = self._generate_style_key("pageRoot")

        return {
            style_key: {
                "resolutions": resolutions
            }
        }
    
    def _css_to_nocode_prop(self, css_prop: str) -> str:
        """Convert CSS property name to Nocode camelCase."""
        # Already camelCase from browser
        return css_prop

    def _nocode_to_css_prop(self, nocode_prop: str) -> str:
        """Convert Nocode camelCase property to CSS kebab-case."""
        import re
        return re.sub(r'([A-Z])', r'-\1', nocode_prop).lower()

    def _process_css_value(self, value: str) -> str:
        """Pass through CSS value as-is. No processing needed."""
        return value if value else ""
