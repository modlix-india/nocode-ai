"""Execution mode handlers for page generation"""
import json
import logging
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING

from app.config import settings
from app.streaming.events import ProgressCallback

if TYPE_CHECKING:
    from app.agents.base import AgentInput, AgentOutput

from .models import (
    PageAgentRequest, PageAgentResponse, PageAgentMode,
    AgentLogEntry, TokenUsageByAgent, TokenUsageSummary, ContextUsageInfo
)
from .converters import get_html_to_nocode_converter
from .detectors import get_request_detector

logger = logging.getLogger(__name__)


class StyleOnlyExecutor:
    """
    Fast path executor for style-only modifications.
    Only runs Styles and Animation agents (both use Haiku - fast and cheap).
    """

    def __init__(self, styles_agent, animation_agent, context_builder):
        self.styles_agent = styles_agent
        self.animation_agent = animation_agent
        self.context_builder = context_builder
        self.RATE_LIMIT_DELAY_HAIKU = 2

    async def execute(
        self,
        request: PageAgentRequest,
        progress_callback: Optional[ProgressCallback] = None
    ) -> PageAgentResponse:
        from app.utils.merge import merge_agent_outputs

        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)

        await emit('status', "Detected style modification - using fast mode (Haiku only)...")

        agent_logs: Dict[str, AgentLogEntry] = {}
        from app.agents.base import AgentInput

        context = self.context_builder.build_context(request)
        outputs = {}

        if request.existingPage:
            outputs["layout"] = {"rootComponent": request.existingPage.get("rootComponent")}

        enhancement_input = AgentInput(
            user_request=request.instruction,
            context=context,
            previous_outputs={}
        )

        await emit('phase', 'Styling (Fast Mode)')

        name, styles_result = await self._run_agent_with_progress(
            self.styles_agent, "Styles", enhancement_input, progress_callback
        )
        agent_logs[name.lower()] = self._format_agent_log(styles_result, self.styles_agent)
        outputs["styles"] = styles_result.result

        await self._sleep_with_keepalive(
            self.RATE_LIMIT_DELAY_HAIKU, progress_callback, "Waiting before Animation..."
        )
        name, animation_result = await self._run_agent_with_progress(
            self.animation_agent, "Animation", enhancement_input, progress_callback
        )
        agent_logs[name.lower()] = self._format_agent_log(animation_result, self.animation_agent)
        outputs["animation"] = animation_result.result

        await emit('merging', 'Applying style changes...')

        logger.info(f"[FastMode] Styles output keys: {list(outputs.get('styles', {}).keys())}")
        logger.info(f"[FastMode] Animation output keys: {list(outputs.get('animation', {}).keys())}")

        merged_page = merge_agent_outputs(outputs, request.existingPage)

        return PageAgentResponse(
            success=all(log.status == "success" for log in agent_logs.values()),
            page=merged_page,
            agentLogs=agent_logs
        )

    async def _run_agent_with_progress(self, agent, name: str, input: "AgentInput", progress: Optional[ProgressCallback]) -> Tuple[str, "AgentOutput"]:
        if progress:
            model_name = getattr(agent, 'model', 'unknown').split('/')[-1]
            await progress.agent_start(name, f"Starting {name} (using {model_name.split('-')[1] if '-' in model_name else model_name})...")
        result = await agent.execute(input, progress)
        if progress:
            await progress.agent_complete(name, result.success, f"{name} {'completed' if result.success else 'failed'}")
        return (name, result)

    async def _sleep_with_keepalive(self, seconds: int, progress: Optional[ProgressCallback], message: str = "Waiting..."):
        import asyncio
        from app.agents.base import KEEPALIVE_INTERVAL
        elapsed = 0
        while elapsed < seconds:
            sleep_time = min(KEEPALIVE_INTERVAL, seconds - elapsed)
            await asyncio.sleep(sleep_time)
            elapsed += sleep_time
            if progress and elapsed < seconds:
                await progress.keepalive(message)

    def _format_agent_log(self, result: "AgentOutput", agent=None) -> AgentLogEntry:
        model = getattr(agent, 'model', None) if agent else None
        return AgentLogEntry(status="success" if result.success else "error", reasoning=result.reasoning, errors=result.errors, model=model)


