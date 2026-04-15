"""Section builder — converts extracted element tree to Modlix componentDefinition.

Programmatic conversion: walks the element tree from section_extractor and creates
Modlix components directly from the exact getComputedStyle values. No LLM needed
for structure — the extraction already has everything.

This is FAST and FAITHFUL — every text block, image, link from the source appears
in the output with its exact CSS values.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from app.agents.appbuilder.tools.section_extractor import SectionSpec

logger = logging.getLogger(__name__)

# HTML tag → Modlix component type
_TAG_MAP = {
    "h1": "Text", "h2": "Text", "h3": "Text", "h4": "Text", "h5": "Text", "h6": "Text",
    "p": "Text", "span": "Text", "label": "Text", "figcaption": "Text",
    "blockquote": "Text", "code": "Text", "pre": "Text", "li": "Text",
    "img": "Image", "picture": "Image",
    "button": "Button",
    "iframe": "Iframe",
    "video": "Video",
    "input": "TextBox", "textarea": "TextArea", "select": "Dropdown",
}

# Styles to skip (browser defaults / noise)
_SKIP_STYLES = {"_isRow"}

# Modlix resolution names for responsive viewports
_RESPONSIVE_MAP = {
    "tablet": "TABLET_LANDSCAPE_SCREEN_SMALL",
    "mobile": "MOBILE_LANDSCAPE_SCREEN_SMALL",
}


async def build_section(
    spec: SectionSpec,
    body_font: str = "",
) -> dict[str, Any]:
    """Convert a section's element tree to Modlix componentDefinition.

    Programmatic conversion — walks the tree and creates components with
    exact computed styles. No LLM calls.

    Args:
        spec: Complete section specification from extraction.
        body_font: Primary font family (for reference, not forced on every component).

    Returns:
        Dict of component key → component definition.
    """
    comp_def: dict[str, Any] = {}
    section_key = f"{spec.name}Section"

    if not spec.element_tree or spec.element_tree.get("error"):
        logger.warning("Section '%s' has no element tree — creating empty Grid", spec.name)
        comp_def[section_key] = _make_empty_grid(section_key, spec)
        return comp_def

    # Convert the root element of this section
    _convert_element(
        el=spec.element_tree,
        comp_def=comp_def,
        key_override=section_key,
        depth=0,
    )

    # Ensure section root has width:100% and section-level background
    if section_key in comp_def:
        sec_comp = comp_def[section_key]
        sp = sec_comp.get("styleProperties", {})
        if sp:
            first_sid = next(iter(sp))
            all_res = sp[first_sid].setdefault("resolutions", {}).setdefault("ALL", {})
            # Always full width
            if "width" not in all_res:
                all_res["width"] = {"value": "100%"}
            else:
                # Fixed-width section (e.g. width: 720px) — add max-width: 100%
                # so it doesn't overflow when viewport is narrower than the fixed width.
                # Root has align-items:center so this section will still center.
                w_val = all_res["width"].get("value", "") if isinstance(all_res["width"], dict) else ""
                if "px" in w_val and "maxWidth" not in all_res:
                    all_res["maxWidth"] = {"value": "100%"}
            # Apply section background from spec if not already set
            if spec.bg_color and spec.bg_color != "rgba(0, 0, 0, 0)" and "backgroundColor" not in all_res:
                all_res["backgroundColor"] = {"value": spec.bg_color}
            if spec.bg_image and "backgroundImage" not in all_res:
                all_res["backgroundImage"] = {"value": spec.bg_image}
                if "backgroundSize" not in all_res:
                    all_res["backgroundSize"] = {"value": "cover"}
                if "backgroundPosition" not in all_res:
                    all_res["backgroundPosition"] = {"value": "center"}

            # Ensure section height matches the source bounding rect.
            # The extracted CSS height may be "auto" (filtered as default) or
            # smaller than the visual rect (JS-driven dynamic content). Using
            # minHeight from the bounding rect guarantees vertical space.
            if spec.height > 50:
                existing_h = all_res.get("height", {})
                existing_hv = existing_h.get("value", "") if isinstance(existing_h, dict) else ""
                existing_minh = all_res.get("minHeight", {})
                existing_minhv = existing_minh.get("value", "") if isinstance(existing_minh, dict) else ""

                # If no explicit height or the CSS height is significantly
                # smaller than the bounding rect, set minHeight
                css_h_px = 0
                if existing_hv and "px" in existing_hv:
                    try:
                        css_h_px = float(existing_hv.replace("px", ""))
                    except ValueError:
                        pass
                css_minh_px = 0
                if existing_minhv and "px" in existing_minhv:
                    try:
                        css_minh_px = float(existing_minhv.replace("px", ""))
                    except ValueError:
                        pass

                rect_h = spec.height
                # If CSS height is significantly shorter than visual rect,
                # override the CSS height with the bounding rect height.
                # CSS height: Npx caps the element at N pixels even if content
                # is taller, so we need to replace it (not just add minHeight).
                if css_h_px > 0 and css_h_px < rect_h * 0.8:
                    all_res["height"] = {"value": f"{rect_h}px"}
                elif not existing_hv and css_minh_px < rect_h * 0.8:
                    all_res["minHeight"] = {"value": f"{rect_h}px"}

    # Apply responsive diffs
    if spec.responsive_diffs:
        _apply_responsive_diffs(comp_def, spec)

    logger.info("Section '%s' built: %d components programmatically", spec.name, len(comp_def))
    return comp_def


_key_counter = 0


def _gen_key(prefix: str) -> str:
    """Generate a unique component key."""
    global _key_counter
    _key_counter += 1
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _convert_element(
    el: dict,
    comp_def: dict[str, Any],
    key_override: str | None = None,
    depth: int = 0,
) -> str | None:
    """Strict 1:1 DOM → Modlix mapping.

    - Tag decides component type.
    - All extracted computed styles are passed through.
    - No layout inference (no ROWLAYOUT/THREECOLUMNSLAYOUT detection).
    - No width/height clamping.
    - No "empty Grid" pruning.
    - `_bare` on every component so Modlix defaults don't override us.
    """
    if not el or depth > 12:
        return None

    tag = el.get("tag", "div")
    # SVG subtree: Modlix can't render SVG shape primitives as components —
    # drop. The `<svg>` itself is converted to data-URI Image by extractor.
    if tag in ("svg", "path", "g", "defs", "clippath", "mask", "use", "symbol",
               "polygon", "polyline", "circle", "rect", "ellipse", "line"):
        return None

    styles = dict(el.get("styles", {}))  # copy so we don't mutate extraction
    styles.pop("_isRow", None)            # internal marker; row layout comes from
                                          # extracted `display:flex; flex-direction:row`
    text = el.get("text")
    src = el.get("src")
    href = el.get("href", "")
    alt = el.get("alt", "")
    children_data = el.get("children", [])

    # Tag → component type
    comp_type = _TAG_MAP.get(tag, "Grid")
    # Text-tag with children → Grid (children carry the text)
    if comp_type == "Text" and children_data and not text:
        comp_type = "Grid"
    if tag == "li" and children_data:
        comp_type = "Grid"
    if tag == "a":
        if children_data and not text:
            comp_type = "Grid"
        else:
            comp_type = "Text"

    # Key
    el_id = el.get("id", "")
    if key_override:
        key = key_override
    elif el_id:
        key = re.sub(r"\W", "_", el_id)[:30]
    else:
        key = _gen_key(tag[:3])
    while key in comp_def:
        key = key + "_" + uuid.uuid4().hex[:4]

    properties: dict[str, Any] = {}
    children_map: dict[str, bool] = {}

    # Type-specific required props
    if comp_type == "Image":
        # SVG data URIs are kept — the clone_tool will upload them to files
        # service and rewrite the src to a proper URL.
        if src:
            properties["src"] = {"value": src}
        if alt:
            properties["alt"] = {"value": alt}
    elif comp_type == "Text":
        all_text = text or _collect_all_text(el)
        if all_text:
            properties["text"] = {"value": all_text[:500]}
        elif not children_data:
            return None  # no text, no children → nothing to render
    elif comp_type == "Button":
        label = text or _collect_all_text(el)
        properties["label"] = {"value": label[:100] or "Button"}
    elif comp_type == "Iframe":
        if src:
            properties["src"] = {"value": src}
    elif comp_type == "Video":
        video_src = el.get("videoSrc", "")
        poster = el.get("poster", "")
        if video_src:
            properties["src"] = {"value": video_src}
        if poster:
            properties["poster"] = {"value": poster}
        if el.get("autoplay"):
            properties["autoPlay"] = {"value": True}
        if el.get("loop"):
            properties["loop"] = {"value": True}
        if el.get("muted") or el.get("autoplay"):
            properties["muted"] = {"value": True}  # autoplay needs muted
        # Mobile autoplay fidelity
        properties["playsInline"] = {"value": True}
        # If neither source nor poster, don't emit — empty Video renders nothing
        if not video_src and not poster:
            return None
    elif comp_type == "TextBox":
        placeholder = el.get("placeholder", "")
        if placeholder:
            properties["placeholder"] = {"value": placeholder}

    # Link href on Grid-wrapping <a>
    if href and tag == "a" and not href.startswith("javascript:") and comp_type == "Grid":
        properties["linkPath"] = {"value": href}

    # Recurse into children for container types
    if comp_type == "Grid":
        properties["containerType"] = {"value": "_bare"}

        # ── Carousel detection ──
        # Pattern: overflow:hidden container → single flex-row child → 3+ same-sized items
        # (some off-screen). Convert to Modlix Carousel component.
        _overflow = styles.get("overflow", "") or styles.get("overflowX", "")
        if _overflow == "hidden" and len(children_data) == 1:
            inner = children_data[0]
            inner_styles = inner.get("styles", {})
            inner_is_row = inner_styles.get("_isRow") or (
                inner_styles.get("display", "") in ("flex", "inline-flex")
                and inner_styles.get("flexDirection", "row") in ("row", "row-reverse")
            )
            inner_kids = inner.get("children", [])
            if inner_is_row and len(inner_kids) >= 3:
                # Check if items are similarly sized (carousel slides)
                rects = [k.get("rect", {}) for k in inner_kids if k.get("rect")]
                if rects:
                    heights = [r.get("h", 0) for r in rects]
                    widths = [r.get("w", 0) for r in rects]
                    avg_h = sum(heights) / len(heights) if heights else 0
                    avg_w = sum(widths) / len(widths) if widths else 0
                    # Similar sizes = all within 20% of average
                    similar = (avg_h > 50 and avg_w > 50 and
                               all(abs(h - avg_h) < avg_h * 0.2 for h in heights) and
                               all(abs(w - avg_w) < avg_w * 0.2 for w in widths))
                    # At least one item is off-screen (x > 1440)
                    has_offscreen = any(r.get("x", 0) > 1400 for r in rects)

                    if similar and has_offscreen:
                        # Convert to Carousel component
                        comp_type = "Carousel"
                        properties = {
                            "autoPlay": {"value": True},
                            "slideSpeed": {"value": 3000},
                            "animationType": {"value": "slide"},
                            "animationDuration": {"value": 500},
                            "showArrowButtons": {"value": True},
                            "arrowButtons": {"value": "RightTop"},
                            "showIndicators": {"value": False},
                        }
                        # Build children from the inner flex row's items
                        display_idx = 0
                        for slide_el in inner_kids:
                            slide_key = _convert_element(slide_el, comp_def, depth=depth + 1)
                            if slide_key:
                                comp_def[slide_key]["displayOrder"] = display_idx
                                children_map[slide_key] = True
                                display_idx += 1

                        # Build the Carousel component
                        sp = _build_style_properties(styles)
                        # Ensure carousel has correct dimensions
                        if sp:
                            first_sid = next(iter(sp))
                            ar = sp[first_sid].get("resolutions", {}).get("ALL", {})
                            ar.pop("overflow", None)  # Carousel handles its own overflow
                            ar.pop("overflowX", None)

                        comp_def[key] = {
                            "key": key, "name": key, "type": "Carousel",
                            "properties": properties,
                            "styleProperties": sp,
                            "children": children_map,
                            "displayOrder": 0,
                        }
                        logger.info("Carousel detected: %d slides at %dx%dpx",
                                    len(children_map), int(avg_w), int(avg_h))
                        return key

        # Modlix Grid applies a `_SINGLECOLUMNLAYOUT` class by default whose CSS
        # forces flex-direction: column. Set ROWLAYOUT when source CSS is a flex row.
        _disp = styles.get("display", "")
        _fd = styles.get("flexDirection", "row")
        if _disp in ("flex", "inline-flex") and _fd in ("row", "row-reverse"):
            properties["layout"] = {"value": "ROWLAYOUT"}

        # Auto-promote position:relative when any child is position:absolute,
        # so overlay coordinates (hero text on hero image, badges, etc.)
        # resolve against THIS container instead of the page root.
        if styles.get("position") not in ("absolute", "fixed", "relative", "sticky"):
            for ch in children_data:
                if (ch.get("styles") or {}).get("position") == "absolute":
                    styles["position"] = "relative"
                    break

        display_idx = 0
        # ::before pseudo-element as a synthetic Text/Image child
        pb = el.get("pseudoBefore")
        if pb:
            pb_key = _make_pseudo_child(pb, comp_def)
            if pb_key:
                comp_def[pb_key]["displayOrder"] = display_idx
                children_map[pb_key] = True
                display_idx += 1

        for child_el in children_data:
            child_key = _convert_element(child_el, comp_def, depth=depth + 1)
            if child_key:
                comp_def[child_key]["displayOrder"] = display_idx
                children_map[child_key] = True
                display_idx += 1

        # ::after pseudo-element
        pa = el.get("pseudoAfter")
        if pa:
            pa_key = _make_pseudo_child(pa, comp_def)
            if pa_key:
                comp_def[pa_key]["displayOrder"] = display_idx
                children_map[pa_key] = True
                display_idx += 1
        # Grid with direct text but no children → add a Text child carrying it
        if text and not children_map:
            txt_key = _gen_key("txt")
            comp_def[txt_key] = {
                "key": txt_key, "name": txt_key, "type": "Text",
                "properties": {
                    "text": {"value": text[:500]},
                    "designType": {"value": "_bare"},
                },
                "styleProperties": {}, "children": {}, "displayOrder": 0,
            }
            children_map[txt_key] = True

    # `_bare` designType on non-Grid components → no Modlix-default styling
    if comp_type in ("Text", "Button", "Image", "Iframe", "Video", "Link",
                     "TextBox", "TextArea", "Dropdown"):
        properties.setdefault("designType", {"value": "_bare"})

    style_props = _build_style_properties(styles)

    comp_def[key] = {
        "key": key,
        "name": key,
        "type": comp_type,
        "properties": properties,
        "styleProperties": style_props,
        "children": children_map,
        "displayOrder": 0,
    }
    return key


def _build_style_properties(styles: dict[str, str]) -> dict[str, Any]:
    """Convert a CSS styles dict to Modlix styleProperties format.

    Strict 1:1 — pass all extracted styles through. Only skip what genuinely
    breaks Modlix:
      - `background` shorthand (silently wipes out backgroundImage in Modlix)
      - `_isRow` / `_*` internal markers (not CSS)
    """
    if not styles:
        return {}

    # Per-side border coherence check.
    #
    # When applied as an INLINE style, `border-top-style: solid` with NO
    # `border-top-width` falls back to the browser default `medium` (~3px),
    # producing a visible ghost border. Our extractor filters out `0px` and
    # `none` via DEFAULTS, but that leaves orphaned `solid` style entries on
    # elements where the source had `border-top: 0 solid <color>` — common
    # when a site uses Tailwind's preflight (`border-style: solid` + `border-width: 0`).
    #
    # Rule: for each side, if its width is missing/zero, drop its style and color.
    def _zero_width(v: str) -> bool:
        if not v:
            return True
        vv = str(v).strip()
        if vv in ("0", "0px", "medium"):
            return True
        return False

    drop_sides: set[str] = set()
    for side in ("Top", "Right", "Bottom", "Left"):
        w = styles.get("border" + side + "Width", "")
        if _zero_width(w):
            drop_sides.add(side)

    # Shorthand `borderWidth` — if it decomposes to all-zero, nothing to draw
    shorthand_w = styles.get("borderWidth", "")
    shorthand_zero = shorthand_w and all(_zero_width(x) for x in shorthand_w.split())
    drop_shorthand_borders = len(drop_sides) == 4 or shorthand_zero

    zero_side_props: set[str] = set()
    for side in drop_sides:
        zero_side_props.add("border" + side + "Width")
        zero_side_props.add("border" + side + "Style")
        zero_side_props.add("border" + side + "Color")

    # Outline coherence: drop the shorthand and its companions when no visible
    # outline is authored. A common pattern from normalize.css / radix / tailwind
    # is `outline: <color> none 0px` which is a no-op BUT pollutes every
    # component's styleProperties, and some renderers interpret it as `outline: medium`.
    outline_val = styles.get("outline", "")
    outline_width = styles.get("outlineWidth", "")
    outline_style = styles.get("outlineStyle", "")
    drop_outline = False
    if outline_val:
        ov = str(outline_val)
        if " none " in f" {ov} " or ov.endswith(" 0px") or ov.endswith(" 0") \
                or " 0px " in f" {ov} ":
            drop_outline = True
    if outline_width in ("0", "0px") or outline_style in ("none", "hidden"):
        drop_outline = True
    if drop_outline:
        # Outline has no visible effect; drop all outline-* props so Modlix
        # doesn't inline `outline: color none 0px` which can mis-render.
        zero_side_props.update({
            "outline", "outlineColor", "outlineStyle", "outlineWidth",
            "outlineOffset",
        })

    clean = {}
    for prop, val in styles.items():
        if not val:
            continue
        if prop.startswith("_"):          # internal markers
            continue
        if prop == "background":           # shorthand wipes backgroundImage
            continue
        # Drop per-side style/color where that side has zero/missing width
        if prop in zero_side_props:
            continue
        # Drop shorthand border props when all sides have zero width
        if drop_shorthand_borders and prop in (
            "border", "borderStyle", "borderColor", "borderWidth"
        ):
            continue
        # Drop explicit none/hidden border styles (no visible effect)
        if (prop.startswith("border") and prop.endswith("Style")
                and val in ("none", "hidden")):
            continue
        # Drop explicit zero widths
        if (prop.startswith("border") and prop.endswith("Width")
                and val in ("0", "0px")):
            continue
        clean[prop] = val

    if not clean:
        return {}

    style_id = uuid.uuid4().hex[:22]
    return {
        style_id: {
            "resolutions": {
                "ALL": {prop: {"value": str(val)} for prop, val in clean.items()}
            }
        }
    }


def _parse_px(val: str) -> float:
    """Parse a pixel value string to float."""
    try:
        return float(val.replace("px", ""))
    except (ValueError, AttributeError):
        return 0


def _make_pseudo_child(pseudo: dict, comp_def: dict[str, Any]) -> str | None:
    """Turn a ::before / ::after pseudo-element spec into a Text or Image child.

    Handles:
      - type=text: icon-font glyphs (FontAwesome, Material Icons), quote marks,
        separators. The content string is the computed ::before content value.
      - type=image: url(...) pseudo-backgrounds (logo marks, decorators).
    """
    if not pseudo:
        return None
    ptype = pseudo.get("type")
    styles = pseudo.get("styles", {}) or {}

    if ptype == "image":
        src = pseudo.get("src", "")
        if not src:
            return None
        key = _gen_key("pse")
        comp_def[key] = {
            "key": key, "name": key, "type": "Image",
            "properties": {
                "src": {"value": src},
                "alt": {"value": ""},
                "designType": {"value": "_bare"},
            },
            "styleProperties": _build_style_properties(styles),
            "children": {}, "displayOrder": 0,
        }
        return key

    content = pseudo.get("content", "")
    if not content.strip():
        return None
    key = _gen_key("pse")
    comp_def[key] = {
        "key": key, "name": key, "type": "Text",
        "properties": {
            "text": {"value": content[:200]},
            "designType": {"value": "_bare"},
        },
        "styleProperties": _build_style_properties(styles),
        "children": {}, "displayOrder": 0,
    }
    return key


def _collect_all_text(el: dict) -> str:
    """Recursively collect all text from an element and its children."""
    parts = []
    text = el.get("text", "")
    if text:
        parts.append(text)
    for child in el.get("children", []):
        child_text = _collect_all_text(child)
        if child_text:
            parts.append(child_text)
    return " ".join(parts)[:500]


def _make_empty_grid(key: str, spec: SectionSpec) -> dict[str, Any]:
    """Create an empty Grid component for a section that failed extraction."""
    styles = {}
    if spec.bg_color and spec.bg_color != "rgba(0, 0, 0, 0)":
        styles["backgroundColor"] = spec.bg_color
    if spec.bg_image:
        styles["backgroundImage"] = spec.bg_image
    styles["width"] = "100%"
    styles["gap"] = "0"

    return {
        "key": key, "name": key, "type": "Grid",
        "properties": {},
        "styleProperties": _build_style_properties(styles),
        "children": {}, "displayOrder": 0,
    }


def _apply_responsive_diffs(comp_def: dict[str, Any], spec: SectionSpec) -> None:
    """Apply responsive style diffs to components.

    The diffs from section_extractor are keyed by prop name at the top level
    and by child_N for nested elements. We walk the component tree in parallel
    with the diff tree to apply overrides.
    """
    for vp_name, diffs in spec.responsive_diffs.items():
        resolution = _RESPONSIVE_MAP.get(vp_name)
        if not resolution or not diffs:
            continue

        # Apply top-level diffs to the section root component
        section_key = f"{spec.name}Section"
        if section_key in comp_def:
            top_diffs = {k: v for k, v in diffs.items() if not k.startswith("child_")}
            if top_diffs:
                _add_resolution_override(comp_def[section_key], resolution, top_diffs)

        # Apply child diffs recursively
        _apply_child_diffs(comp_def, section_key, diffs, resolution)


def _apply_child_diffs(
    comp_def: dict[str, Any],
    parent_key: str,
    diffs: dict,
    resolution: str,
) -> None:
    """Recursively apply responsive diffs to child components."""
    if parent_key not in comp_def:
        return

    parent = comp_def[parent_key]
    child_keys = list(parent.get("children", {}).keys())

    for diff_key, diff_val in diffs.items():
        if not diff_key.startswith("child_"):
            continue
        try:
            child_idx = int(diff_key.split("_")[1])
        except (ValueError, IndexError):
            continue
        if child_idx >= len(child_keys):
            continue

        child_key = child_keys[child_idx]
        if child_key not in comp_def:
            continue

        # Apply style diffs to this child
        style_diffs = {k: v for k, v in diff_val.items() if not k.startswith("child_")}
        if style_diffs:
            _add_resolution_override(comp_def[child_key], resolution, style_diffs)

        # Recurse into this child's children
        _apply_child_diffs(comp_def, child_key, diff_val, resolution)


def _add_resolution_override(
    comp: dict[str, Any],
    resolution: str,
    overrides: dict[str, str],
) -> None:
    """Add responsive style overrides to a component."""
    if not overrides:
        return

    sp = comp.get("styleProperties", {})
    if sp:
        # Add to first style entry
        first_sid = next(iter(sp))
        res = sp[first_sid].setdefault("resolutions", {}).setdefault(resolution, {})
        for prop, val in overrides.items():
            res[prop] = {"value": str(val)}
    else:
        style_id = uuid.uuid4().hex[:22]
        comp["styleProperties"] = {
            style_id: {
                "resolutions": {
                    resolution: {prop: {"value": str(val)} for prop, val in overrides.items()}
                }
            }
        }
