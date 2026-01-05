"""Detection utilities for page generation request analysis"""
import re
import logging
from typing import Optional, List

from .models import PageAgentRequest, PageAgentMode

logger = logging.getLogger(__name__)


class RequestDetector:
    """
    Analyzes page generation requests to determine execution mode and required agents.
    """

    def detect_url_in_instruction(self, instruction: str) -> Optional[str]:
        """
        Detect if the instruction contains a URL that suggests website import.
        Returns the best URL for import (prioritizes design reference URLs over social media).
        """
        url_pattern = r'https?://[^\s<>"\')\]]+(?:\.[^\s<>"\')\]]+)+'
        matches = re.findall(url_pattern, instruction, re.IGNORECASE)

        valid_urls = []
        social_media_urls = []

        for url in matches:
            url = url.rstrip('.,;:!?')

            if any(skip in url.lower() for skip in ['localhost', '127.0.0.1', '0.0.0.0']):
                continue

            if any(skip in url.lower() for skip in ['.pdf', '.zip', '.tar', '.gz', '.exe', '.dmg']):
                continue

            if any(social in url.lower() for social in ['linkedin.com', 'twitter.com', 'facebook.com', 'instagram.com', 'github.com']):
                social_media_urls.append(url)
            else:
                valid_urls.append(url)

        if valid_urls:
            logger.info(f"Detected design URL for import: {valid_urls[0]}")
            return valid_urls[0]
        elif social_media_urls:
            logger.info(f"Detected social media URL for import: {social_media_urls[0]}")
            return social_media_urls[0]

        return None

    def is_exact_copy_request(self, instruction: str) -> bool:
        """
        Detect if the user wants an EXACT copy of the website.
        Returns True only for explicit "exact copy" requests.
        """
        instruction_lower = instruction.lower()

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

        for keyword in not_exact_keywords:
            if keyword in instruction_lower:
                logger.info(f"Detected 'inspired-by' intent due to: '{keyword}'")
                return False

        exact_keywords = [
            "exact copy", "exact replica", "exact same", "exactly like", "exactly the same",
            "clone", "replicate exactly", "copy exactly", "1:1 copy", "one to one",
            "carbon copy", "duplicate", "mirror", "identical", "pixel perfect",
            "same as", "copy this", "import this", "recreate this exactly"
        ]

        for keyword in exact_keywords:
            if keyword in instruction_lower:
                logger.info(f"Detected 'exact copy' intent due to: '{keyword}'")
                return True

        logger.info("No explicit copy intent detected, defaulting to 'inspired-by' mode")
        return False

    def wants_dark_theme(self, instruction: str) -> bool:
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

        return False

    def is_style_modification(self, instruction: str) -> bool:
        """
        Detect if the instruction is primarily about styling.
        Returns True for instructions that only need Styles/Animation agents.
        Very conservative - only returns True for clear style-only requests.
        """
        instruction_lower = instruction.lower()

        style_keywords = [
            'color', 'background', 'font', 'size', 'padding', 'margin',
            'border', 'shadow', 'prominent', 'bigger', 'smaller', 'larger',
            'bold', 'italic', 'opacity', 'transparent', 'dark', 'light',
            'bright', 'muted', 'spacing', 'align', 'rounded',
            'gradient', 'hover', 'fade', 'slide',
            'appearance', 'visual', 'theme',
            'highlight', 'emphasize', 'stand out', 'pop', 'subtle'
        ]

        structural_keywords = [
            'add', 'remove', 'delete', 'create', 'insert', 'move', 'build', 'make',
            'button', 'form', 'input', 'textbox', 'dropdown', 'checkbox', 'radio',
            'text', 'image', 'icon', 'link', 'grid', 'layout', 'table', 'list',
            'component', 'element', 'section', 'page', 'container', 'wrapper',
            'header', 'footer', 'sidebar', 'navbar', 'menu',
            'click', 'event', 'action', 'function', 'handler', 'trigger',
            'navigate', 'submit', 'send', 'fetch', 'api', 'call',
            'data', 'bind', 'store', 'value', 'state',
            'calculator', 'login', 'signup', 'register', 'counter', 'todo',
            'with all', 'that are required', 'need to', 'should have',
            'generate', 'implement', 'develop', 'setup', 'configure'
        ]

        has_style_keyword = any(kw in instruction_lower for kw in style_keywords)
        has_structural_keyword = any(kw in instruction_lower for kw in structural_keywords)

        is_short_instruction = len(instruction_lower.split()) <= 8

        if has_structural_keyword:
            logger.debug("[is_style_modification] Found structural keyword, running full pipeline")
            return False

        if has_style_keyword and is_short_instruction:
            logger.info("[is_style_modification] Detected style-only: short instruction with style keywords")
            return True

        logger.debug("[is_style_modification] Uncertain, running full pipeline")
        return False

    def determine_agents_needed(self, request: PageAgentRequest, is_inspired_by: bool = False) -> List[str]:
        """
        Determine which agents are needed based on the request.
        Returns a list of agent names to run.

        Args:
            request: The page agent request
            is_inspired_by: If True, this is an inspired-by mode request (URL reference)
                           which should run the full creation pipeline
        """
        instruction_lower = request.instruction.lower()
        mode = request.options.mode

        # For CREATE mode or inspired-by mode, run the full pipeline
        # Inspired-by is essentially creating a new page with a style reference
        if mode == PageAgentMode.CREATE or is_inspired_by:
            logger.info(f"Running full pipeline: mode={mode}, is_inspired_by={is_inspired_by}")
            return ["layout", "component", "events", "styles", "animation", "data", "review"]

        agents = []

        layout_keywords = ['layout', 'structure', 'arrange', 'organize', 'grid', 'row', 'column',
                          'header', 'footer', 'sidebar', 'section', 'container']
        if any(kw in instruction_lower for kw in layout_keywords) and not request.options.preserveLayout:
            agents.append("layout")

        component_keywords = ['component', 'button', 'input', 'textbox', 'form', 'add', 'create',
                             'remove', 'delete', 'element', 'field', 'label']
        if any(kw in instruction_lower for kw in component_keywords):
            agents.append("component")

        events_keywords = ['click', 'event', 'action', 'function', 'handler', 'onclick', 'trigger',
                          'navigate', 'submit', 'send', 'fetch', 'api', 'call', 'interaction']
        if any(kw in instruction_lower for kw in events_keywords) and not request.options.preserveEvents:
            agents.append("events")

        animation_keywords = ['animate', 'animation', 'transition', 'fade', 'slide', 'hover',
                             'effect', 'motion', 'smooth', 'bounce', 'pulse']
        if any(kw in instruction_lower for kw in animation_keywords):
            agents.append("animation")

        data_keywords = ['data', 'bind', 'store', 'value', 'state', 'variable', 'binding',
                        'form data', 'save', 'load', 'fetch', 'api']
        if any(kw in instruction_lower for kw in data_keywords):
            agents.append("data")

        if not agents:
            logger.info("No specific agents detected, running component + styles")
            agents = ["component"]

        if "styles" not in agents and not request.options.preserveStyles:
            agents.append("styles")

        agents.append("review")

        logger.info(f"Determined agents needed: {agents}")
        return agents

    def extract_color_palette(self, visual_data) -> List[str]:
        """Extract dominant colors from the visual data for style reference."""
        colors = set()

        if visual_data and visual_data.root_styles:
            for key, value in visual_data.root_styles.items():
                if isinstance(value, str) and ('rgb' in value.lower() or '#' in value):
                    colors.add(value)

        if visual_data and visual_data.elements:
            for elem in visual_data.elements[:5]:
                if hasattr(elem, 'styles') and elem.styles:
                    desktop = elem.styles.get('desktop', {})
                    for key in ['backgroundColor', 'color', 'borderColor']:
                        if key in desktop and desktop[key]:
                            colors.add(desktop[key])

        return list(colors)[:10]


_detector = None

def get_request_detector() -> RequestDetector:
    global _detector
    if _detector is None:
        _detector = RequestDetector()
    return _detector
