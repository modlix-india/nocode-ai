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
    
    # Normalize all components - ensure they have required fields
    _normalize_components(merged.get("componentDefinition", {}))
    
    # Apply import mode styles to root component if available
    import_styles = outputs.get("_import_styles", {})
    if import_styles:
        _apply_import_root_styles(merged, import_styles)
    
    return merged


def _apply_import_root_styles(merged: Dict[str, Any], import_styles: Dict[str, Any]):
    """
    Apply exact CSS values to the root component in import mode.
    
    This ensures the root container has the correct background color and base styles
    from the original website.
    """
    root_key = merged.get("rootComponent")
    if not root_key:
        return
    
    component_def = merged.get("componentDefinition", {})
    root_comp = component_def.get(root_key)
    if not root_comp:
        return
    
    # Get exact styles from import
    bg_color = import_styles.get("backgroundColor", "")
    text_color = import_styles.get("textColor", "")
    font_family = import_styles.get("fontFamily", "")
    theme = import_styles.get("theme", "light")
    
    if not bg_color and not text_color:
        return
    
    logger.info(f"Applying import root styles: theme={theme}, bg={bg_color}, text={text_color}")
    
    # Ensure styleProperties exists
    if "styleProperties" not in root_comp:
        root_comp["styleProperties"] = {}
    
    if "rootStyle" not in root_comp["styleProperties"]:
        root_comp["styleProperties"]["rootStyle"] = {"resolutions": {"ALL": {}}}
    
    if "resolutions" not in root_comp["styleProperties"]["rootStyle"]:
        root_comp["styleProperties"]["rootStyle"]["resolutions"] = {"ALL": {}}
    
    if "ALL" not in root_comp["styleProperties"]["rootStyle"]["resolutions"]:
        root_comp["styleProperties"]["rootStyle"]["resolutions"]["ALL"] = {}
    
    all_styles = root_comp["styleProperties"]["rootStyle"]["resolutions"]["ALL"]
    
    # Apply exact values from import (override any existing)
    if bg_color:
        all_styles["backgroundColor"] = {"value": bg_color}
    if text_color:
        all_styles["color"] = {"value": text_color}
    if font_family:
        all_styles["fontFamily"] = {"value": font_family}
    
    # Ensure full viewport coverage
    all_styles["minHeight"] = {"value": "100vh"}
    all_styles["margin"] = {"value": "0"}
    
    logger.info(f"Root component '{root_key}' styled with: bg={bg_color}, text={text_color}")


def _normalize_components(component_def: Dict[str, Any]):
    """
    Normalize all component definitions to ensure they have required fields.
    
    - Ensures each component has a 'name' field (uses 'key' if missing)
    - Ensures each component has a 'key' field matching its dictionary key
    """
    for key, comp in component_def.items():
        if not isinstance(comp, dict):
            continue
        
        # Ensure 'key' field exists and matches the dictionary key
        if "key" not in comp or comp["key"] != key:
            comp["key"] = key
        
        # Ensure 'name' field exists (use key as fallback)
        if "name" not in comp or not comp["name"]:
            comp["name"] = key
            logger.debug(f"Added missing 'name' to component: {key}")


