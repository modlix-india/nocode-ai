"""CSS-to-Modlix styleProperties converter.

Converts standard CSS (from scraped websites or vision analysis) into the
Modlix componentDefinition styleProperties format:

    styleProperties: {
        "<uniqueStyleId>": {
            "resolutions": {
                "ALL": {
                    "<subComponent>-<cssProp>:<pseudoState>": {"value": "<val>"}
                },
                "MOBILE_LANDSCAPE_SCREEN_SMALL": { ... }
            }
        }
    }

Style key format: [<subComponent>-]<cssProperty>[:<pseudoState>]
  - subComponent: optional prefix (label, icon, inputBox, etc.)
  - cssProperty: camelCase CSS property (backgroundColor, paddingLeft)
  - pseudoState: optional suffix (hover, active, focus, disabled)

CSS shorthand properties (padding, margin, border, background) are expanded
to their individual properties since Modlix doesn't support shorthand.
"""

from __future__ import annotations

import re
import uuid
from typing import Any


# ── CSS Property Name Conversion ─────────────────────────────────

def _to_camel_case(css_prop: str) -> str:
    """Convert kebab-case CSS property to camelCase."""
    parts = css_prop.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _is_shorthand(prop: str) -> bool:
    """Check if a CSS property is a shorthand."""
    return prop in _SHORTHAND_EXPANDERS


# ── Shorthand Expansion ─────────────────────────────────────────

def _expand_padding_margin(prop: str, value: str) -> dict[str, str]:
    """Expand padding/margin shorthand to individual properties."""
    prefix = prop  # 'padding' or 'margin'
    parts = value.split()

    if len(parts) == 1:
        return {
            f"{prefix}Top": parts[0],
            f"{prefix}Right": parts[0],
            f"{prefix}Bottom": parts[0],
            f"{prefix}Left": parts[0],
        }
    if len(parts) == 2:
        return {
            f"{prefix}Top": parts[0],
            f"{prefix}Right": parts[1],
            f"{prefix}Bottom": parts[0],
            f"{prefix}Left": parts[1],
        }
    if len(parts) == 3:
        return {
            f"{prefix}Top": parts[0],
            f"{prefix}Right": parts[1],
            f"{prefix}Bottom": parts[2],
            f"{prefix}Left": parts[1],
        }
    if len(parts) >= 4:
        return {
            f"{prefix}Top": parts[0],
            f"{prefix}Right": parts[1],
            f"{prefix}Bottom": parts[2],
            f"{prefix}Left": parts[3],
        }
    return {prefix: value}


def _expand_border_radius(value: str) -> dict[str, str]:
    """Expand border-radius shorthand."""
    parts = value.split()
    if len(parts) == 1:
        return {"borderRadius": parts[0]}
    if len(parts) == 2:
        return {
            "borderTopLeftRadius": parts[0],
            "borderTopRightRadius": parts[1],
            "borderBottomRightRadius": parts[0],
            "borderBottomLeftRadius": parts[1],
        }
    if len(parts) == 3:
        return {
            "borderTopLeftRadius": parts[0],
            "borderTopRightRadius": parts[1],
            "borderBottomRightRadius": parts[2],
            "borderBottomLeftRadius": parts[1],
        }
    if len(parts) >= 4:
        return {
            "borderTopLeftRadius": parts[0],
            "borderTopRightRadius": parts[1],
            "borderBottomRightRadius": parts[2],
            "borderBottomLeftRadius": parts[3],
        }
    return {"borderRadius": value}


def _expand_border(value: str) -> dict[str, str]:
    """Expand border shorthand (e.g. '1px solid #000')."""
    parts = value.split(None, 2)
    result: dict[str, str] = {}

    if len(parts) >= 1:
        for side in ("Top", "Right", "Bottom", "Left"):
            result[f"border{side}Width"] = parts[0]
    if len(parts) >= 2:
        for side in ("Top", "Right", "Bottom", "Left"):
            result[f"border{side}Style"] = parts[1]
    if len(parts) >= 3:
        for side in ("Top", "Right", "Bottom", "Left"):
            result[f"border{side}Color"] = parts[2]

    return result


