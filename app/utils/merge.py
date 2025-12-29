"""Merge agent outputs into a single page definition

IMPORTANT: Nocode page structure:
- `rootComponent`: string key of the root component (e.g., "pageRoot")
- `componentDefinition`: flat dict mapping component keys to component objects
- `children`: dict of { "childKey": true } - references, NOT nested objects
"""
from typing import Dict, Any, Optional, Union
from copy import deepcopy
import logging

logger = logging.getLogger(__name__)


def merge_agent_outputs(
    outputs: Dict[str, Dict[str, Any]],
    existing_page: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Merge outputs from all specialist agents into a single page definition.
    
    Priority order for conflicts:
    1. Layout (structure is foundational)
    2. Component (component choices)
    3. Data (binding paths)
    4. Events (event handlers)
    5. Styles (visual styling)
    6. Animation (animations layer on top)
    
    Args:
        outputs: Dict mapping agent name to their output
        existing_page: Optional existing page to merge into
    
    Returns:
        Merged page definition
    """
    merged = {}
    
    # Start with existing page if modifying
    if existing_page:
        merged = deepcopy(existing_page)
    
    # Ensure componentDefinition exists
    if "componentDefinition" not in merged:
        merged["componentDefinition"] = {}
    
    # 1. Merge layout structure (foundational)
    layout_output = outputs.get("layout", {})
    if "componentDefinition" in layout_output:
        # Layout agent returns new components - merge them
        for key, comp in layout_output["componentDefinition"].items():
            if key not in merged["componentDefinition"]:
                merged["componentDefinition"][key] = deepcopy(comp)
            else:
                # Merge with existing
                _deep_merge(merged["componentDefinition"][key], comp)
    
    if "rootComponent" in layout_output:
        merged["rootComponent"] = layout_output["rootComponent"]
    
    # 2. Merge component properties
    component_output = outputs.get("component", {})
    if "components" in component_output:
        _merge_components(merged["componentDefinition"], component_output["components"])
    
    # 3. Merge data bindings
    data_output = outputs.get("data", {})
    if data_output:
        # Store initialization
        if "storeInitialization" in data_output:
            merged["properties"] = merged.get("properties", {})
            merged["properties"]["storeInitialization"] = data_output["storeInitialization"]
        
        # Component bindings
        if "componentBindings" in data_output:
            _apply_bindings(merged["componentDefinition"], data_output["componentBindings"])
    
    # 4. Merge event functions
    events_output = outputs.get("events", {})
    if "eventFunctions" in events_output:
        merged["eventFunctions"] = merged.get("eventFunctions", {})
        merged["eventFunctions"].update(events_output["eventFunctions"])
    
    # Apply component events
    if "componentEvents" in events_output:
        _apply_events(merged["componentDefinition"], events_output["componentEvents"])
    
    # 5. Merge styles
    styles_output = outputs.get("styles", {})
    if "componentStyles" in styles_output:
        logger.info(f"Applying styles to components: {list(styles_output['componentStyles'].keys())}")
        _apply_styles(merged["componentDefinition"], styles_output["componentStyles"])
    
    # 6. Merge animations (layer on top of styles)
    animation_output = outputs.get("animation", {})
    if "componentAnimations" in animation_output:
        logger.info(f"Applying animations to components: {list(animation_output['componentAnimations'].keys())}")
        _apply_styles(merged["componentDefinition"], animation_output["componentAnimations"])
    
    # Add keyframe animations if present
    if "keyframeAnimations" in animation_output:
        merged["cssStyles"] = merged.get("cssStyles", "")
        for name, keyframes in animation_output["keyframeAnimations"].items():
            merged["cssStyles"] += f"\n{keyframes}"
    
    # Log summary of changes
    if existing_page:
        orig_def = existing_page.get("componentDefinition", {})
        merged_def = merged.get("componentDefinition", {})
        changed = []
        for key in merged_def:
            if key in orig_def:
                if str(merged_def[key].get("styleProperties")) != str(orig_def[key].get("styleProperties")):
                    changed.append(key)
        logger.info(f"[Merge Summary] Components with style changes: {changed}")
    
    return merged


def _merge_components(
    component_def: Dict[str, Any],
    components: Dict[str, Any]
):
    """Merge component definitions into the flat componentDefinition map"""
    if not isinstance(components, dict):
        logger.warning(f"components is not a dict: {type(components)}")
        return
    
    for key, comp in components.items():
        if not isinstance(comp, dict):
            logger.warning(f"Component {key} is not a dict: {type(comp)}")
            continue
        
        if key not in component_def:
            component_def[key] = deepcopy(comp)
        else:
            # Merge properties
            if "name" in comp:
                component_def[key]["name"] = comp["name"]
            if "type" in comp:
                component_def[key]["type"] = comp["type"]
            if "properties" in comp:
                component_def[key]["properties"] = component_def[key].get("properties", {})
                _deep_merge(component_def[key]["properties"], comp["properties"])
            if "children" in comp:
                component_def[key]["children"] = component_def[key].get("children", {})
                component_def[key]["children"].update(comp["children"])


def _apply_bindings(
    component_def: Dict[str, Any],
    bindings: Dict[str, Any]
):
    """Apply data bindings to components in flat componentDefinition"""
    if not isinstance(bindings, dict):
        logger.warning(f"bindings is not a dict: {type(bindings)}")
        return
    
    for key, binding in bindings.items():
        if key not in component_def:
            logger.warning(f"Binding for unknown component: {key}")
            continue
        
        if not isinstance(binding, dict):
            logger.warning(f"Binding for {key} is not a dict: {type(binding)}")
            continue
        
        comp = component_def[key]
        comp["properties"] = comp.get("properties", {})
        
        # Apply binding path
        if "bindingPath" in binding:
            component_name = comp.get("type", comp.get("name", "")).lower()
            prop_group = _get_property_group(component_name)
            if prop_group:
                comp["properties"][prop_group] = comp["properties"].get(prop_group, {})
                comp["properties"][prop_group]["bindingPath"] = {"value": binding["bindingPath"]}
        
        # Apply visibility
        if "visibility" in binding:
            comp["properties"]["visibility"] = {"value": binding["visibility"]}
        
        # Apply disabled
        if "disabled" in binding:
            comp["properties"]["disabled"] = {"value": binding["disabled"]}


def _apply_events(
    component_def: Dict[str, Any],
    component_events: Dict[str, Any]
):
    """Apply event handlers to components in flat componentDefinition"""
    if not isinstance(component_events, dict):
        logger.warning(f"component_events is not a dict: {type(component_events)}")
        return
    
    for key, events in component_events.items():
        if key not in component_def:
            logger.warning(f"Events for unknown component: {key}")
            continue
        
        if not isinstance(events, dict):
            logger.warning(f"Events for {key} is not a dict: {type(events)}")
            continue
        
        comp = component_def[key]
        comp["properties"] = comp.get("properties", {})
        
        for event_name, handlers in events.items():
            comp["properties"][event_name] = {"value": handlers}


def _apply_styles(
    component_def: Dict[str, Any],
    component_styles: Dict[str, Any]
):
    """
    Apply styles to components in flat componentDefinition.
    
    The style structure uses resolutions for responsive design:
    {
        "<componentKey>": {
            "<styleId>": {
                "resolutions": {
                    "ALL": {
                        "property": { "value": "..." },
                        "property:hover": { "value": "..." }
                    },
                    "MOBILE_POTRAIT_SCREEN": { ... }
                }
            }
        }
    }
    """
    logger.info(f"[_apply_styles] Called with {len(component_styles) if isinstance(component_styles, dict) else 0} style targets")
    if isinstance(component_styles, dict):
        logger.info(f"[_apply_styles] Style targets: {list(component_styles.keys())}")
        logger.info(f"[_apply_styles] Available components: {list(component_def.keys())[:10]}...")
    if not isinstance(component_styles, dict):
        logger.warning(f"component_styles is not a dict: {type(component_styles)}")
        return
    
    for key, styles in component_styles.items():
        if key not in component_def:
            logger.warning(f"Styles for unknown component: {key}")
            continue
        
        if not isinstance(styles, dict):
            logger.warning(f"Styles for {key} is not a dict: {type(styles)}")
            continue
        
        comp = component_def[key]
        
        # Check if it's the new format (has resolution-style entries)
        if _is_new_style_format(styles):
            # New format: styles is a dict of styleId -> { resolutions: {...} }
            comp["styleProperties"] = comp.get("styleProperties", {})
            logger.info(f"Merging new-format styles for {key}: {list(styles.keys())}")
            before_styles = str(comp.get("styleProperties", {}))[:200]
            _deep_merge(comp["styleProperties"], styles)
            after_styles = str(comp.get("styleProperties", {}))[:200]
            logger.info(f"  Before: {before_styles}")
            logger.info(f"  After: {after_styles}")
        else:
            # Legacy format: has "styleProperties" and "stylePropertiesWithPseudoStates"
            if "styleProperties" in styles:
                comp["styleProperties"] = comp.get("styleProperties", {})
                sp = styles["styleProperties"]
                if isinstance(sp, dict):
                    _deep_merge(comp["styleProperties"], sp)
            
            if "stylePropertiesWithPseudoStates" in styles:
                comp["stylePropertiesWithPseudoStates"] = comp.get("stylePropertiesWithPseudoStates", {})
                spps = styles["stylePropertiesWithPseudoStates"]
                if isinstance(spps, dict):
                    _deep_merge(comp["stylePropertiesWithPseudoStates"], spps)


def _is_new_style_format(styles: Any) -> bool:
    """
    Check if the styles dict uses the new resolution-based format.
    
    New format has entries like:
    { "<styleId>": { "resolutions": { "ALL": { ... } } } }
    
    Old format has:
    { "styleProperties": { ... }, "stylePropertiesWithPseudoStates": { ... } }
    """
    if not styles or not isinstance(styles, dict):
        return False
    
    # If it has "styleProperties" or "stylePropertiesWithPseudoStates" as top-level keys, it's old format
    if "styleProperties" in styles or "stylePropertiesWithPseudoStates" in styles:
        return False
    
    # Check if any value has "resolutions" key - indicates new format
    for value in styles.values():
        if isinstance(value, dict) and "resolutions" in value:
            return True
    
    return False


def _get_property_group(component_name: str) -> Optional[str]:
    """Get the property group name for a component type"""
    mapping = {
        "textbox": "textBox",
        "password": "password",
        "checkbox": "checkbox",
        "dropdown": "dropdown",
        "radiobutton": "radioButton",
        "text": "text",
        "button": "button",
        "image": "image",
        "link": "link",
    }
    return mapping.get(component_name.lower())


def _deep_merge(target: Dict, source: Any):
    """Deep merge source into target"""
    if not isinstance(source, dict):
        logger.warning(f"Cannot merge non-dict source: {type(source)}")
        return
    
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)
