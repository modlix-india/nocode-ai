"""HTML-to-Modlix page definition converter.

Converts scraped HTML + CSS directly into a complete Modlix page definition
(componentDefinition JSON) ready to be PUT to the API. No LLM needed for
the conversion — this is pure programmatic translation.

HTML → Modlix mapping:
  <div>, <section>, <header>, <footer>, <nav>, <main>, <article> → Grid
  <p>, <span>, <h1>-<h6>, <label> → Text
  <img> → Image
  <a> with button-like classes → Button, otherwise Text with onClick
  <button> → Button
  <input>, <textarea> → TextBox / TextArea
  <select> → Dropdown
  <ul>/<ol> → Grid with child Text items

CSS → styleProperties:
  All inline styles + computed styles from external CSS are converted
  using the css_converter module.
"""

from __future__ import annotations

import re
import uuid
import logging
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.agents.appbuilder.tools.css_converter import (
    css_to_responsive_style_properties,
    _to_camel_case,
    _SHORTHAND_EXPANDERS,
    _build_style_key,
)

logger = logging.getLogger(__name__)

# ── Component key generation ────────────────────────────────────

_key_counter = 0


def _gen_key(prefix: str = "c") -> str:
    global _key_counter
    _key_counter += 1
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _reset_keys():
    global _key_counter
    _key_counter = 0


# ── HTML tag to Modlix type mapping ─────────────────────────────

_TAG_TYPE_MAP = {
    "div": "Grid",
    "section": "Grid",
    "header": "Grid",
    "footer": "Grid",
    "nav": "Grid",
    "main": "Grid",
    "article": "Grid",
    "aside": "Grid",
    "form": "Grid",
    "ul": "Grid",
    "ol": "Grid",
    "li": "Grid",
    "p": "Text",
    "span": "Text",
    "h1": "Text",
    "h2": "Text",
    "h3": "Text",
    "h4": "Text",
    "h5": "Text",
    "h6": "Text",
    "label": "Text",
    "img": "Image",
    "button": "Button",
    "a": "Text",  # Default for links; upgraded to Button if styled as button
    "input": "TextBox",
    "textarea": "TextArea",
    "select": "Dropdown",
    "video": "Grid",
    "iframe": "Iframe",
    "picture": "Image",
    "figure": "Grid",
    "figcaption": "Text",
    "blockquote": "Text",
    "code": "Text",
    "pre": "Text",
    "table": "Grid",
    "tr": "Grid",
    "td": "Grid",
    "th": "Grid",
}

_HEADING_SIZES = {
    "h1": "48px", "h2": "36px", "h3": "28px",
    "h4": "22px", "h5": "18px", "h6": "16px",
}

_SKIP_TAGS = {"script", "style", "noscript", "link", "meta", "head", "svg", "path", "br", "hr"}


# ── Style parsing ───────────────────────────────────────────────

def _parse_inline_style(style_str: str) -> dict[str, str]:
    """Parse inline CSS style to dict of camelCase properties."""
    if not style_str:
        return {}
    result = {}
    for decl in style_str.split(";"):
        decl = decl.strip()
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip()
        val = val.strip()
        if prop and val:
            camel = _to_camel_case(prop) if "-" in prop else prop
            result[camel] = val
    return result


def _expand_and_convert_styles(css_dict: dict[str, str]) -> dict[str, dict[str, str]]:
    """Convert CSS dict to Modlix style value format, expanding shorthands."""
    result = {}
    for prop, val in css_dict.items():
        if prop in _SHORTHAND_EXPANDERS:
            expanded = _SHORTHAND_EXPANDERS[prop](val)
            for exp_prop, exp_val in expanded.items():
                result[exp_prop] = {"value": exp_val}
        else:
            result[prop] = {"value": val}
    return result


def _make_style_properties(css_dict: dict[str, str]) -> dict[str, Any]:
    """Create a Modlix styleProperties entry from CSS declarations."""
    if not css_dict:
        return {}
    style_id = uuid.uuid4().hex[:22]
    props = _expand_and_convert_styles(css_dict)
    if not props:
        return {}
    return {style_id: {"resolutions": {"ALL": props}}}


# ── Element conversion ──────────────────────────────────────────

def _is_button_like(tag: Tag) -> bool:
    """Check if an <a> tag looks like a button."""
    classes = " ".join(tag.get("class", [])).lower()
    return (
        tag.name == "button"
        or "btn" in classes
        or "button" in classes
        or "cta" in classes
    )


def _get_text_content(tag: Tag) -> str:
    """Get direct text content of a tag (not nested children)."""
    texts = []
    for child in tag.children:
        if isinstance(child, str):
            t = child.strip()
            if t:
                texts.append(t)
    return " ".join(texts)[:500]


def _get_all_text(tag: Tag) -> str:
    """Get all text content from a tag."""
    return tag.get_text(strip=True)[:500]