class ImportModeExecutor:
    """
    Executor for website import mode (exact copy).
    Uses multi-viewport visual extraction and direct 1:1 conversion.
    """

    def __init__(self):
        self.converter = get_html_to_nocode_converter()

    async def execute(
        self,
        request: PageAgentRequest,
        progress_callback: Optional[ProgressCallback] = None
    ) -> PageAgentResponse:
        from app.services.website_extractor import get_website_extractor
        from app.services.image_uploader import get_image_uploader

        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)

        if not request.sourceUrl:
            raise ValueError("sourceUrl is required for import mode")

        await emit('status', f"Starting website import from {request.sourceUrl}...")

        agent_logs: Dict[str, AgentLogEntry] = {}

        # Phase 1: Visual Extraction
        await emit('phase', 'Visual Extraction')
        await emit('status', "Extracting visual data at multiple viewport sizes...")

        try:
            extractor = get_website_extractor()
            visual_data = await extractor.extract(request.sourceUrl)

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
                agentLogs={"extraction": AgentLogEntry(status="error", errors=[f"Failed to extract website: {str(e)}"])}
            )

        # Phase 2: Image Upload
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

                agent_logs["image_upload"] = AgentLogEntry(status="success", reasoning=f"Uploaded {successful} images")
            except Exception as e:
                logger.error(f"Image upload phase failed: {e}")
                agent_logs["image_upload"] = AgentLogEntry(status="error", errors=[str(e)])

        # Phase 3: Direct Conversion
        await emit('phase', 'Direct Conversion')
        await emit('status', "Converting elements to Nocode components...")

        try:
            page_def = self.converter.convert_visual_to_nocode(visual_data, uploaded_images)
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
                agentLogs={**agent_logs, "conversion": AgentLogEntry(status="error", errors=[f"Failed to convert: {str(e)}"])}
            )

        # Finalization
        await emit('phase', 'Finalization')

        if request.existingPage and request.existingPage.get("name"):
            page_def["name"] = request.existingPage["name"]

        if "properties" not in page_def:
            page_def["properties"] = {}

        logger.info(f"Import complete: {component_count} components from {request.sourceUrl}")
        await emit('status', 'Import complete!')

        return PageAgentResponse(
            success=all(log.status == "success" for log in agent_logs.values()),
            page=page_def,
            agentLogs=agent_logs
        )


