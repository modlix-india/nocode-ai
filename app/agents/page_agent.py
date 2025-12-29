"""Page Agent - Orchestrates sub-agents to generate/modify pages"""
import asyncio
from typing import Dict, Any, Optional, List, Tuple
from pydantic import BaseModel
from enum import Enum
import logging

from app.agents.base import AgentInput, AgentOutput, KEEPALIVE_INTERVAL
from app.agents.layout import LayoutAgent
from app.agents.component import ComponentAgent
from app.agents.events import EventsAgent
from app.agents.styles import StylesAgent
from app.agents.animation import AnimationAgent
from app.agents.data import DataAgent
from app.agents.review import ReviewAgent
from app.utils.merge import merge_agent_outputs
from app.streaming.events import ProgressCallback

logger = logging.getLogger(__name__)


class PageAgentMode(str, Enum):
    CREATE = "create"      # Generate new page from scratch
    MODIFY = "modify"      # Modify specific aspects of existing page
    ENHANCE = "enhance"    # Add features to existing page


class PageAgentOptions(BaseModel):
    """Options for page generation"""
    mode: PageAgentMode = PageAgentMode.CREATE
    preserveEvents: bool = False   # Keep existing events when modifying
    preserveStyles: bool = False   # Keep existing styles when modifying
    preserveLayout: bool = False   # Keep existing layout when modifying


class PageAgentRequest(BaseModel):
    """Request to generate or modify a page"""
    instruction: str
    page: Optional[Dict[str, Any]] = None  # Existing page definition
    selectedComponentKey: Optional[str] = None  # Component to focus on
    componentScreenshot: Optional[str] = None  # Base64 image for visual feedback
    options: PageAgentOptions = PageAgentOptions()
    
    # Alias for backward compatibility
    @property
    def existingPage(self) -> Optional[Dict[str, Any]]:
        return self.page


class AgentLogEntry(BaseModel):
    """Log entry for agent execution"""
    status: str  # "success" | "error"
    reasoning: Optional[str] = None
    errors: List[str] = []
    model: Optional[str] = None  # Track which model was used


class PageAgentResponse(BaseModel):
    """Response from page generation"""
    success: bool
    page: Dict[str, Any]
    agentLogs: Dict[str, AgentLogEntry]


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
        progress_callback: Optional[ProgressCallback] = None
    ) -> PageAgentResponse:
        """Main entry point for page generation/modification"""
        
        # Helper to emit progress
        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)
        
        # Detect if this is a simple style-only modification
        # Skip unnecessary agents to reduce time and rate limit issues
        is_style_only = self._is_style_modification(request.instruction)
        
        if is_style_only and request.options.mode == PageAgentMode.MODIFY:
            return await self._execute_style_only(request, progress_callback)
        
        await emit('status', f"Starting page generation in {request.options.mode.value} mode...")
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
        
        if not request.options.preserveLayout or request.existingPage is None:
            name, layout_result = await self._run_agent_with_progress(
                self.layout_agent, "Layout", foundation_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(layout_result, self.layout_agent)
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_SONNET, progress_callback, "Waiting before Component..."
            )
        
        # Run Component agent (two-phase internally: Haiku analyze → Sonnet generate)
        name, component_result = await self._run_agent_with_progress(
            self.component_agent, "Component", foundation_input, progress_callback
        )
        agent_logs[name.lower()] = self._format_agent_log(component_result, self.component_agent)
        
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
        if not request.options.preserveEvents or request.existingPage is None:
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_SONNET, progress_callback, "Waiting before Events..."
            )
            name, events_result = await self._run_agent_with_progress(
                self.events_agent, "Events", enhancement_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(events_result, self.events_agent)
            outputs["events"] = events_result.result
        
        # Styles agent (Haiku - fast)
        if not request.options.preserveStyles or request.existingPage is None:
            await self._sleep_with_keepalive(
                self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Styles..."
            )
            name, styles_result = await self._run_agent_with_progress(
                self.styles_agent, "Styles", enhancement_input, progress_callback
            )
            agent_logs[name.lower()] = self._format_agent_log(styles_result, self.styles_agent)
            outputs["styles"] = styles_result.result
        
        # Animation agent (Haiku - fast)
        await self._sleep_with_keepalive(
            self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Animation..."
        )
        name, animation_result = await self._run_agent_with_progress(
            self.animation_agent, "Animation", enhancement_input, progress_callback
        )
        agent_logs[name.lower()] = self._format_agent_log(animation_result, self.animation_agent)
        outputs["animation"] = animation_result.result
        
        # Data agent (Haiku - fast)
        await self._sleep_with_keepalive(
            self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Data..."
        )
        name, data_result = await self._run_agent_with_progress(
            self.data_agent, "Data", enhancement_input, progress_callback
        )
        agent_logs[name.lower()] = self._format_agent_log(data_result, self.data_agent)
        outputs["data"] = data_result.result
        
        # Preserve existing data if options set
        if request.existingPage:
            if request.options.preserveEvents and "events" not in outputs:
                outputs["events"] = {"eventFunctions": request.existingPage.get("eventFunctions", {})}
            if request.options.preserveStyles and "styles" not in outputs:
                outputs["styles"] = {"componentStyles": {}}  # Use existing styles
        
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
        
        # Build response
        final_page = review_result.result if review_result.success else merged_page
        
        response = PageAgentResponse(
            success=all(log.status == "success" for log in agent_logs.values()),
            page=final_page,
            agentLogs=agent_logs
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
        
        # Add screenshot for visual feedback (Claude Vision)
        if request.componentScreenshot:
            context["componentScreenshot"] = request.componentScreenshot
            context["hasVisualFeedback"] = True
        
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
    
    def _is_style_modification(self, instruction: str) -> bool:
        """
        Detect if the instruction is primarily about styling.
        Returns True for instructions that only need Styles/Animation agents.
        """
        instruction_lower = instruction.lower()
        
        # Keywords that indicate style-only changes
        style_keywords = [
            'color', 'background', 'font', 'size', 'padding', 'margin',
            'border', 'shadow', 'prominent', 'bigger', 'smaller', 'larger',
            'bold', 'italic', 'opacity', 'transparent', 'dark', 'light',
            'bright', 'muted', 'spacing', 'align', 'center', 'rounded',
            'gradient', 'hover', 'animation', 'animate', 'fade', 'slide',
            'look', 'style', 'appearance', 'visual', 'design', 'theme',
            'highlight', 'emphasize', 'stand out', 'pop', 'subtle'
        ]
        
        # Keywords that indicate structural changes (NOT style-only)
        structural_keywords = [
            'add', 'remove', 'delete', 'create', 'insert', 'move',
            'button', 'form', 'input', 'text', 'image', 'grid', 'layout',
            'component', 'element', 'section', 'click', 'event', 'action',
            'navigate', 'submit', 'data', 'bind', 'store', 'fetch'
        ]
        
        has_style_keyword = any(kw in instruction_lower for kw in style_keywords)
        has_structural_keyword = any(kw in instruction_lower for kw in structural_keywords)
        
        # Style-only if has style keywords but no structural keywords
        return has_style_keyword and not has_structural_keyword
    
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