def _convert_element(
    tag: Tag,
    base_url: str,
    comp_def: dict[str, Any],
    depth: int = 0,
    max_depth: int = 8,
    max_children: int = 30,
) -> str | None:
    """Convert an HTML element to a Modlix component.

    Returns the component key, or None if skipped.
    Adds the component to comp_def dict.
    """
    if not isinstance(tag, Tag):
        return None
    if tag.name in _SKIP_TAGS:
        return None
    if depth > max_depth:
        return None

    tag_name = tag.name.lower()
    comp_type = _TAG_TYPE_MAP.get(tag_name, "Grid")
    classes = " ".join(tag.get("class", []))
    inline_style = tag.get("style", "")

    # Parse styles
    css_dict = _parse_inline_style(inline_style)

    # Generate component key
    comp_id = tag.get("id", "")
    if comp_id:
        key = re.sub(r"[^a-zA-Z0-9_]", "_", comp_id)[:30]
    else:
        key = _gen_key(tag_name[:3])

    # Ensure unique key
    while key in comp_def:
        key = key + "_" + uuid.uuid4().hex[:4]

    # ── Handle specific element types ───────────────────────

    properties: dict[str, Any] = {}
    children_map: dict[str, bool] = {}

    if tag_name == "img":
        src = tag.get("src", "") or tag.get("data-src", "")
        if src:
            src = urljoin(base_url, src)
        alt = tag.get("alt", "")
        properties["src"] = {"value": src}
        if alt:
            properties["alt"] = {"value": alt}
        # Default image styles
        if "width" not in css_dict:
            css_dict["width"] = "100%"
        if "objectFit" not in css_dict:
            css_dict["objectFit"] = "cover"

    elif tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        comp_type = "Text"
        text = _get_all_text(tag)
        if text:
            properties["text"] = {"value": text}
        # Default heading styles
        if "fontSize" not in css_dict:
            css_dict["fontSize"] = _HEADING_SIZES.get(tag_name, "24px")
        if "fontWeight" not in css_dict:
            css_dict["fontWeight"] = "700" if tag_name in ("h1", "h2") else "600"

    elif tag_name in ("p", "span", "label"):
        comp_type = "Text"
        text = _get_all_text(tag)
        if text:
            properties["text"] = {"value": text}

    elif tag_name == "a":
        href = tag.get("href", "")
        text = _get_all_text(tag)
        if _is_button_like(tag):
            comp_type = "Button"
            if text:
                properties["label"] = {"value": text}
        else:
            comp_type = "Text"
            if text:
                properties["text"] = {"value": text}
        # Store href for later (onClick or navigation)

    elif tag_name == "button":
        comp_type = "Button"
        text = _get_all_text(tag)
        if text:
            properties["label"] = {"value": text}

    elif tag_name in ("input", "textarea"):
        comp_type = "TextBox" if tag_name == "input" else "TextArea"
        placeholder = tag.get("placeholder", "")
        if placeholder:
            properties["placeholder"] = {"value": placeholder}
        input_type = tag.get("type", "text")
        if input_type == "email":
            properties["placeholder"] = {"value": placeholder or "Email"}

    elif comp_type == "Grid":
        # For Grid (div, section, etc.), check for background image in style
        bg_match = re.search(r"url\(['\"]?([^'\")\s]+)", inline_style)
        if bg_match:
            bg_url = urljoin(base_url, bg_match.group(1))
            css_dict["backgroundImage"] = f"url('{bg_url}')"
            if "backgroundSize" not in css_dict:
                css_dict["backgroundSize"] = "cover"
            if "backgroundPosition" not in css_dict:
                css_dict["backgroundPosition"] = "center"

    # ── Process children (for Grid-type components) ─────────

    if comp_type == "Grid":
        child_count = 0
        for child in tag.children:
            if child_count >= max_children:
                break
            if isinstance(child, Tag):
                child_key = _convert_element(
                    child, base_url, comp_def,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                if child_key:
                    children_map[child_key] = True
                    child_count += 1
            elif isinstance(child, str):
                text = child.strip()
                if text and len(text) > 2:
                    # Inline text in a Grid → create a Text child
                    text_key = _gen_key("txt")
                    comp_def[text_key] = {
                        "key": text_key,
                        "name": text_key,
                        "type": "Text",
                        "properties": {"text": {"value": text[:300]}},
                        "styleProperties": {},
                        "children": {},
                        "displayOrder": child_count,
                    }
                    children_map[text_key] = True
                    child_count += 1

    # ── Build component ─────────────────────────────────────

    style_props = _make_style_properties(css_dict)

    component: dict[str, Any] = {
        "key": key,
        "name": key,
        "type": comp_type,
        "properties": properties,
        "styleProperties": style_props,
        "children": children_map,
        "displayOrder": 0,
    }

    comp_def[key] = component
    return key


# ── Main conversion function ────────────────────────────────────


def html_to_page_definition(
    html: str,
    base_url: str,
    page_name: str = "home",
    app_code: str = "",
    client_code: str = "",
) -> dict[str, Any]:
    """Convert HTML to a complete Modlix page definition.

    Args:
        html: Raw HTML content.
        base_url: Base URL for resolving relative URLs.
        page_name: Name for the page.
        app_code: Application code.
        client_code: Client code.

    Returns:
        Complete page definition dict ready for POST /api/ui/pages.
    """
    _reset_keys()
    soup = BeautifulSoup(html, "lxml")

    body = soup.find("body")
    if not body:
        body = soup

    comp_def: dict[str, Any] = {}

    # Create root Grid
    root_key = "root"
    root_children: dict[str, bool] = {}

    # Convert body children to Modlix components
    child_count = 0
    for child in body.children:
        if not isinstance(child, Tag):
            continue
        if child.name in _SKIP_TAGS:
            continue
        if child_count >= 20:  # Max 20 top-level sections
            break

        child_key = _convert_element(child, base_url, comp_def, depth=0)
        if child_key:
            # Set display order
            comp_def[child_key]["displayOrder"] = child_count
            root_children[child_key] = True
            child_count += 1

    # Build root component
    comp_def[root_key] = {
        "key": root_key,
        "name": root_key,
        "type": "Grid",
        "properties": {},
        "styleProperties": _make_style_properties({
            "width": "100%",
            "minHeight": "100vh",
        }),
        "children": root_children,
        "displayOrder": 0,
    }

    logger.info("Converted HTML to %d Modlix components", len(comp_def))

    return {
        "name": page_name,
        "appCode": app_code,
        "clientCode": client_code,
        "rootComponent": root_key,
        "componentDefinition": comp_def,
        "eventFunctions": {},
        "properties": {},
        "translations": {},
        "message": f"Converted from {base_url}",
    }


async def scrape_and_convert(
    url: str,
    page_name: str = "home",
    app_code: str = "",
    client_code: str = "",
) -> dict[str, Any]:
    """Scrape a website with computed styles and convert to a Modlix page definition.

    Uses Playwright to render the page in a real browser, then extracts
    computed styles for every visible element. This captures ALL styling —
    from external CSS files, class selectors, media queries, and inheritance.

    Falls back to basic HTML parsing if Playwright is unavailable.
    """
    try:
        return await _scrape_with_computed_styles(url, page_name, app_code, client_code)
    except Exception as e:
        logger.warning("Playwright computed styles failed, falling back to basic: %s", e)
        from app.agents.appbuilder.tools.web_scraper import _fetch_html
        html, final_url = await _fetch_html(url)
        return html_to_page_definition(html, final_url, page_name, app_code, client_code)


# ── Playwright computed styles extraction (v2 with parent diffing) ──

_EXTRACT_JS = """() => {
    const LAYOUT_PROPS = [
        'display', 'flexDirection', 'flexWrap', 'justifyContent',
        'alignItems', 'alignSelf', 'gap', 'gridTemplateColumns', 'gridTemplateRows',
        'flex', 'flexGrow', 'flexShrink', 'flexBasis',
    ];
    const BOX_PROPS = [
        'width', 'height', 'minHeight', 'maxWidth', 'minWidth', 'maxHeight',
        'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
        'marginTop', 'marginRight', 'marginBottom', 'marginLeft',
    ];
    const BG_PROPS = [
        'backgroundColor', 'backgroundImage', 'backgroundSize',
        'backgroundPosition', 'backgroundRepeat', 'backgroundClip',
    ];
    const BORDER_PROPS = [
        'borderTopWidth', 'borderTopStyle', 'borderTopColor',
        'borderRightWidth', 'borderRightStyle', 'borderRightColor',
        'borderBottomWidth', 'borderBottomStyle', 'borderBottomColor',
        'borderLeftWidth', 'borderLeftStyle', 'borderLeftColor',
        'borderTopLeftRadius', 'borderTopRightRadius',
        'borderBottomLeftRadius', 'borderBottomRightRadius',
    ];
    const TEXT_PROPS = [
        'color', 'fontSize', 'fontFamily', 'fontWeight',
        'lineHeight', 'textAlign', 'letterSpacing',
        'textDecoration', 'textDecorationLine', 'textTransform',
        'whiteSpace', 'wordBreak', 'textOverflow',
    ];
    const POSITION_PROPS = [
        'position', 'top', 'left', 'right', 'bottom', 'zIndex',
    ];
    const VISUAL_PROPS = [
        'opacity', 'boxShadow', 'overflow', 'overflowX', 'overflowY',
        'objectFit', 'cursor', 'transform', 'transition',
        'backdropFilter',
    ];

    const ALL_PROPS = [...LAYOUT_PROPS, ...BOX_PROPS, ...BG_PROPS,
                       ...BORDER_PROPS, ...TEXT_PROPS, ...POSITION_PROPS, ...VISUAL_PROPS];

    const DEFAULTS = {
        backgroundColor: 'rgba(0, 0, 0, 0)', backgroundImage: 'none', backgroundClip: 'border-box',
        boxShadow: 'none',
        borderTopWidth: '0px', borderRightWidth: '0px', borderBottomWidth: '0px', borderLeftWidth: '0px',
        borderTopStyle: 'none', borderRightStyle: 'none', borderBottomStyle: 'none', borderLeftStyle: 'none',
        borderTopColor: 'rgb(0, 0, 0)', borderRightColor: 'rgb(0, 0, 0)',
        borderBottomColor: 'rgb(0, 0, 0)', borderLeftColor: 'rgb(0, 0, 0)',
        borderTopLeftRadius: '0px', borderTopRightRadius: '0px',
        borderBottomLeftRadius: '0px', borderBottomRightRadius: '0px',
        display: 'block', position: 'static', opacity: '1',
        overflow: 'visible', overflowX: 'visible', overflowY: 'visible',
        zIndex: 'auto', gap: 'normal', flexDirection: 'row', flexWrap: 'nowrap',
        flex: '0 1 auto', flexGrow: '0', flexShrink: '1', flexBasis: 'auto', alignSelf: 'auto',
        textAlign: 'start', letterSpacing: 'normal', objectFit: 'fill',
        textDecoration: 'none solid rgb(0, 0, 0)', textDecorationLine: 'none', textTransform: 'none',
        whiteSpace: 'normal', wordBreak: 'normal', textOverflow: 'clip',
        cursor: 'auto', transform: 'none', transition: 'all 0s ease 0s',
        backdropFilter: 'none',
        top: 'auto', left: 'auto', right: 'auto', bottom: 'auto',
        marginTop: '0px', marginRight: '0px', marginBottom: '0px', marginLeft: '0px',
        paddingTop: '0px', paddingRight: '0px', paddingBottom: '0px', paddingLeft: '0px',
        minWidth: 'auto', maxWidth: 'none', minHeight: 'auto', maxHeight: 'none',
        gridTemplateColumns: 'none', gridTemplateRows: 'none',
    };

    const INHERITED = new Set(['color','fontSize','fontFamily','fontWeight','lineHeight','textAlign',
                                'letterSpacing','whiteSpace','wordBreak','textTransform','cursor']);

    function camelToKebab(s) { return s.replace(/([A-Z])/g, '-$1').toLowerCase(); }

    function getOwnStyles(el, parentStyles) {
        const computed = window.getComputedStyle(el);
        const styles = {};
        for (const prop of ALL_PROPS) {
            const val = computed.getPropertyValue(camelToKebab(prop));
            if (!val) continue;
            if (DEFAULTS[prop] === val) continue;
            if (INHERITED.has(prop) && parentStyles && parentStyles[prop] === val) continue;
            // Skip auto values for sizing (except positioned elements which use auto meaningfully)
            if (val === 'auto' && ['height','width','maxWidth','maxHeight'].includes(prop)) continue;
            if (val === 'none' && ['maxWidth','maxHeight','minHeight','minWidth'].includes(prop)) continue;
            if (val === 'normal' && ['justifyContent','alignItems','lineHeight','letterSpacing'].includes(prop)) continue;
            if (val === '0px' && ['minHeight','marginTop','marginRight','marginBottom','marginLeft'].includes(prop)) continue;
            if (val === 'start' && prop === 'textAlign') continue;
            if (val === 'repeat' && prop === 'backgroundRepeat') continue;
            // Skip full-width px values at desktop (likely just viewport width)
            if (prop === 'width' && val.includes('px') && parseFloat(val) > 1400) continue;
            // Skip excessively large height values — these are scroll-computed heights,
            // not intentional fixed heights. Content should determine height naturally.
            if ((prop === 'height' || prop === 'minHeight') && val.includes('px') && parseFloat(val) > 1200) continue;
            if (prop.includes('border') && prop.includes('Color') && val === parentStyles?.color) continue;
            styles[prop] = val;
        }
        const display = computed.getPropertyValue('display');
        if (display === 'flex' || display === 'inline-flex') styles.display = 'flex';
        else if (display === 'grid' || display === 'inline-grid') styles.display = 'grid';
        else delete styles.display;
        return styles;
    }

    function extractElement(el, depth, parentStyles) {
        if (depth > 8 || !el || !el.tagName) return null;
        const tag = el.tagName.toLowerCase();
        if (['script','style','noscript','link','meta','head','path','br','hr'].includes(tag)) return null;
        const rect = el.getBoundingClientRect();
        // Convert SVG to a data URI image instead of skipping entirely
        if (tag === 'svg') {
            if (rect.width < 1 && rect.height < 1) return null;
            try {
                const svgStr = new XMLSerializer().serializeToString(el);
                const svgB64 = btoa(unescape(encodeURIComponent(svgStr)));
                return {tag: 'img', id: el.id || '', classes: '', text: '',
                        src: 'data:image/svg+xml;base64,' + svgB64,
                        href: '', alt: el.getAttribute('aria-label') || 'icon', placeholder: '',
                        styles: {}, isRowLayout: false,
                        rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
                        children: []};
            } catch(e) { return null; }
        }
        if (rect.width < 1 && rect.height < 1) return null;
        if (rect.top > 5000) return null;

        const styles = getOwnStyles(el, parentStyles);
        const computed = window.getComputedStyle(el);
        const myFullStyles = {};
        for (const prop of ALL_PROPS) myFullStyles[prop] = computed.getPropertyValue(camelToKebab(prop));

        const display = computed.getPropertyValue('display');
        const flexDir = computed.getPropertyValue('flex-direction');
        const gridCols = computed.getPropertyValue('grid-template-columns');
        let isRowLayout = false;
        if ((display === 'flex' || display === 'inline-flex') && flexDir === 'row') isRowLayout = true;
        if ((display === 'grid' || display === 'inline-grid') && gridCols && gridCols !== 'none') isRowLayout = true;
        if (!isRowLayout && el.children.length >= 2) {
            const r1 = el.children[0].getBoundingClientRect();
            const r2 = el.children[1].getBoundingClientRect();
            if (Math.abs(r1.top - r2.top) < 20 && r1.width > 10 && r2.width > 10) isRowLayout = true;
        }

        const data = {
            tag, id: el.id || '',
            classes: (typeof el.className === 'string' ? el.className : '').substring(0, 100),
            text: '', src: '', href: '', alt: '', placeholder: '',
            styles, isRowLayout,
            rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
            children: [],
        };
        for (const node of el.childNodes) {
            if (node.nodeType === 3) {
                const t = node.textContent.trim();
                if (t) data.text += (data.text ? ' ' : '') + t.substring(0, 300);
            }
        }
        if (tag === 'img') {
            data.src = el.src || el.dataset?.src || el.dataset?.lazy || '';
            data.alt = el.alt || '';
        }
        if (tag === 'picture') {
            // Extract best source from picture element
            const source = el.querySelector('source');
            const img = el.querySelector('img');
            data.src = (source && source.srcset ? source.srcset.split(',')[0].trim().split(' ')[0] : '')
                     || (img ? img.src || img.dataset?.src || '' : '');
            data.alt = img ? img.alt || '' : '';
            data.tag = 'img'; // Convert picture to img
        }
        if (tag === 'a') {
            data.href = el.href || '';
        }
        if (tag === 'iframe') {
            data.src = el.src || el.dataset?.src || '';
        }
        if (tag === 'video') {
            data.src = el.poster || '';
            const source = el.querySelector('source');
            if (source) data.videoSrc = source.src || '';
        }
        if (tag === 'input' || tag === 'textarea') data.placeholder = el.placeholder || '';

        let childCount = 0;
        for (const child of el.children) {
            if (childCount >= 25) break;
            const childData = extractElement(child, depth + 1, myFullStyles);
            if (childData) { data.children.push(childData); childCount++; }
        }
        return data;
    }

    const body = document.body;
    const bodyComputed = window.getComputedStyle(body);
    const bodyStyles = {};
    for (const prop of ALL_PROPS) bodyStyles[prop] = bodyComputed.getPropertyValue(camelToKebab(prop));

    // Unwrap SPA root containers — React (#root, #app, #__next, #___gatsby),
    // Vue (#app), Angular (app-root), etc. Find the deepest single-child wrapper
    // chain and extract from the first element that has multiple visible children.
    let contentRoot = body;
    let contentParentStyles = bodyStyles;
    for (let i = 0; i < 5; i++) {
        const visibleChildren = [];
        for (const child of contentRoot.children) {
            const tag = (child.tagName || '').toLowerCase();
            if (['script','style','noscript','link','meta'].includes(tag)) continue;
            const r = child.getBoundingClientRect();
            if (r.width < 10 || r.height < 10) continue;
            visibleChildren.push(child);
        }
        // If exactly one visible child that's a generic wrapper (div/main/section),
        // unwrap into it to find real content sections
        if (visibleChildren.length === 1) {
            const wrapper = visibleChildren[0];
            const wrapperTag = wrapper.tagName.toLowerCase();
            if (['div', 'main', 'section'].includes(wrapperTag)) {
                const wc = window.getComputedStyle(wrapper);
                const wStyles = {};
                for (const prop of ALL_PROPS) wStyles[prop] = wc.getPropertyValue(camelToKebab(prop));
                contentParentStyles = wStyles;
                contentRoot = wrapper;
                continue;
            }
        }
        break;
    }

    const result = [];
    for (const child of contentRoot.children) {
        const d = extractElement(child, 0, contentParentStyles);
        if (d) result.push(d);
    }
    return result;
}"""

# JS to extract pseudo-class styles (:hover, :focus, :active, :disabled) from stylesheets.
# Returns a flat map: CSS selector (without pseudo) → { "hover": {prop: val}, "focus": {...} }
_EXTRACT_PSEUDO_JS = """() => {
    const PSEUDO_RE = /:(hover|focus|active|disabled|visited)(?![\\w-])/;
    const PROPS = [
        'color','backgroundColor','backgroundImage','borderColor',
        'borderTopColor','borderRightColor','borderBottomColor','borderLeftColor',
        'borderWidth','borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth',
        'borderRadius','borderTopLeftRadius','borderTopRightRadius',
        'borderBottomLeftRadius','borderBottomRightRadius',
        'boxShadow','textDecoration','textDecorationLine','textDecorationColor',
        'opacity','transform','cursor','outline','outlineColor','outlineWidth',
        'fontSize','fontWeight','letterSpacing','lineHeight',
        'paddingTop','paddingRight','paddingBottom','paddingLeft',
        'marginTop','marginRight','marginBottom','marginLeft',
        'width','height','minHeight','maxWidth',
    ];

    function camelToKebab(s) { return s.replace(/([A-Z])/g, '-$1').toLowerCase(); }
    function kebabToCamel(s) { return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

    const pseudoMap = {};

    try {
        for (const sheet of document.styleSheets) {
            let rules;
            try { rules = sheet.cssRules || sheet.rules; } catch(e) { continue; }
            if (!rules) continue;

            for (const rule of rules) {
                if (rule.type !== 1) continue;
                const sel = rule.selectorText || '';
                const match = sel.match(PSEUDO_RE);
                if (!match) continue;

                const pseudoState = match[1];
                const baseSel = sel.replace(PSEUDO_RE, '').replace(/:+$/, '').trim();
                if (!baseSel) continue;

                let matchedEls;
                try { matchedEls = document.querySelectorAll(baseSel); } catch(e) { continue; }
                if (!matchedEls.length) continue;

                const styles = {};
                for (const prop of PROPS) {
                    const val = rule.style.getPropertyValue(camelToKebab(prop));
                    if (val && val !== 'initial' && val !== 'inherit' && val !== 'unset') {
                        styles[kebabToCamel(prop)] = val.trim();
                    }
                }
                if (Object.keys(styles).length === 0) continue;

                for (const el of matchedEls) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 1 && rect.height < 1) continue;
                    if (rect.top > 5000) continue;

                    const elId = el.id || '';
                    const tag = el.tagName.toLowerCase();
                    const path = [];
                    let node = el;
                    while (node && node !== document.body && node.parentElement) {
                        const parent = node.parentElement;
                        const idx = Array.from(parent.children).indexOf(node);
                        path.unshift(idx);
                        node = parent;
                    }
                    const pathKey = path.join('.');

                    if (!pseudoMap[pathKey]) pseudoMap[pathKey] = {elId, tag, pseudoStyles: {}};
                    if (!pseudoMap[pathKey].pseudoStyles[pseudoState]) {
                        pseudoMap[pathKey].pseudoStyles[pseudoState] = {};
                    }
                    Object.assign(pseudoMap[pathKey].pseudoStyles[pseudoState], styles);
                }
            }
        }
    } catch(e) {}

    return pseudoMap;
}"""


async def _scrape_with_computed_styles(
    url: str,
    page_name: str,
    app_code: str,
    client_code: str,
) -> dict[str, Any]:
    """Use Playwright to scrape a page with smart style extraction.

    Key improvements over v1:
    - Parent-child style diffing: only keeps styles that DIFFER from parent
    - Layout translation: strips absolute positioning, keeps flex/grid layout
    - Aggressive denoising: filters browser defaults and inherited values
    - Bounding box extraction for layout analysis
    """
    import asyncio
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await asyncio.sleep(1)

        elements_data = await page.evaluate(_EXTRACT_JS)

        # Extract body-level styles (font, color, background) for root component
        body_styles = await page.evaluate("""() => {
            const cs = window.getComputedStyle(document.body);
            return {
                fontFamily: cs.getPropertyValue('font-family'),
                fontSize: cs.getPropertyValue('font-size'),
                fontWeight: cs.getPropertyValue('font-weight'),
                color: cs.getPropertyValue('color'),
                backgroundColor: cs.getPropertyValue('background-color'),
            };
        }""")
        logger.info("Body styles: font=%s, color=%s", body_styles.get("fontFamily", ""), body_styles.get("color", ""))

        # Extract pseudo-class styles (:hover, :focus, etc.) from stylesheets
        logger.info("Extracting pseudo-class styles from stylesheets...")
        pseudo_map = await page.evaluate(_EXTRACT_PSEUDO_JS)
        logger.info("Found pseudo styles for %d elements", len(pseudo_map))

        # Extract at tablet viewport for responsive overrides
        logger.info("Extracting tablet styles (1024px)...")
        await page.set_viewport_size({"width": 1024, "height": 768})
        await asyncio.sleep(0.5)
        tablet_data = await page.evaluate(_EXTRACT_JS)

        # Extract at mobile viewport for responsive overrides
        logger.info("Extracting mobile styles (375px)...")
        await page.set_viewport_size({"width": 375, "height": 812})
        await asyncio.sleep(0.5)
        mobile_data = await page.evaluate(_EXTRACT_JS)

        await browser.close()

    logger.info(
        "Extracted %d desktop + %d tablet + %d mobile elements",
        len(elements_data), len(tablet_data), len(mobile_data),
    )

    # Convert desktop extraction to Modlix components
    _reset_keys()
    comp_def: dict[str, Any] = {}
    root_children: dict[str, bool] = {}

    for i, el_data in enumerate(elements_data[:20]):
        child_key = _convert_browser_element(el_data, url, comp_def, depth=0)
        if child_key:
            comp_def[child_key]["displayOrder"] = i
            root_children[child_key] = True

    # Use body font for root — inherited styles are stripped from children,
    # so fontFamily must be set on root for the page to render correctly
    body_font = body_styles.get("fontFamily", "")
    # Clean up the font family — remove quotes and fallbacks for the primary font
    primary_font = body_font.split(",")[0].strip().strip("'\"") if body_font else ""

    root_styles = {
        "width": "100vw",
        "height": "100vh",
        "overflow": "auto",
        "gap": "0",
    }
    if primary_font:
        root_styles["fontFamily"] = primary_font
    # Apply body color and background if meaningful
    body_color = body_styles.get("color", "")
    if body_color and body_color != "rgb(0, 0, 0)":
        root_styles["color"] = body_color
    body_bg = body_styles.get("backgroundColor", "")
    if body_bg and body_bg != "rgba(0, 0, 0, 0)" and body_bg != "rgb(255, 255, 255)":
        root_styles["backgroundColor"] = body_bg

    # Root
    comp_def["root"] = {
        "key": "root",
        "name": "rootGrid",
        "type": "Grid",
        "properties": {},
        "styleProperties": _make_style_properties(root_styles),
        "children": root_children,
        "displayOrder": 0,
    }

    # Merge tablet + mobile overrides via tree-position matching
    _merge_responsive_overrides(comp_def, elements_data, tablet_data, mobile_data)

    # Merge pseudo-class styles (hover, focus, active, disabled)
    _merge_pseudo_styles(comp_def, elements_data, pseudo_map)

    # Extract unique fonts — includes root font from body styles
    font_packs = await _extract_font_packs(comp_def)

    logger.info("Converted to %d Modlix components with responsive styles", len(comp_def))

    return {
        "name": page_name,
        "appCode": app_code,
        "clientCode": client_code,
        "rootComponent": "root",
        "componentDefinition": comp_def,
        "eventFunctions": {},
        "properties": {},
        "translations": {},
        "message": f"Cloned from {url} with responsive styles",
        "_fontPacks": font_packs,  # consumed by clone_tool to update Application
    }


# Common system/fallback fonts that don't need Google Fonts loading
_SYSTEM_FONTS = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
    "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded",
    "arial", "helvetica", "times new roman", "times", "courier new", "courier",
    "georgia", "verdana", "tahoma", "trebuchet ms", "impact", "comic sans ms",
    "segoe ui",
}

