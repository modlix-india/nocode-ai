"""Context building utilities for page generation agents"""
import logging
from typing import Dict, Any, List, Optional

from .models import PageAgentRequest, PageAgentMode

logger = logging.getLogger(__name__)


class ContextBuilder:
    """
    Builds context dictionaries for agent inputs from page generation requests.
    """

    def build_context(self, request: PageAgentRequest) -> Dict[str, Any]:
        """Build context for agents based on request."""
        context = {
            "mode": request.options.mode.value,
            "hasExistingPage": request.existingPage is not None,
            "preserveEvents": request.options.preserveEvents,
            "preserveStyles": request.options.preserveStyles,
            "preserveLayout": request.options.preserveLayout
        }

        if request.existingPage:
            if request.selectedComponentKey and request.options.mode == PageAgentMode.MODIFY:
                context["existingPage"] = self.extract_relevant_context(
                    request.existingPage,
                    request.selectedComponentKey
                )
            else:
                context["existingPage"] = request.existingPage
            context["existingComponents"] = self.extract_component_keys(request.existingPage)

        if request.selectedComponentKey:
            context["selectedComponentKey"] = request.selectedComponentKey
            if request.existingPage:
                comp_def = request.existingPage.get("componentDefinition", {})
                if request.selectedComponentKey in comp_def:
                    context["selectedComponent"] = comp_def[request.selectedComponentKey]

        if request.componentScreenshot:
            context["componentScreenshot"] = request.componentScreenshot
            context["hasVisualFeedback"] = True

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

        if request.file:
            context["uploadedFile"] = {
                "name": request.file.name,
                "type": request.file.type,
                "content": request.file.content,
            }
            context["hasUploadedFile"] = True
            logger.info(f"Uploaded file included: {request.file.name} ({request.file.type})")

        if request.theme:
            context["theme"] = {"themeName": request.theme.themeName}
            context["hasTheme"] = True
            context["themeInstructions"] = (
                "The application uses a theme system. Theme values are accessed via 'Theme.<propertyName>' syntax. "
                "When setting styles, prefer using theme values (e.g., 'Theme.primaryColor', 'Theme.textColor') "
                "instead of hardcoded values when appropriate. "
                "You can ignore theme values if the user explicitly requests specific colors or values. "
                "Theme values provide consistency across the application."
            )
            logger.info(f"Theme included: {request.theme.themeName}")

        if request.iconPacks:
            context["availableIconPacks"] = request.iconPacks
            context["hasIconPacks"] = True
            logger.info(f"Available icon packs included: {len(request.iconPacks)} packs")

        if request.fontPacks:
            fontNames = [pack.name for pack in request.fontPacks]
            context["availableFontPacks"] = [
                {"name": pack.name, "code": pack.code} for pack in request.fontPacks
            ]
            context["availableFonts"] = fontNames
            context["hasFontPacks"] = True
            context["fontInstructions"] = (
                "When using fonts in styles, prefer fonts from the availableFontPacks list. "
                "Each font pack has a name and a code (HTML link tag) that needs to be added to the app definition for the font to load. "
                "If a required font is not in the list, you can suggest adding it to the app definition with the appropriate font pack code. "
                "Suggest font additions in your reasoning or as a separate recommendation."
            )
            logger.info(f"Available font packs included: {len(request.fontPacks)} packs")

        return context

    def extract_relevant_context(self, page: Dict, selected_key: str) -> Dict:
        """
        Extract only relevant parts of the page for modification.
        Returns a minimal page structure with:
        - Selected component and its children
        - Parent chain to root
        - eventFunctions that reference selected component
        """
        comp_def = page.get("componentDefinition", {})

        if selected_key not in comp_def:
            return page

        relevant_keys = {selected_key}

        def collect_children(key):
            comp = comp_def.get(key, {})
            for child_key in comp.get("children", {}).keys():
                if child_key in comp_def:
                    relevant_keys.add(child_key)
                    collect_children(child_key)

        collect_children(selected_key)

        def find_parent(target_key):
            for key, comp in comp_def.items():
                if target_key in comp.get("children", {}):
                    return key
            return None

        parent = find_parent(selected_key)
        while parent:
            relevant_keys.add(parent)
            parent = find_parent(parent)

        minimal_comp_def = {k: comp_def[k] for k in relevant_keys if k in comp_def}

        return {
            "name": page.get("name"),
            "rootComponent": page.get("rootComponent"),
            "componentDefinition": minimal_comp_def,
            "eventFunctions": page.get("eventFunctions", {}) if len(str(page.get("eventFunctions", {}))) < 2000 else {},
            "_note": f"Truncated page context focusing on '{selected_key}' and its hierarchy"
        }

    def extract_component_keys(self, page: Dict) -> List[str]:
        """Extract all component keys from existing page."""
        keys = []

        def traverse(component):
            if isinstance(component, dict):
                if "key" in component:
                    keys.append(component["key"])
                for child in component.get("children", {}).values():
                    traverse(child)

        traverse(page.get("rootComponent", {}))
        return keys


_builder = None

def get_context_builder() -> ContextBuilder:
    global _builder
    if _builder is None:
        _builder = ContextBuilder()
    return _builder