def _expand_background(value: str) -> dict[str, str]:
    """Expand background shorthand — extract color, image, position, size."""
    result: dict[str, str] = {}

    # Check for gradient
    if "gradient" in value.lower():
        result["backgroundImage"] = value
        return result

    # Check for url()
    url_match = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", value)
    if url_match:
        result["backgroundImage"] = f"url('{url_match.group(1)}')"
        # Remove url part for further parsing
        remaining = re.sub(r"url\([^)]+\)", "", value).strip()
    else:
        remaining = value

    # Check for color
    color_match = re.match(r"(#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)|[a-zA-Z]+)", remaining)
    if color_match:
        color = color_match.group(1)
        if color.lower() not in ("no-repeat", "repeat", "center", "cover", "contain", "fixed", "scroll"):
            result["backgroundColor"] = color

    # Check for common background properties in remaining text
    if "no-repeat" in value:
        result["backgroundRepeat"] = "no-repeat"
    if "cover" in value:
        result["backgroundSize"] = "cover"
    if "contain" in value:
        result["backgroundSize"] = "contain"
    if "center" in value and "background" not in result.get("backgroundImage", ""):
        result["backgroundPosition"] = "center"
    if "fixed" in value:
        result["backgroundAttachment"] = "fixed"

    return result or {"backgroundColor": value}


_SHORTHAND_EXPANDERS = {
    "padding": lambda v: _expand_padding_margin("padding", v),
    "margin": lambda v: _expand_padding_margin("margin", v),
    "borderRadius": lambda v: _expand_border_radius(v),
    "border": lambda v: _expand_border(v),
    "background": lambda v: _expand_background(v),
}


# ── CSS to Modlix Conversion ────────────────────────────────────


def css_to_style_properties(
    css_declarations: dict[str, str],
    resolution: str = "ALL",
    sub_component: str = "",
    pseudo_state: str = "",
) -> dict[str, Any]:
    """Convert a dict of CSS property-value pairs to Modlix styleProperties format.

    Args:
        css_declarations: CSS property (kebab-case or camelCase) → value.
            e.g. {"background-color": "#1a1a2e", "padding": "20px 40px"}
        resolution: Modlix resolution key (default "ALL").
        sub_component: Optional sub-component prefix (e.g. "label", "icon").
        pseudo_state: Optional pseudo-state suffix (e.g. "hover", "disabled").

    Returns:
        Modlix styleProperties dict ready to merge into a component.
        {
            "<uniqueId>": {
                "resolutions": {
                    "<resolution>": {
                        "<key>": {"value": "<val>"},
                        ...
                    }
                }
            }
        }
    """
    style_id = uuid.uuid4().hex[:22]
    props: dict[str, dict[str, str]] = {}

    for raw_prop, value in css_declarations.items():
        if not value or not raw_prop:
            continue

        # Normalize to camelCase
        camel_prop = _to_camel_case(raw_prop) if "-" in raw_prop else raw_prop

        # Expand shorthands
        if camel_prop in _SHORTHAND_EXPANDERS:
            expanded = _SHORTHAND_EXPANDERS[camel_prop](value)
            for exp_prop, exp_val in expanded.items():
                key = _build_style_key(exp_prop, sub_component, pseudo_state)
                props[key] = {"value": exp_val}
        else:
            key = _build_style_key(camel_prop, sub_component, pseudo_state)
            props[key] = {"value": value}

    return {
        style_id: {
            "resolutions": {
                resolution: props,
            }
        }
    }


def _build_style_key(css_prop: str, sub_component: str = "", pseudo_state: str = "") -> str:
    """Build a Modlix style key from components."""
    key = css_prop
    if sub_component:
        key = f"{sub_component}-{key}"
    if pseudo_state:
        key = f"{key}:{pseudo_state}"
    return key


# ── Multi-Resolution Conversion ─────────────────────────────────