# Known Google Fonts — maps lowercase name to exact Google Fonts name.
# Covers the top 100+ most-used web fonts. If a font is here, we know
# the Google Fonts link will work.
_KNOWN_GOOGLE_FONTS = {
    "roboto": "Roboto", "open sans": "Open+Sans", "lato": "Lato",
    "montserrat": "Montserrat", "poppins": "Poppins", "inter": "Inter",
    "raleway": "Raleway", "nunito": "Nunito", "playfair display": "Playfair+Display",
    "source sans pro": "Source+Sans+Pro", "source sans 3": "Source+Sans+3",
    "oswald": "Oswald", "merriweather": "Merriweather", "pt sans": "PT+Sans",
    "noto sans": "Noto+Sans", "noto serif": "Noto+Serif", "ubuntu": "Ubuntu",
    "rubik": "Rubik", "work sans": "Work+Sans", "mulish": "Mulish",
    "nunito sans": "Nunito+Sans", "fira sans": "Fira+Sans", "barlow": "Barlow",
    "quicksand": "Quicksand", "dm sans": "DM+Sans", "manrope": "Manrope",
    "karla": "Karla", "libre baskerville": "Libre+Baskerville",
    "josefin sans": "Josefin+Sans", "cabin": "Cabin", "arimo": "Arimo",
    "hind": "Hind", "dosis": "Dosis", "exo 2": "Exo+2",
    "titillium web": "Titillium+Web", "asap": "Asap", "maven pro": "Maven+Pro",
    "archivo": "Archivo", "lexend": "Lexend", "space grotesk": "Space+Grotesk",
    "plus jakarta sans": "Plus+Jakarta+Sans", "outfit": "Outfit",
    "sarabun": "Sarabun", "philosopher": "Philosopher", "cormorant": "Cormorant",
    "aleo": "Aleo", "arvo": "Arvo", "unbounded": "Unbounded",
    "advent pro": "Advent+Pro", "roboto slab": "Roboto+Slab",
    "roboto condensed": "Roboto+Condensed", "roboto mono": "Roboto+Mono",
    "pt serif": "PT+Serif", "dancing script": "Dancing+Script",
    "bebas neue": "Bebas+Neue", "anton": "Anton", "lobster": "Lobster",
    "pacifico": "Pacifico", "caveat": "Caveat", "comfortaa": "Comfortaa",
    "overpass": "Overpass", "bitter": "Bitter", "catamaran": "Catamaran",
    "crimson text": "Crimson+Text", "vollkorn": "Vollkorn",
    "ibm plex sans": "IBM+Plex+Sans", "ibm plex serif": "IBM+Plex+Serif",
    "ibm plex mono": "IBM+Plex+Mono", "spectral": "Spectral",
    "signika": "Signika", "abel": "Abel", "oxygen": "Oxygen",
    "libre franklin": "Libre+Franklin", "heebo": "Heebo",
    "assistant": "Assistant", "kanit": "Kanit", "prompt": "Prompt",
    "jost": "Jost", "sora": "Sora", "figtree": "Figtree",
    "geist": "Geist", "geist mono": "Geist+Mono",
}

