"""HTML to Nocode conversion utilities for website import"""
import re
import uuid
import hashlib
import base64
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class HtmlToNocodeConverter:
    """
    Converts extracted visual data from websites to Nocode page definitions.
    Provides 1:1 mapping of HTML elements to Nocode components with full CSS preservation.
    """

    def convert_visual_to_nocode(
        self,
        visual_data,
        uploaded_images: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Directly convert VisualElement tree to Nocode componentDefinition.
        No LLM involved - pure 1:1 mapping of elements to components.
        """
        component_def = {}
        root_children = {}

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

        key_counter = [0]
        images_converted = []

        def generate_key(base: str) -> str:
            key_counter[0] += 1
            clean = re.sub(r'[^a-zA-Z0-9]', '', base)[:20]
            return f"{clean}_{key_counter[0]}"

        def convert_element(elem, parent_children: Dict, display_order: int) -> None:
            tag = elem.tag.lower()
            has_children = len(elem.children) > 0

            TEXT_TAGS = {'span', 'strong', 'em', 'b', 'i', 'p', 'text', 'label'}
            children_are_text_only = all(
                child.tag.lower() in TEXT_TAGS for child in elem.children
            ) if has_children else True

            comp_type = self._tag_to_component_type(tag, has_children, children_are_text_only)
            elem_key = generate_key(elem.id or tag)
            properties = {}
            needs_text_child = False
            text_for_child = ""

            if tag == "a" and has_children:
                href = elem.attributes.get("href", "")
                if href:
                    properties["linkPath"] = {"value": href}
                if elem.text and elem.text.strip():
                    needs_text_child = True
                    text_for_child = elem.text.strip()

            if comp_type == "Text":
                properties["text"] = {"value": elem.text or ""}
            elif comp_type == "Button":
                properties["label"] = {"value": elem.text.strip() if elem.text else ""}
                properties["designType"] = {"value": "_text"}
                properties["colorScheme"] = {"value": "_secondary"}
            elif comp_type == "Link":
                properties["label"] = {"value": elem.text or ""}
                properties["linkPath"] = {"value": elem.attributes.get("href", "")}
            elif comp_type == "Image":
                original_src = elem.image_url or elem.attributes.get("src", "")
                if original_src.startswith("data:image/svg+xml"):
                    src = original_src
                else:
                    src = uploaded_images.get(original_src, original_src)
                properties["src"] = {"value": src}
                properties["alt"] = {"value": elem.attributes.get("alt", "")}
                images_converted.append({"key": elem_key, "original": original_src[:50] if original_src else "EMPTY"})
            elif comp_type == "TextBox":
                properties["placeholder"] = {"value": elem.attributes.get("placeholder", "")}

            style_props = self._build_element_styles(elem, comp_type)

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

            child_display_order = 0

            if needs_text_child and text_for_child:
                text_child_key = generate_key(f"{elem_key}_text")
                text_child = {
                    "key": text_child_key,
                    "name": f"{elem_key.replace('_', ' ').title()} Text",
                    "type": "Text",
                    "displayOrder": child_display_order,
                    "properties": {"text": {"value": text_for_child}},
                    "styleProperties": {},
                    "override": False,
                    "children": {}
                }
                component["children"][text_child_key] = True
                component_def[text_child_key] = text_child
                child_display_order += 1

            for child in elem.children:
                convert_element(child, component["children"], child_display_order)
                child_display_order += 1

        for idx, elem in enumerate(visual_data.elements):
            convert_element(elem, root_children, idx)

        logger.info(f"Converted {len(component_def)} components, {len(images_converted)} images")
        return {"rootComponent": root_key, "componentDefinition": component_def}

    def _tag_to_component_type(self, tag: str, has_children: bool = False, children_are_text_only: bool = True) -> str:
        """Map HTML tag to Nocode component type."""
        tag_lower = tag.lower()
        if tag_lower == "a" and has_children:
            return "Grid"
        if tag_lower == "button" and has_children and not children_are_text_only:
            return "Grid"
        # li with complex children (links, images, etc.) should be a Grid container
        if tag_lower == "li" and has_children and not children_are_text_only:
            return "Grid"

        tag_map = {
            "h1": "Text", "h2": "Text", "h3": "Text", "h4": "Text", "h5": "Text", "h6": "Text",
            "p": "Text", "span": "Text", "label": "Text", "li": "Text",
            "strong": "Text", "b": "Text", "em": "Text", "i": "Text",
            "button": "Button", "a": "Link",
            "img": "Image", "svg": "Image", "path": "Grid",
            "input": "TextBox", "textarea": "TextArea", "select": "Dropdown",
            "div": "Grid", "section": "Grid", "article": "Grid", "main": "Grid",
            "header": "Grid", "footer": "Grid", "nav": "Grid", "aside": "Grid",
            "form": "Grid", "ul": "Grid", "ol": "Grid", "figure": "Grid",
        }
        return tag_map.get(tag_lower, "Grid")

    def _build_element_styles(self, elem, comp_type: str) -> Dict[str, Any]:
        """Build responsive styles from extracted CSS at all viewports."""
        style_props = {}
        desktop = elem.styles.get("desktop", {})
        tablet = elem.styles.get("tablet", {})
        mobile = elem.styles.get("mobile", {})

        # Debug logging for responsive styles - use INFO level for visibility
        elem_id_short = elem.id[:30] if elem.id else 'unknown'
        logger.info(f"[StylesBuild] Element {elem_id_short} ({comp_type}): desktop={len(desktop)}, tablet={len(tablet)}, mobile={len(mobile)}")

        # Check if tablet/mobile differ from desktop
        if tablet:
            tablet_diffs = [k for k in tablet if tablet.get(k) != desktop.get(k)]
            if tablet_diffs:
                logger.info(f"[StylesBuild]   Tablet differs: {tablet_diffs[:5]}")
        if mobile:
            mobile_diffs = [k for k in mobile if mobile.get(k) != desktop.get(k)]
            if mobile_diffs:
                logger.info(f"[StylesBuild]   Mobile differs: {mobile_diffs[:5]}")

        CONTAINER_PROPS = {
            "position", "top", "right", "bottom", "left", "zIndex",
            "margin", "marginTop", "marginRight", "marginBottom", "marginLeft",
            "display", "opacity", "transform", "visibility",
            "width", "height", "maxWidth", "maxHeight", "minWidth", "minHeight"
        }
        IMAGE_ELEMENT_PROPS = {
            "objectFit", "objectPosition", "borderRadius",
            "borderTopLeftRadius", "borderTopRightRadius",
            "borderBottomLeftRadius", "borderBottomRightRadius"
        }

        root_styles = {}

        if comp_type == "Grid":
            GRID_RESET_PROPS = {"flexDirection": "row", "flexWrap": "nowrap", "gap": "0px", "position": "static"}
            for prop, default_val in GRID_RESET_PROPS.items():
                css_prop = self._nocode_to_css_prop(prop)
                extracted_val = desktop.get(css_prop) or desktop.get(prop)
                root_styles[prop] = {"value": self._process_css_value(extracted_val) if extracted_val else default_val}

        for prop, value in desktop.items():
            if not value:
                continue
            nocode_prop = self._css_to_nocode_prop(prop)
            processed = self._process_css_value(value)
            if not processed:
                continue
            if comp_type == "Image" and nocode_prop in IMAGE_ELEMENT_PROPS:
                root_styles[f"image-{nocode_prop}"] = {"value": processed}
            elif comp_type == "Image" and nocode_prop not in CONTAINER_PROPS:
                continue
            else:
                root_styles[nocode_prop] = {"value": processed}

        if comp_type == "Image":
            pos = root_styles.get("position", {}).get("value", "")
            if pos == "absolute":
                top = root_styles.get("top", {}).get("value", "")
                left = root_styles.get("left", {}).get("value", "")
                right = root_styles.get("right", {}).get("value", "")
                bottom = root_styles.get("bottom", {}).get("value", "")
                edges_are_zero = all(e in ("0", "0px", "0%") for e in [top, left, right, bottom] if e)
                if edges_are_zero and top and left:
                    root_styles["image-width"] = {"value": "100%"}
                    root_styles["image-height"] = {"value": "100%"}

        resolutions = {"ALL": root_styles} if root_styles else {}

        # Build tablet diff: compare tablet against desktop
        tablet_diff = self._build_diff_styles_for_type(desktop, tablet, comp_type, CONTAINER_PROPS, IMAGE_ELEMENT_PROPS)
        if tablet_diff:
            resolutions["TABLET_LANDSCAPE_SCREEN_SMALL"] = tablet_diff
            logger.info(f"[Responsive] Added TABLET styles for {elem_id_short}: {list(tablet_diff.keys())}")
        elif tablet:
            # Log why no diff was found even though tablet styles exist
            raw_diffs = [k for k in tablet if tablet.get(k) != desktop.get(k)]
            if raw_diffs:
                logger.info(f"[Responsive] Tablet raw diffs exist but filtered out for {elem_id_short}: {raw_diffs[:3]}")

        # Build mobile diff: compare mobile against the effective styles (desktop + tablet overrides)
        # This ensures mobile captures changes from tablet, not just from desktop
        effective_tablet = {**desktop, **tablet}
        mobile_diff = self._build_diff_styles_for_type(effective_tablet, mobile, comp_type, CONTAINER_PROPS, IMAGE_ELEMENT_PROPS)
        if mobile_diff:
            resolutions["MOBILE_LANDSCAPE_SCREEN_SMALL"] = mobile_diff
            logger.info(f"[Responsive] Added MOBILE styles for {elem_id_short}: {list(mobile_diff.keys())}")
        elif mobile:
            # Log why no diff was found even though mobile styles exist
            raw_diffs = [k for k in mobile if mobile.get(k) != effective_tablet.get(k)]
            if raw_diffs:
                logger.info(f"[Responsive] Mobile raw diffs exist but filtered out for {elem_id_short}: {raw_diffs[:3]}")

        style_key = self._generate_style_key(elem.id)
        if resolutions:
            style_props[style_key] = {"resolutions": resolutions}
        return style_props

    def _build_diff_styles_for_type(self, base: Dict, current: Dict, comp_type: str, container_props: set, image_props: set) -> Dict[str, Any]:
        """Build diff styles with proper prefix handling.

        Compares current viewport styles against base and returns only the differences.
        Also includes properties that exist in current but not in base.
        """
        diff_styles = {}

        # Get all properties from current viewport
        for prop, value in current.items():
            if not value:
                continue

            base_value = base.get(prop)

            # Include if: value exists AND (base doesn't have it OR values differ)
            if value != base_value:
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed and processed.lower() not in {"initial", "inherit", "unset"}:
                    if comp_type == "Image" and nocode_prop in image_props:
                        diff_styles[f"image-{nocode_prop}"] = {"value": processed}
                    elif comp_type == "Image" and nocode_prop not in container_props:
                        continue
                    else:
                        diff_styles[nocode_prop] = {"value": processed}

        return diff_styles

    def _generate_style_key(self, element_id: str) -> str:
        """Generate a unique style key for a component."""
        if element_id:
            hash_bytes = hashlib.md5(element_id.encode()).digest()
            return base64.urlsafe_b64encode(hash_bytes)[:22].decode()
        return str(uuid.uuid4()).replace("-", "")[:22]

    def _create_responsive_styles(self, viewport_styles: Dict, default_styles: Dict = None) -> Dict[str, Any]:
        """Create Nocode styleProperties with responsive resolutions."""
        resolutions = {}
        desktop_styles = viewport_styles.get("desktop", {})
        tablet_styles = viewport_styles.get("tablet", {})
        mobile_styles = viewport_styles.get("mobile", {})
        all_styles = {}

        if default_styles:
            for prop, value in default_styles.items():
                all_styles[prop] = {"value": value}

        for prop, value in desktop_styles.items():
            if value and prop != "theme":
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed:
                    all_styles[nocode_prop] = {"value": processed}

        resolutions["ALL"] = all_styles

        # Build tablet diff: compare against desktop
        tablet_resolutions = self._build_viewport_diff(desktop_styles, tablet_styles)
        if tablet_resolutions:
            resolutions["TABLET_LANDSCAPE_SCREEN_SMALL"] = tablet_resolutions

        # Build mobile diff: compare against effective styles (desktop + tablet overrides)
        effective_tablet = {**desktop_styles, **tablet_styles}
        mobile_resolutions = self._build_viewport_diff(effective_tablet, mobile_styles)
        if mobile_resolutions:
            resolutions["MOBILE_LANDSCAPE_SCREEN_SMALL"] = mobile_resolutions

        style_key = self._generate_style_key("pageRoot")
        return {style_key: {"resolutions": resolutions}}

    def _build_viewport_diff(self, base_styles: Dict, current_styles: Dict) -> Dict[str, Any]:
        """Build diff between two viewport style dictionaries."""
        diff = {}
        for prop, value in current_styles.items():
            if value and value != base_styles.get(prop) and prop != "theme":
                nocode_prop = self._css_to_nocode_prop(prop)
                processed = self._process_css_value(value)
                if processed:
                    diff[nocode_prop] = {"value": processed}
        return diff

    def _css_to_nocode_prop(self, css_prop: str) -> str:
        return css_prop

    def _nocode_to_css_prop(self, nocode_prop: str) -> str:
        return re.sub(r'([A-Z])', r'-\1', nocode_prop).lower()

    def _process_css_value(self, value: str) -> str:
        return value if value else ""

    def serialize_elements(self, elements: List) -> List[Dict[str, Any]]:
        """Serialize VisualElement objects to dicts for JSON."""
        result = []
        for elem in elements:
            result.append({
                "id": elem.id, "tag": elem.tag, "text": elem.text,
                "imageUrl": elem.image_url, "styles": elem.styles,
                "bounds": elem.bounds, "attributes": elem.attributes,
                "children": self.serialize_elements(elem.children)
            })
        return result


_converter = None

def get_html_to_nocode_converter() -> HtmlToNocodeConverter:
    global _converter
    if _converter is None:
        _converter = HtmlToNocodeConverter()
    return _converter