def css_to_responsive_style_properties(
    styles_by_resolution: dict[str, dict[str, str]],
    sub_component: str = "",
    pseudo_state: str = "",
) -> dict[str, Any]:
    """Convert CSS declarations for multiple resolutions.

    Args:
        styles_by_resolution: Mapping of resolution → CSS declarations.
            e.g. {
                "ALL": {"background-color": "#1a1a2e", "padding": "80px"},
                "MOBILE_LANDSCAPE_SCREEN_SMALL": {"padding": "20px"},
            }

    Returns:
        Single Modlix styleProperties entry with all resolutions.
    """
    style_id = uuid.uuid4().hex[:22]
    resolutions: dict[str, dict[str, dict[str, str]]] = {}

    for resolution, declarations in styles_by_resolution.items():
        props: dict[str, dict[str, str]] = {}
        for raw_prop, value in declarations.items():
            if not value or not raw_prop:
                continue

            camel_prop = _to_camel_case(raw_prop) if "-" in raw_prop else raw_prop

            if camel_prop in _SHORTHAND_EXPANDERS:
                expanded = _SHORTHAND_EXPANDERS[camel_prop](value)
                for exp_prop, exp_val in expanded.items():
                    key = _build_style_key(exp_prop, sub_component, pseudo_state)
                    props[key] = {"value": exp_val}
            else:
                key = _build_style_key(camel_prop, sub_component, pseudo_state)
                props[key] = {"value": value}

        if props:
            resolutions[resolution] = props

    return {style_id: {"resolutions": resolutions}}


# ── CSS Media Query to Resolution Mapping ───────────────────────

_MEDIA_QUERY_MAP = {
    # Max-width based (mobile-first breakpoints)
    480: "MOBILE_LANDSCAPE_SCREEN_SMALL",
    600: "MOBILE_LANDSCAPE_SCREEN_SMALL",
    768: "TABLET_POTRAIT_SCREEN_SMALL",
    992: "TABLET_LANDSCAPE_SCREEN_SMALL",
    1024: "TABLET_LANDSCAPE_SCREEN",
    1200: "DESKTOP_SCREEN",
}


def map_media_query_to_resolution(max_width: int) -> str:
    """Map a CSS max-width media query breakpoint to a Modlix resolution."""
    for threshold, resolution in sorted(_MEDIA_QUERY_MAP.items()):
        if max_width <= threshold:
            return resolution
    return "ALL"


# ── Section-Type Style Templates ────────────────────────────────
#
# Pre-built style templates for common section types.
# Used when the scraper identifies a section type (hero, navbar, etc.)
# and we need to generate appropriate base styles.