# Map non-Google/system fonts to their closest Google Font equivalent.
# Used for custom/paid fonts that can't be loaded from Google Fonts.
_FONT_REPLACEMENTS = {
    "sf pro": "Inter",
    "sf pro display": "Inter",
    "sf pro text": "Inter",
    "sf mono": "Roboto Mono",
    "neue haas grotesk": "Inter",
    "proxima nova": "Montserrat",
    "futura": "Nunito Sans",
    "avenir": "Nunito",
    "avenir next": "Nunito Sans",
    "gotham": "Montserrat",
    "cera pro": "Montserrat",
    "cera mono": "Roboto Mono",
    "circular": "DM Sans",
    "gilroy": "Poppins",
    "graphik": "Inter",
    "aktiv grotesk": "Inter",
    "brandon grotesque": "Nunito",
    "din": "Barlow",
    "din next": "Barlow",
    "museo sans": "Nunito",
    "museo slab": "Roboto Slab",
    "freight sans": "Source Sans 3",
    "freight text": "Source Serif 4",
    "sentinel": "Merriweather",
    "whitney": "Work Sans",
    "calibre": "Inter",
    "acumin pro": "Source Sans 3",
    "neue montreal": "DM Sans",
    "general sans": "DM Sans",
    "cabinet grotesk": "Outfit",
    "clash display": "Space Grotesk",
    "satoshi": "DM Sans",
    "switzer": "Inter",
    "walsheim": "DM Sans",
}