class InspiredByModeExecutor:
    """
    Executor for inspired-by mode.
    Uses website as REFERENCE but generates unique content via LLM agents.
    """

    def __init__(self, layout_agent, component_agent, events_agent, styles_agent, animation_agent, data_agent):
        self.layout_agent = layout_agent
        self.component_agent = component_agent
        self.events_agent = events_agent
        self.styles_agent = styles_agent
        self.animation_agent = animation_agent
        self.data_agent = data_agent
        self.detector = get_request_detector()

    def _extract_page_content(self, visual_data) -> Dict[str, Any]:
        """
        Extract text content, colors, and structure from the visual data.
        This provides the LLM with actual content from the reference page.
        """
        result = {
            "texts": [],
            "colors": [],
            "structure": [],
            "backgroundColor": "#ffffff",
            "textColor": "#1a1a1a",
            "isDarkTheme": False,
        }

        if not visual_data or not visual_data.elements:
            return result

        seen_texts = set()
        seen_colors = set()

        for elem in visual_data.elements:
            # Extract text content
            if elem.text and elem.text.strip():
                text = elem.text.strip()[:200]  # Limit text length
                if text not in seen_texts and len(text) > 2:
                    seen_texts.add(text)
                    result["texts"].append({
                        "text": text,
                        "tag": elem.tag,
                        "isHeading": elem.tag in ["h1", "h2", "h3", "h4", "h5", "h6"],
                        "isButton": elem.tag == "button" or "btn" in (elem.attributes.get("class", "") or "").lower(),
                        "isLink": elem.tag == "a",
                    })

            # Extract colors from desktop styles
            desktop_styles = elem.styles.get("desktop", {})
            for color_prop in ["color", "backgroundColor", "borderColor"]:
                color_val = desktop_styles.get(color_prop, "")
                if color_val and color_val not in seen_colors:
                    if color_val.startswith("#") or color_val.startswith("rgb"):
                        seen_colors.add(color_val)
                        result["colors"].append({
                            "value": color_val,
                            "property": color_prop,
                        })

        # Extract root background and text colors from viewport-based root_styles
        root_styles = visual_data.root_styles.get("desktop", {})
        if "backgroundColor" in root_styles:
            result["backgroundColor"] = root_styles["backgroundColor"]
        if "color" in root_styles:
            result["textColor"] = root_styles["color"]

        # Auto-detect dark theme from background color
        bg_color = result["backgroundColor"]
        result["isDarkTheme"] = self._is_dark_color(bg_color)

        # If root didn't have background, check first few elements for dark backgrounds
        if not result["isDarkTheme"] and result["colors"]:
            bg_colors = [c["value"] for c in result["colors"] if c["property"] == "backgroundColor"]
            dark_bg_count = sum(1 for c in bg_colors[:5] if self._is_dark_color(c))
            if dark_bg_count >= 2:  # If multiple dark backgrounds found
                result["isDarkTheme"] = True
                # Use the first dark background as the primary
                for c in bg_colors:
                    if self._is_dark_color(c):
                        result["backgroundColor"] = c
                        break

        logger.info(f"[ExtractContent] Background: {result['backgroundColor']}, isDark: {result['isDarkTheme']}")

        # Limit results to avoid token bloat
        result["texts"] = result["texts"][:30]
        result["colors"] = result["colors"][:20]

        return result

    def _is_dark_color(self, color: str) -> bool:
        """
        Determine if a color is dark based on luminance.
        Returns True if the color is dark (would need light text on it).
        """
        if not color:
            return False

        try:
            # Handle hex colors
            if color.startswith("#"):
                hex_color = color.lstrip("#")
                if len(hex_color) == 3:
                    hex_color = "".join(c * 2 for c in hex_color)
                if len(hex_color) == 6:
                    r = int(hex_color[0:2], 16)
                    g = int(hex_color[2:4], 16)
                    b = int(hex_color[4:6], 16)
                else:
                    return False

            # Handle rgb/rgba colors
            elif color.startswith("rgb"):
                import re
                match = re.match(r'rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', color)
                if match:
                    r, g, b = int(match.group(1)), int(match.group(2)), int(match.group(3))
                else:
                    return False
            else:
                return False

            # Calculate relative luminance (simplified)
            # Dark colors have low luminance
            luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
            return luminance < 0.5  # Threshold for dark

        except Exception as e:
            logger.debug(f"Could not parse color '{color}': {e}")
            return False

    def _build_content_summary(self, extracted_content: Dict[str, Any]) -> str:
        """Build a summary of extracted content for the LLM prompt."""
        lines = []

        # Text content summary
        texts = extracted_content.get("texts", [])
        if texts:
            headings = [t for t in texts if t.get("isHeading")]
            buttons = [t for t in texts if t.get("isButton")]
            links = [t for t in texts if t.get("isLink")]
            paragraphs = [t for t in texts if not t.get("isHeading") and not t.get("isButton") and not t.get("isLink")]

            if headings:
                lines.append("HEADINGS from reference:")
                for h in headings[:5]:
                    lines.append(f"  - {h['text'][:80]}")

            if buttons:
                lines.append("BUTTONS from reference:")
                for b in buttons[:5]:
                    lines.append(f"  - {b['text'][:40]}")

            if links:
                lines.append("NAVIGATION LINKS from reference:")
                for l in links[:8]:
                    lines.append(f"  - {l['text'][:40]}")

            if paragraphs:
                lines.append("SAMPLE TEXT from reference:")
                for p in paragraphs[:3]:
                    lines.append(f"  - {p['text'][:100]}...")

        # Color palette summary
        colors = extracted_content.get("colors", [])
        if colors:
            bg_colors = [c["value"] for c in colors if c["property"] == "backgroundColor"][:5]
            text_colors = [c["value"] for c in colors if c["property"] == "color"][:5]

            if bg_colors:
                lines.append(f"BACKGROUND COLORS: {', '.join(bg_colors)}")
            if text_colors:
                lines.append(f"TEXT COLORS: {', '.join(text_colors)}")

        bg = extracted_content.get("backgroundColor", "#ffffff")
        text = extracted_content.get("textColor", "#1a1a1a")
        is_dark = extracted_content.get("isDarkTheme", False)

        lines.append(f"THEME: {'DARK' if is_dark else 'LIGHT'}")
        lines.append(f"PRIMARY BACKGROUND: {bg}")
        lines.append(f"PRIMARY TEXT COLOR: {text}")

        if is_dark:
            lines.append("IMPORTANT: This is a DARK theme website. Use dark backgrounds and light text colors.")

        return "\n".join(lines)

    async def execute(
        self,
        request: PageAgentRequest,
        progress_callback: Optional[ProgressCallback] = None
    ) -> PageAgentResponse:
        from app.services.website_extractor import get_website_extractor
        from app.utils.merge import merge_agent_outputs
        from app.agents.base import AgentInput

        async def emit(method: str, *args, **kwargs):
            if progress_callback:
                await getattr(progress_callback, method)(*args, **kwargs)

        await emit('status', "Inspired-by mode: Using reference website for style inspiration...")

        agent_logs: Dict[str, AgentLogEntry] = {}
        source_url = request.sourceUrl

        try:
            extractor = get_website_extractor()
            await emit('status', f"Capturing reference screenshot from {source_url}...")

            try:
                visual_data = await extractor.extract(source_url)
                screenshot_base64 = visual_data.screenshot if visual_data else None

                extracted_colors = self.detector.extract_color_palette(visual_data) if visual_data else []

                # Extract text content and structure from the source page
                extracted_content = self._extract_page_content(visual_data) if visual_data else {}
                logger.info(f"[InspiredByMode] Extracted content: {len(extracted_content.get('texts', []))} texts, {len(extracted_content.get('colors', []))} colors")

                # Use auto-detected theme from source website, or user's explicit preference
                user_wants_dark = self.detector.wants_dark_theme(request.instruction)
                source_is_dark = extracted_content.get("isDarkTheme", False)
                use_dark_theme = user_wants_dark or source_is_dark

                logger.info(f"[InspiredByMode] Theme detection: user_wants_dark={user_wants_dark}, source_is_dark={source_is_dark}, using_dark={use_dark_theme}")

                # Build style hints with the detected/extracted colors
                style_hints = {
                    "referenceUrl": source_url,
                    "hasScreenshot": screenshot_base64 is not None,
                    "rootStyles": visual_data.root_styles if visual_data else {},
                    "colorPalette": extracted_colors,
                    "extractedColors": extracted_content.get("colors", []),
                    "theme": "dark" if use_dark_theme else "light",
                    "backgroundColor": extracted_content.get("backgroundColor", "#1a1a1a" if use_dark_theme else "#ffffff"),
                    "textColor": extracted_content.get("textColor", "#ffffff" if use_dark_theme else "#1a1a1a"),
                }

                await emit('status', "Reference captured. Generating your page with similar style...")

            except Exception as e:
                logger.warning(f"Could not capture reference website: {e}")
                screenshot_base64 = None
                style_hints = {"referenceUrl": source_url, "error": str(e)}
                extracted_content = {}
                await emit('status', "Could not capture reference, proceeding with text description...")
            finally:
                await extractor.close()

            # Build content summary for the LLM
            content_summary = self._build_content_summary(extracted_content) if extracted_content else ""

            enhanced_instruction = f"""
{request.instruction}

STYLE REFERENCE: The user provided {source_url} as a style reference.
Use the EXACT color scheme and visual style from the reference.

## EXTRACTED CONTENT FROM REFERENCE:
{content_summary if content_summary else "No content extracted - use placeholder content"}

## INSTRUCTIONS:
1. Use the SAME color palette as the reference (background colors, text colors, accent colors)
2. Match the visual style (typography, spacing, layout patterns)
3. Use similar content structure (headings, sections, CTAs)
4. Generate appropriate placeholder content that matches the reference's tone
"""

            agent_context = {
                "styleHints": style_hints,
                "extractedContent": extracted_content,
            }
            if screenshot_base64:
                agent_context["referenceScreenshot"] = screenshot_base64

            await emit('status', "Running layout agent with style reference...")

            # For inspired-by mode, always run the full creation pipeline
            # since we're essentially creating a new page with a style reference
            agents_needed = self.detector.determine_agents_needed(
                PageAgentRequest(
                    instruction=enhanced_instruction,
                    existingPage=request.existingPage,
                    options=request.options,
                    sourceUrl=None
                ),
                is_inspired_by=True  # This ensures component agent is included
            )
            logger.info(f"[InspiredByMode] Agents needed: {agents_needed}")

            outputs: Dict[str, Any] = {}

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

            if "component" in agents_needed:
                await emit('status', "Component agent creating structure...")
                layout_for_component = outputs.get("layout", {})
                layout_containers = layout_for_component.get("componentDefinition", {})
                logger.info(f"[InspiredByMode] Passing {len(layout_containers)} containers to Component agent: {list(layout_containers.keys())[:10]}")
                try:
                    comp_result = await self.component_agent.execute(
                        AgentInput(
                            user_request=enhanced_instruction,
                            context=agent_context,
                            previous_outputs={"layout": layout_for_component}
                        )
                    )
                    outputs["component"] = comp_result.result
                    # Log component agent output details
                    comp_output = comp_result.result
                    if isinstance(comp_output, dict):
                        logger.info(f"[InspiredByMode] Component agent output keys: {list(comp_output.keys())}")
                        if "components" in comp_output:
                            comp_types = {}
                            for k, v in comp_output["components"].items():
                                t = v.get("type", "Unknown") if isinstance(v, dict) else "Invalid"
                                comp_types[t] = comp_types.get(t, 0) + 1
                            logger.info(f"[InspiredByMode] Component agent created: {len(comp_output['components'])} components, types: {comp_types}")
                    agent_logs["component"] = AgentLogEntry(
                        status="success" if comp_result.success else "failed",
                        reasoning=comp_result.reasoning,
                        errors=comp_result.errors
                    )
                except Exception as e:
                    logger.error(f"[InspiredByMode] Component agent failed: {e}", exc_info=True)
                    agent_logs["component"] = AgentLogEntry(status="failed", error=str(e))

            if "styles" in agents_needed:
                await emit('status', "Styles agent applying reference styling...")
                try:
                    style_context = dict(agent_context)
                    style_context["styleHints"] = style_hints
                    style_context["importMode"] = False

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

            # Log outputs before merge
            logger.info(f"[InspiredByMode] About to merge outputs. Keys: {list(outputs.keys())}")
            for out_key, out_val in outputs.items():
                if isinstance(out_val, dict):
                    logger.info(f"[InspiredByMode] outputs['{out_key}'] keys: {list(out_val.keys())}")

            merged_page = merge_agent_outputs(outputs, request.existingPage)

            # Log merged result
            merged_comp_def = merged_page.get("componentDefinition", {})
            merged_types = {}
            for k, v in merged_comp_def.items():
                t = v.get("type", "Unknown") if isinstance(v, dict) else "Invalid"
                merged_types[t] = merged_types.get(t, 0) + 1
            logger.info(f"[InspiredByMode] After merge: {len(merged_comp_def)} components, types: {merged_types}")

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