def get_section_template_styles(
    section_type: str,
    colors: dict[str, str] | None = None,
) -> dict[str, str]:
    """Get base CSS declarations for a section type.

    Args:
        section_type: e.g. "hero", "navbar", "footer", "features"
        colors: Optional color overrides {"primary": "#hex", "background": "#hex", etc.}

    Returns:
        CSS declarations dict (kebab-case) suitable for css_to_style_properties().
    """
    c = colors or {}
    bg = c.get("background", "#1a1a2e")
    primary = c.get("primary", "#c9a44c")
    text = c.get("text", "#ffffff")
    text_muted = c.get("text_muted", "rgba(255,255,255,0.7)")

    templates: dict[str, dict[str, str]] = {
        "navbar": {
            "width": "100%",
            "position": "fixed",
            "top": "0",
            "zIndex": "10",
            "backgroundColor": "rgba(0,0,0,0.85)",
            "paddingTop": "16px",
            "paddingBottom": "16px",
            "paddingLeft": "40px",
            "paddingRight": "40px",
            "justifyContent": "space-between",
            "alignItems": "center",
        },
        "hero": {
            "width": "100%",
            "minHeight": "100vh",
            "backgroundSize": "cover",
            "backgroundPosition": "center",
            "backgroundRepeat": "no-repeat",
            "position": "relative",
            "paddingTop": "120px",
            "paddingBottom": "80px",
            "paddingLeft": "60px",
            "paddingRight": "60px",
            "color": text,
        },
        "hero_overlay": {
            "position": "absolute",
            "top": "0",
            "left": "0",
            "width": "100%",
            "height": "100%",
            "backgroundColor": "rgba(0,0,0,0.5)",
            "zIndex": "0",
        },
        "features": {
            "paddingTop": "80px",
            "paddingBottom": "80px",
            "paddingLeft": "60px",
            "paddingRight": "60px",
            "backgroundColor": bg,
            "gap": "30px",
        },
        "about": {
            "paddingTop": "80px",
            "paddingBottom": "80px",
            "paddingLeft": "60px",
            "paddingRight": "60px",
            "gap": "40px",
        },
        "contact-form": {
            "paddingTop": "80px",
            "paddingBottom": "80px",
            "paddingLeft": "60px",
            "paddingRight": "60px",
            "backgroundColor": bg,
        },
        "footer": {
            "width": "100%",
            "backgroundColor": "#111111",
            "color": text_muted,
            "paddingTop": "40px",
            "paddingBottom": "40px",
            "paddingLeft": "60px",
            "paddingRight": "60px",
        },
        "cta": {
            "paddingTop": "60px",
            "paddingBottom": "60px",
            "paddingLeft": "40px",
            "paddingRight": "40px",
            "backgroundColor": primary,
            "justifyContent": "center",
            "alignItems": "center",
            "textAlign": "center",
        },
        "card": {
            "backgroundColor": "#ffffff",
            "borderRadius": "12px",
            "boxShadow": "0 4px 20px rgba(0,0,0,0.1)",
            "paddingTop": "24px",
            "paddingBottom": "24px",
            "paddingLeft": "24px",
            "paddingRight": "24px",
            "overflow": "hidden",
        },
        "button_primary": {
            "backgroundColor": primary,
            "color": "#000000",
            "paddingTop": "12px",
            "paddingBottom": "12px",
            "paddingLeft": "32px",
            "paddingRight": "32px",
            "borderRadius": "4px",
            "fontWeight": "600",
            "borderTopWidth": "0",
            "borderRightWidth": "0",
            "borderBottomWidth": "0",
            "borderLeftWidth": "0",
            "cursor": "pointer",
        },
    }

    return templates.get(section_type, {})


# ── High-Level Conversion from Scraped Data ─────────────────────


def scraped_section_to_style_properties(
    section: dict[str, Any],
    colors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a scraped website section into Modlix styleProperties.

    Uses the section's type, inline styles, and background image to generate
    appropriate styleProperties. Falls back to template styles for the section type.

    Args:
        section: A section dict from the web scraper.
        colors: Design system colors extracted from vision analysis.

    Returns:
        Modlix styleProperties dict ready to set on a component.
    """
    section_type = section.get("type", "section")
    inline_styles = section.get("styles", {})
    bg_image = section.get("backgroundImage", "")

    # Start with template styles for this section type
    base_styles = get_section_template_styles(section_type, colors)

    # Override with scraped inline styles (already parsed)
    for prop, value in inline_styles.items():
        camel = _to_camel_case(prop) if "-" in prop else prop
        base_styles[camel] = value

    # Add background image if present
    if bg_image:
        if "gradient" in bg_image:
            base_styles["backgroundImage"] = bg_image
        else:
            # Apply with dark overlay for hero sections
            if section_type in ("hero", "section") and "backgroundImage" not in base_styles:
                base_styles["backgroundImage"] = (
                    f"linear-gradient(rgba(0,0,0,0.5), rgba(0,0,0,0.5)), url('{bg_image}')"
                )
                base_styles["backgroundSize"] = "cover"
                base_styles["backgroundPosition"] = "center"
                base_styles["backgroundRepeat"] = "no-repeat"
            else:
                base_styles["backgroundImage"] = f"url('{bg_image}')"

    # Generate responsive overrides
    mobile_styles: dict[str, str] = {}
    for prop in ("paddingLeft", "paddingRight"):
        if prop in base_styles:
            # Reduce padding on mobile
            val = base_styles[prop]
            if val.endswith("px"):
                try:
                    px = int(val.replace("px", ""))
                    mobile_styles[prop] = f"{max(16, px // 3)}px"
                except ValueError:
                    pass

    styles_by_resolution = {"ALL": base_styles}
    if mobile_styles:
        styles_by_resolution["MOBILE_LANDSCAPE_SCREEN_SMALL"] = mobile_styles

    return css_to_responsive_style_properties(styles_by_resolution)