async def _resolve_font_with_llm(font_name: str) -> str | None:
    """Ask Gemini Flash to suggest a Google Font replacement for an unknown font.

    Returns the Google Font name, or None if unavailable.
    """
    try:
        import google.generativeai as genai
        from app.config import settings

        if not settings.GOOGLE_API_KEY:
            return None

        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")

        import asyncio
        response = await asyncio.to_thread(
            model.generate_content,
            f"What Google Font is most visually similar to '{font_name}'? "
            f"Reply with ONLY the exact Google Font name, nothing else. "
            f"If '{font_name}' IS a Google Font, just reply with its exact name.",
        )
        suggestion = (response.text or "").strip().strip("'\"")
        if suggestion and len(suggestion) < 50:
            logger.info("LLM suggested Google Font '%s' for '%s'", suggestion, font_name)
            return suggestion
    except Exception as e:
        logger.warning("LLM font suggestion failed for '%s': %s", font_name, e)
    return None


async def _extract_font_packs(comp_def: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Extract unique font families and resolve them to Google Fonts.

    For each font found in components:
    1. If it's a system font → skip (no loading needed)
    2. If it's a known Google Font → use directly
    3. If it has a known replacement → swap to the Google Font equivalent
    4. Otherwise → ask Gemini Flash for a suggestion

    Returns dict of fontPack ID → {"name": "FontName", "code": "<link ...>"}
    Also updates comp_def in-place to replace non-Google fonts with their equivalents.
    """
    fonts: set[str] = set()

    for comp in comp_def.values():
        sp = comp.get("styleProperties", {})
        for sdata in sp.values():
            for res_data in sdata.get("resolutions", {}).values():
                ff = res_data.get("fontFamily", {})
                val = ff.get("value", "") if isinstance(ff, dict) else ""
                if val:
                    for font in val.split(","):
                        font = font.strip().strip("'\"")
                        if font and font.lower() not in _SYSTEM_FONTS:
                            fonts.add(font)

    font_packs: dict[str, dict[str, str]] = {}
    # Map: original font name → resolved Google Font name (for replacements)
    resolved: dict[str, str] = {}

    for font in sorted(fonts):
        lower = font.lower()

        # Check if it's a known Google Font
        if lower in _KNOWN_GOOGLE_FONTS:
            google_name = font  # Use original casing
            url_name = _KNOWN_GOOGLE_FONTS[lower]
        elif lower in _FONT_REPLACEMENTS:
            # Known non-Google font → use replacement
            replacement = _FONT_REPLACEMENTS[lower]
            logger.info("Font '%s' → Google Font replacement '%s'", font, replacement)
            google_name = replacement
            url_name = replacement.replace(" ", "+")
            resolved[font] = replacement
        else:
            # Unknown font — try LLM suggestion
            suggestion = await _resolve_font_with_llm(font)
            if suggestion:
                google_name = suggestion
                url_name = suggestion.replace(" ", "+")
                resolved[font] = suggestion
            else:
                # Fallback: try using the font name as-is (might work if it IS a Google Font)
                google_name = font
                url_name = font.replace(" ", "+")

        # Dedup by resolved Google Font name
        already_added = {fp["name"].lower() for fp in font_packs.values()}
        if google_name.lower() in already_added:
            continue

        pack_id = uuid.uuid4().hex[:22]
        code = (
            f'<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            f'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            f'<link href="https://fonts.googleapis.com/css2?family={url_name}'
            f':ital,wght@0,100..900;1,100..900&display=swap" rel="stylesheet">'
        )
        font_packs[pack_id] = {"name": google_name, "code": code}

    # Update comp_def to replace non-Google fonts with their resolved equivalents
    if resolved:
        for comp in comp_def.values():
            sp = comp.get("styleProperties", {})
            for sdata in sp.values():
                for res_data in sdata.get("resolutions", {}).values():
                    ff = res_data.get("fontFamily", {})
                    val = ff.get("value", "") if isinstance(ff, dict) else ""
                    if val:
                        for orig, replacement in resolved.items():
                            if orig in val:
                                res_data["fontFamily"] = {"value": val.replace(orig, replacement)}
        logger.info("Replaced fonts in components: %s", resolved)

    if font_packs:
        logger.info("Font packs: %s", [fp["name"] for fp in font_packs.values()])

    return font_packs


def _merge_responsive_overrides(
    comp_def: dict[str, Any],
    desktop_data: list[dict],
    tablet_data: list[dict],
    mobile_data: list[dict],
) -> None:
    """Merge tablet and mobile style overrides into component styleProperties.

    Uses tree-position matching: walks the desktop and responsive extraction
    trees in parallel (same child index at each level = same component).
    Only styles that DIFFER from the desktop baseline get added as overrides.

    Resolution mapping:
    - ALL = desktop (1440px) — already in styleProperties
    - TABLET_LANDSCAPE_SCREEN_SMALL (≤1024px) = tablet overrides
    - MOBILE_LANDSCAPE_SCREEN_SMALL (≤640px) = mobile overrides
    """
    # Build tree-position → component key mapping from the desktop tree
    key_map: dict[str, str] = {}
    _build_tree_position_map(desktop_data, comp_def, key_map, [])

    # Build tree-position → styles for tablet and mobile
    tablet_styles = _flatten_tree_styles(tablet_data, [])
    mobile_styles = _flatten_tree_styles(mobile_data, [])
    desktop_styles = _flatten_tree_styles(desktop_data, [])

    viewports = [
        ("TABLET_LANDSCAPE_SCREEN_SMALL", tablet_styles),
        ("MOBILE_LANDSCAPE_SCREEN_SMALL", mobile_styles),
    ]

    override_count = 0
    for resolution_name, vp_styles in viewports:
        for pos, vp_style in vp_styles.items():
            comp_key = key_map.get(pos)
            if not comp_key or comp_key not in comp_def:
                continue

            desk_style = desktop_styles.get(pos, {})
            overrides: dict[str, dict[str, str]] = {}
            for prop, val in vp_style.items():
                if val != desk_style.get(prop, "") and val:
                    overrides[prop] = {"value": val}

            if not overrides:
                continue

            comp = comp_def[comp_key]
            sp = comp.get("styleProperties", {})
            if sp:
                for sdata in sp.values():
                    sdata.setdefault("resolutions", {})[resolution_name] = overrides
                    break
            else:
                style_id = uuid.uuid4().hex[:22]
                comp["styleProperties"] = {
                    style_id: {
                        "resolutions": {
                            resolution_name: overrides,
                        }
                    }
                }
            override_count += 1

    logger.info("Merged %d responsive overrides (tablet + mobile)", override_count)


def _build_tree_position_map(
    extraction_data: list[dict],
    comp_def: dict[str, Any],
    key_map: dict[str, str],
    path: list[int],
) -> None:
    """Walk desktop extraction tree and comp_def tree in parallel to map
    tree positions to component keys.

    The extraction and conversion happen in the same order (same children,
    same depth limits), so child indices align.
    """
    # Get the root's children keys in display order
    if not path:
        root = comp_def.get("root", {})
        root_children = root.get("children", {})
        child_keys = _sorted_child_keys(comp_def, root_children)

        for i, el_data in enumerate(extraction_data[:20]):
            pos_str = str(i)
            if i < len(child_keys):
                key_map[pos_str] = child_keys[i]
                _map_children_recursive(el_data, comp_def, child_keys[i], key_map, [i])


def _map_children_recursive(
    el_data: dict,
    comp_def: dict[str, Any],
    comp_key: str,
    key_map: dict[str, str],
    path: list[int],
) -> None:
    """Recursively match extraction children to component children."""
    comp = comp_def.get(comp_key, {})
    comp_children = comp.get("children", {})
    child_keys = _sorted_child_keys(comp_def, comp_children)
    el_children = el_data.get("children", [])

    for i, child_el in enumerate(el_children):
        if i >= len(child_keys):
            break
        child_path = path + [i]
        pos_str = ".".join(str(p) for p in child_path)
        key_map[pos_str] = child_keys[i]
        _map_children_recursive(child_el, comp_def, child_keys[i], key_map, child_path)


def _sorted_child_keys(comp_def: dict[str, Any], children: dict[str, bool]) -> list[str]:
    """Return child keys sorted by displayOrder."""
    return sorted(
        children.keys(),
        key=lambda k: comp_def.get(k, {}).get("displayOrder", 0),
    )


def _flatten_tree_styles(
    extraction_data: list[dict],
    path: list[int],
) -> dict[str, dict[str, str]]:
    """Flatten an extraction tree into {position_string → styles}."""
    result: dict[str, dict[str, str]] = {}

    for i, el_data in enumerate(extraction_data[:20] if not path else extraction_data):
        child_path = path + [i] if path else [i]
        pos_str = ".".join(str(p) for p in child_path) if len(child_path) > 1 else str(child_path[0])
        result[pos_str] = el_data.get("styles", {})

        for child_result in [_flatten_tree_styles(el_data.get("children", []), child_path)]:
            result.update(child_result)

    return result


def _merge_pseudo_styles(
    comp_def: dict[str, Any],
    desktop_data: list[dict],
    pseudo_map: dict[str, Any],
) -> None:
    """Merge pseudo-class styles into component styleProperties.

    pseudo_map keys are tree positions ("0.1.3") from the JS extraction.
    Values are: {"elId": "...", "tag": "...", "pseudoStyles": {"hover": {prop: val}, ...}}

    Modlix format: inline pseudo in property name → "color:hover": {"value": "#red"}
    within the same resolution block (ALL).
    """
    if not pseudo_map:
        return

    # Build position → component key map
    key_map: dict[str, str] = {}
    _build_tree_position_map(desktop_data, comp_def, key_map, [])

    pseudo_count = 0
    for pos_str, pseudo_data in pseudo_map.items():
        comp_key = key_map.get(pos_str)
        if not comp_key or comp_key not in comp_def:
            continue

        pseudo_styles = pseudo_data.get("pseudoStyles", {})
        if not pseudo_styles:
            continue

        comp = comp_def[comp_key]
        sp = comp.get("styleProperties", {})

        # Find the default style entry (no pseudoState) to inject into
        default_sid = None
        default_sdata = None
        for sid, sdata in sp.items():
            if not sdata.get("pseudoState"):
                default_sid = sid
                default_sdata = sdata
                break

        if not default_sdata:
            # Create one if none exists
            default_sid = uuid.uuid4().hex[:22]
            default_sdata = {"resolutions": {"ALL": {}}}
            sp[default_sid] = default_sdata
            comp["styleProperties"] = sp

        all_res = default_sdata.setdefault("resolutions", {}).setdefault("ALL", {})

        for state, props in pseudo_styles.items():
            for prop, val in props.items():
                all_res[f"{prop}:{state}"] = {"value": str(val)}
                pseudo_count += 1

    logger.info("Merged %d pseudo-class style properties (hover/focus/active/disabled)", pseudo_count)


def _convert_browser_element(
    el_data: dict[str, Any],
    base_url: str,
    comp_def: dict[str, Any],
    depth: int,
) -> str | None:
    """Convert a browser-extracted element to a Modlix component."""
    if depth > 8:
        return None

    tag = el_data.get("tag", "div")
    comp_type = _TAG_TYPE_MAP.get(tag, "Grid")
    styles = el_data.get("styles", {})
    text = el_data.get("text", "")
    children_data = el_data.get("children", [])

    # Generate key
    el_id = el_data.get("id", "")
    if el_id:
        key = re.sub(r"[^a-zA-Z0-9_]", "_", el_id)[:30]
    else:
        key = _gen_key(tag[:3])
    while key in comp_def:
        key = key + "_" + uuid.uuid4().hex[:4]

    properties: dict[str, Any] = {}
    children_map: dict[str, bool] = {}

    # Type-specific handling
    href = el_data.get("href", "")

    if tag == "img" or tag == "picture":
        comp_type = "Image"
        src = el_data.get("src", "")
        properties["src"] = {"value": src}
        alt = el_data.get("alt", "")
        if alt:
            properties["alt"] = {"value": alt}

    elif tag == "iframe":
        comp_type = "Iframe"
        src = el_data.get("src", "")
        if src:
            properties["src"] = {"value": src}

    elif tag == "video":
        # Video with poster → Image; otherwise Grid container
        poster = el_data.get("src", "")
        if poster:
            comp_type = "Image"
            properties["src"] = {"value": poster}
            properties["alt"] = {"value": "Video"}
        else:
            comp_type = "Grid"

    elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "label",
                 "figcaption", "blockquote", "code", "pre"):
        comp_type = "Text"
        all_text = text
        if not all_text:
            all_text = _collect_text(el_data)
        if all_text:
            properties["text"] = {"value": all_text[:500]}

    elif tag == "a":
        classes = str(el_data.get("classes", "")).lower()
        if "btn" in classes or "button" in classes or "cta" in classes:
            comp_type = "Button"
            properties["label"] = {"value": text[:100] or "Link"}
            properties["designType"] = {"value": "_outlined"}
        else:
            # Links with children (images, grids) → Grid with linkPath
            if children_data and not text:
                comp_type = "Grid"
            else:
                comp_type = "Text"
                if text:
                    properties["text"] = {"value": text[:300]}
        # Preserve link target
        if href and not href.startswith("javascript:"):
            if comp_type == "Grid":
                properties["linkPath"] = {"value": href}
            elif comp_type == "Button":
                properties["onClick"] = {
                    "value": f"Page.navigate('{href}')",
                }

    elif tag == "button":
        comp_type = "Button"
        btn_text = text or _collect_text(el_data)
        properties["label"] = {"value": btn_text[:100] or "Button"}
        properties["designType"] = {"value": "_outlined"}

    elif tag in ("input", "textarea"):
        comp_type = "TextBox" if tag == "input" else "TextArea"
        placeholder = el_data.get("placeholder", "")
        if placeholder:
            properties["placeholder"] = {"value": placeholder}

    # For Grid types, process children
    if comp_type == "Grid":
        # Override Modlix's default 5px gap — use source gap or 0
        if "gap" not in styles:
            styles["gap"] = "0px"

        # Set ROWLAYOUT if the source element lays children out horizontally
        if el_data.get("isRowLayout"):
            properties["layout"] = {"value": "ROWLAYOUT"}
            # Remove display:flex from styles since Grid handles it via layout prop
            styles.pop("display", None)
            styles.pop("flexDirection", None)

        child_count = 0
        for child_data in children_data:
            if child_count >= 25:
                break
            child_key = _convert_browser_element(child_data, base_url, comp_def, depth + 1)
            if child_key:
                comp_def[child_key]["displayOrder"] = child_count
                children_map[child_key] = True
                child_count += 1

        # If Grid has direct text and no children with that text, add as Text child
        if text and not children_map:
            text_key = _gen_key("txt")
            comp_def[text_key] = {
                "key": text_key, "name": text_key, "type": "Text",
                "properties": {"text": {"value": text[:300]}},
                "styleProperties": {}, "children": {}, "displayOrder": 0,
            }
            children_map[text_key] = True

    # Clean up styles that Grid handles via properties
    if comp_type == "Grid":
        styles.pop("display", None)  # Grid is always flex

    # Image-specific style defaults
    if comp_type == "Image":
        if "objectFit" not in styles:
            styles["objectFit"] = "cover"
        if "width" not in styles:
            styles["width"] = "100%"

    # Build styleProperties from computed styles
    style_props = _make_style_properties(styles) if styles else {}

    comp_def[key] = {
        "key": key, "name": key, "type": comp_type,
        "properties": properties, "styleProperties": style_props,
        "children": children_map, "displayOrder": 0,
    }
    return key


def _collect_text(el_data: dict[str, Any]) -> str:
    """Recursively collect all text from an element tree."""
    parts = []
    text = el_data.get("text", "")
    if text:
        parts.append(text)
    for child in el_data.get("children", []):
        child_text = _collect_text(child)
        if child_text:
            parts.append(child_text)
    return " ".join(parts)[:500]