def _merge_components(
    component_def: Dict[str, Any],
    components: Dict[str, Any]
):
    """Merge component definitions into the flat componentDefinition map.

    Also handles 'parent' field: if a component specifies a parent,
    we add it to that parent's children automatically AND remove it from
    any previous parent.

    Children strategy:
    - When AI specifies children for an existing component, REPLACE them entirely
    - When a child moves to a new parent, remove it from the old parent
    """
    if not isinstance(components, dict):
        logger.warning(f"components is not a dict: {type(components)}")
        return

    # Track parent-child relationships to apply after all components are added
    parent_child_pairs: list = []

    for key, comp in components.items():
        if not isinstance(comp, dict):
            logger.warning(f"Component {key} is not a dict: {type(comp)}")
            continue

        # Check for parent relationship before merging
        parent_key = comp.pop("parent", None) if "parent" in comp else None
        if parent_key:
            parent_child_pairs.append((parent_key, key))

        if key not in component_def:
            # New component - add it directly
            component_def[key] = deepcopy(comp)
            logger.debug(f"Added new component: {key}")
        else:
            # Existing component - merge properties
            if "name" in comp:
                component_def[key]["name"] = comp["name"]
            if "type" in comp:
                component_def[key]["type"] = comp["type"]
            if "properties" in comp:
                component_def[key]["properties"] = component_def[key].get("properties", {})
                _deep_merge(component_def[key]["properties"], comp["properties"])
            if "children" in comp:
                # REPLACE children when AI specifies them
                old_children = list(component_def[key].get("children", {}).keys())
                new_children = list(comp["children"].keys())
                component_def[key]["children"] = deepcopy(comp["children"])
                logger.info(f"Replaced children of {key}: old={old_children}, new={new_children}")

    # Apply parent-child relationships
    for parent_key, child_key in parent_child_pairs:
        # First, remove child from any existing parent (cleanup old relationship)
        _remove_child_from_all_parents(component_def, child_key, exclude_parent=parent_key)

        # Then add to new parent
        if parent_key in component_def:
            if "children" not in component_def[parent_key]:
                component_def[parent_key]["children"] = {}
            component_def[parent_key]["children"][child_key] = True
            logger.debug(f"Added {child_key} as child of {parent_key}")
        else:
            logger.warning(f"Parent component '{parent_key}' not found for child '{child_key}'")


def _remove_child_from_all_parents(
    component_def: Dict[str, Any],
    child_key: str,
    exclude_parent: Optional[str] = None
):
    """Remove a child key from all parent components' children dict.

    This is used when a component moves to a new parent - we need to
    remove it from its old parent(s) to avoid orphan references.

    Args:
        component_def: The flat component definition dict
        child_key: The key of the child to remove
        exclude_parent: Don't remove from this parent (the new parent)
    """
    for comp_key, comp in component_def.items():
        if comp_key == exclude_parent:
            continue
        if not isinstance(comp, dict):
            continue
        children = comp.get("children", {})
        if child_key in children:
            del children[child_key]
            logger.info(f"Removed {child_key} from old parent {comp_key}")


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
    """
    Apply event handlers to components in flat componentDefinition.
    
    Ensures onClick and other event properties follow the format:
    "onClick": {"value": "eventFunctionKey"}
    
    If handlers is an array, takes the first element.
    """
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
            # Extract event function key from handlers
            # Handlers can be:
            # - A string: "eventFunctionKey"
            # - An array: ["eventFunctionKey"] or ["event1", "event2"]
            # - Already in format: {"value": "eventFunctionKey"}
            
            event_key = None
            
            if isinstance(handlers, str):
                # Direct string
                event_key = handlers
            elif isinstance(handlers, list):
                # Array - take first element
                if len(handlers) > 0:
                    event_key = str(handlers[0])
                else:
                    logger.warning(f"Empty handlers array for {key}.{event_name}")
                    continue
            elif isinstance(handlers, dict):
                # Already in format - check if it has "value"
                if "value" in handlers:
                    value = handlers["value"]
                    if isinstance(value, str):
                        event_key = value
                    elif isinstance(value, list) and len(value) > 0:
                        event_key = str(value[0])
                    else:
                        event_key = str(value)
                else:
                    logger.warning(f"Handlers dict for {key}.{event_name} missing 'value' key")
                    continue
            else:
                # Convert to string
                event_key = str(handlers)
            
            # Set the event property in the correct format
            comp["properties"][event_name] = {"value": event_key}


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
    """Deep merge source into target.

    This function recursively merges dictionaries, preserving existing values
    and adding new ones. Used for merging properties, styles, etc.

    Note: Children merging is handled explicitly in _merge_components with
    the _replaceChildren flag for explicit control.
    """
    if not isinstance(source, dict):
        logger.warning(f"Cannot merge non-dict source: {type(source)}")
        return

    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)