class SessionManager:
    """
    Handles session initialization and token usage tracking.
    """

    async def initialize_session(
        self,
        request: PageAgentRequest,
        auth_context: Dict[str, Any],
        request_id: str
    ) -> Tuple[Optional[str], Optional[int]]:
        """Initialize or retrieve a session for tracking."""
        from app.services.session_manager import get_session_manager

        session_manager = get_session_manager()

        if request.sessionId and not request.newSession:
            existing_session = await session_manager.get_session(request.sessionId)
            if existing_session:
                turn_number = await session_manager.increment_turn_count(
                    request.sessionId,
                    auth_context.get("userId")
                )
                logger.info(f"Continuing session {request.sessionId}, turn {turn_number}")
                return request.sessionId, turn_number

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
            return session.session_id, 1
        else:
            logger.warning("Failed to create session, tracking disabled for this request")
            return None, None

    async def record_token_usage(
        self,
        session_id: str,
        request_id: str,
        auth_context: Dict[str, Any],
        token_usages: List[Dict[str, Any]],
        instruction: str,
        final_page: Dict[str, Any]
    ) -> Tuple[Optional[TokenUsageSummary], Optional[ContextUsageInfo]]:
        """Record token usage to database and return usage summaries."""
        from app.services.token_tracker import get_token_tracker
        from app.services.context_manager import get_context_manager
        from app.services.session_manager import get_session_manager
        from app.db.models import AiTokenUsageCreate

        token_tracker = get_token_tracker()
        context_manager = get_context_manager()
        session_manager = get_session_manager()

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

            total_input += input_tokens
            total_output += output_tokens
            total_cache_read += cache_read
            total_cache_creation += cache_creation

            by_agent[agent_type] = TokenUsageByAgent(
                inputTokens=input_tokens,
                outputTokens=output_tokens,
                cacheReadTokens=cache_read,
                cacheCreationTokens=cache_creation,
                model=usage.get("model"),
                latencyMs=usage.get("latency_ms")
            )

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

        if usage_records:
            await token_tracker.record_usage_batch(usage_records, update_session=True)

        session = await session_manager.get_session(session_id)
        if session:
            summary = f"Generated page with {len(final_page.get('componentDefinition', {}))} components"
            await context_manager.add_turn(
                session_id=session_id,
                request_id=request_id,
                turn_number=session.turn_count,
                user_instruction=instruction,
                assistant_summary=summary,
                page_snapshot=json.dumps(final_page) if final_page else None
            )

        token_usage_summary = TokenUsageSummary(
            totalInputTokens=total_input,
            totalOutputTokens=total_output,
            totalCacheReadTokens=total_cache_read,
            totalCacheCreationTokens=total_cache_creation,
            byAgent=by_agent
        )

        context_usage_info = None
        if session:
            context_usage = await context_manager.get_context_usage(session_id)
            if context_usage:
                context_usage_info = ContextUsageInfo(
                    used=context_usage.used,
                    limit=context_usage.limit,
                    percentage=context_usage.percentage,
                    turnsInContext=context_usage.turns_in_context,
                    warning=context_usage.warning
                )

        return token_usage_summary, context_usage_info
